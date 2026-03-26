"""Authentication and Yahoo OAuth routes."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasai.api.deps import get_current_user, get_db
from fantasai.config import settings
from fantasai.models.user import User, UserSettings, YahooConnection
from fantasai.services.encryption import decrypt_token, encrypt_token
from fantasai.services.firebase_auth import verify_firebase_token
from fantasai.services.yahoo_oauth import (
    _now_plus_seconds,
    exchange_code,
    fetch_all_league_teams,
    fetch_league_settings,
    fetch_team_roster,
    fetch_user_guid,
    fetch_user_mlb_leagues,
    fetch_user_team,
    generate_state,
    get_auth_url,
    refresh_access_token,
)
from fantasai.services.yahoo_sync import (
    get_valid_access_token as _get_valid_access_token_sync,
    import_yahoo_league as _import_yahoo_league,
    should_sync,
    sync_user_yahoo,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class VerifyRequest(BaseModel):
    id_token: str


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    date_of_birth: Optional[str] = None  # ISO date string "YYYY-MM-DD"
    managing_style: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    email: Optional[str]
    name: Optional[str]
    date_of_birth: Optional[str]
    managing_style: Optional[str]
    role: str
    onboarding_complete: bool
    yahoo_connected: bool

    model_config = {"from_attributes": True}


def _user_response(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
        "managing_style": user.managing_style,
        "role": user.role,
        "onboarding_complete": user.onboarding_complete,
        "yahoo_connected": user.yahoo_connection is not None,
    }


# ---------------------------------------------------------------------------
# Core auth routes
# ---------------------------------------------------------------------------


@router.post("/verify")
def verify(
    body: VerifyRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Verify a Firebase ID token and create/retrieve the corresponding User row.

    Called by the frontend immediately after Firebase sign-in.
    Returns the user profile and onboarding status.  If the user has a Yahoo
    connection whose data is stale (> 30 min old), a background sync is queued
    so roster data is fresh without blocking the login response.
    """
    try:
        claims = verify_firebase_token(body.id_token)
    except Exception as exc:
        _log.warning("Token verification failed (%s): %s", type(exc).__name__, exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired Firebase token ({type(exc).__name__}: {exc})",
        )

    firebase_uid: str = claims["sub"]
    email: str = claims.get("email", "")
    name: str = claims.get("name", "")

    user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
    is_new = user is None

    if is_new:
        user = User(
            firebase_uid=firebase_uid,
            email=email or None,
            name=name or None,
            role="user",
            onboarding_complete=False,
        )
        db.add(user)
        db.flush()  # get user.id before creating related rows

        # Create default settings row
        db.add(UserSettings(user_id=user.id))
        db.commit()
        db.refresh(user)

        # Send welcome email (best-effort)
        if email:
            try:
                from fantasai.services.email import send_welcome
                send_welcome(email, name or "Manager")
            except Exception:
                pass

        _log.info("New user created: %s (%s)", firebase_uid, email)
    else:
        # Update email/name from Firebase in case they changed
        if email and user.email != email:
            user.email = email
        if name and user.name != name and not user.name:
            user.name = name
        db.commit()

    # Trigger a background Yahoo sync if the user's data is stale.
    # This is non-blocking — the response returns immediately and the sync
    # runs after the HTTP response has been sent.
    if user.yahoo_connection and should_sync(user.yahoo_connection.last_synced):
        background_tasks.add_task(sync_user_yahoo, str(user.id))
        _log.info("Queued background Yahoo sync for user %s", user.id)

    return {"user": _user_response(user), "is_new": is_new}


@router.get("/me")
def get_me(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Return the current user's profile."""
    return _user_response(user)


@router.put("/me")
def update_me(
    body: ProfileUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update mutable profile fields: name, date_of_birth, managing_style."""
    if body.name is not None:
        user.name = body.name
    if body.managing_style is not None:
        user.managing_style = body.managing_style
    if body.date_of_birth is not None:
        from datetime import date
        try:
            user.date_of_birth = date.fromisoformat(body.date_of_birth)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_of_birth must be YYYY-MM-DD")
    db.commit()
    return _user_response(user)


@router.post("/complete-onboarding")
def complete_onboarding(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Mark onboarding as complete. Called at end of FTUE wizard."""
    user.onboarding_complete = True
    db.commit()
    return {"onboarding_complete": True}


# ---------------------------------------------------------------------------
# Yahoo OAuth routes
# ---------------------------------------------------------------------------


@router.get("/yahoo/connect")
def yahoo_connect(
    user: User = Depends(get_current_user),
    response: Response = None,
) -> dict[str, str]:
    """Return the Yahoo authorization URL. Frontend should redirect the user there."""
    if not settings.yahoo_client_id:
        raise HTTPException(status_code=503, detail="Yahoo OAuth not configured")
    state = generate_state()
    # State is passed back in the query string — we embed the user ID for lookup
    auth_url = get_auth_url(state=f"{user.id}:{state}")
    return {"auth_url": auth_url, "state": state}


@router.get("/yahoo/callback")
def yahoo_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Yahoo redirects here after user approves access.

    Exchanges the code for tokens, imports league/team data, then redirects
    the frontend to /onboarding?yahoo=connected (or ?yahoo=error on failure).
    """
    frontend_base = settings.app_url

    if error or not code:
        _log.warning("Yahoo OAuth error: %s", error)
        return RedirectResponse(url=f"{frontend_base}/onboarding?yahoo=error&reason={error or 'no_code'}")

    # Extract user_id from state
    user_id: Optional[str] = None
    if state and ":" in state:
        user_id = state.split(":", 1)[0]

    if not user_id:
        return RedirectResponse(url=f"{frontend_base}/onboarding?yahoo=error&reason=invalid_state")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse(url=f"{frontend_base}/onboarding?yahoo=error&reason=user_not_found")

    try:
        token_data = exchange_code(code)
        access_token: str = token_data["access_token"]
        refresh_token: str = token_data.get("refresh_token", "")
        expires_in: int = int(token_data.get("expires_in", 3600))
        yahoo_guid: str = token_data.get("xoauth_yahoo_guid", "")

        # Encrypt tokens before storing
        enc_access = encrypt_token(access_token)
        enc_refresh = encrypt_token(refresh_token) if refresh_token else None

        # Upsert YahooConnection
        conn = user.yahoo_connection
        if conn is None:
            conn = YahooConnection(user_id=user.id)
            db.add(conn)

        conn.yahoo_guid = yahoo_guid or fetch_user_guid(access_token)
        conn.encrypted_access_token = enc_access
        conn.encrypted_refresh_token = enc_refresh
        conn.token_expiry = _now_plus_seconds(expires_in)
        conn.last_synced = datetime.now(tz=timezone.utc)
        db.flush()

        # Import league + team data
        _import_yahoo_league(db, user, conn, access_token)

        db.commit()
        _log.info("Yahoo connected for user %s (guid=%s)", user.id, conn.yahoo_guid)
        return RedirectResponse(url=f"{frontend_base}/onboarding?yahoo=connected&step=3")

    except Exception as exc:
        db.rollback()
        _log.error("Yahoo OAuth callback failed for user %s: %s", user_id, exc, exc_info=True)
        return RedirectResponse(url=f"{frontend_base}/onboarding?yahoo=error&reason=token_exchange")


def _get_valid_access_token(conn: YahooConnection, db: Session) -> str:
    """Return a valid Yahoo access token, refreshing automatically if expiring.

    Delegates to yahoo_sync.get_valid_access_token — kept here so existing
    callers within this file continue to work unchanged.
    """
    return _get_valid_access_token_sync(conn, db)


# _import_yahoo_league is now defined in fantasai.services.yahoo_sync and re-exported
# via the import alias _import_yahoo_league at the top of this file.
# Kept as a local alias so the yahoo_callback route below can call it unchanged.


@router.delete("/yahoo/disconnect")
def yahoo_disconnect(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Remove the user's Yahoo connection."""
    if user.yahoo_connection:
        db.delete(user.yahoo_connection)
        db.commit()
    return {"status": "disconnected"}


@router.get("/yahoo/status")
def yahoo_status(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Return whether the user has a Yahoo connection and basic connection info."""
    conn = user.yahoo_connection
    if not conn:
        return {"connected": False}
    return {
        "connected": True,
        "league_key": conn.league_key,
        "team_key": conn.team_key,
        "last_synced": conn.last_synced.isoformat() if conn.last_synced else None,
    }


class ResyncTeamRequest(BaseModel):
    team_key: str
    team_name: str
    manager_name: str = ""


@router.post("/yahoo/resync/start")
def yahoo_resync_start(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Step 1 of a progressive resync.

    Fetches league settings and the list of all teams (without their rosters).
    Commits the league row and returns the team list so the frontend can drive
    per-team imports with progress feedback.
    """
    from fantasai.models.league import League

    conn = user.yahoo_connection
    if not conn or not conn.encrypted_access_token:
        raise HTTPException(status_code=400, detail="No Yahoo connection found")

    try:
        access_token = _get_valid_access_token(conn, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Token refresh failed: {exc}")

    try:
        leagues = fetch_user_mlb_leagues(access_token)
    except Exception as exc:
        _log.error("fetch_user_mlb_leagues failed for user %s: %s", user.id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail=f"Yahoo API error: {exc}")
    if not leagues:
        raise HTTPException(status_code=404, detail="No Yahoo MLB leagues found — your account may have no active MLB leagues this season")

    league_info = sorted(leagues, key=lambda x: x.get("season", ""), reverse=True)[0]
    league_key = league_info["league_key"]

    settings_data = fetch_league_settings(access_token, league_key)

    league = db.query(League).filter(League.league_id == league_key).first()
    if league is None:
        league = League(
            league_id=league_key,
            platform="yahoo",
            sport="mlb",
            league_type=league_info.get("scoring_type", "head"),
        )
        db.add(league)

    league.owner_user_id = user.id
    league.scoring_categories = settings_data.get("stat_categories") or []
    league.roster_positions = settings_data.get("roster_positions") or []
    league.settings = {
        "num_teams": league_info.get("num_teams"),
        "name": league_info.get("name"),
        "season": league_info.get("season"),
        "keepers_per_team": settings_data.get("num_keepers", 0),
    }
    conn.league_key = league_key

    # Fetch all team stubs (no rosters yet) and mark the user's team
    all_teams = fetch_all_league_teams(access_token, league_key)
    teams_out = []
    for t in all_teams:
        is_mine = t.get("yahoo_guid") == conn.yahoo_guid
        if is_mine:
            conn.team_key = t["team_key"]
        teams_out.append({
            "team_key": t["team_key"],
            "team_name": t["name"],
            "manager_name": t.get("manager_name", ""),
            "is_mine": is_mine,
        })

    db.commit()

    return {
        "league_key": league_key,
        "league_name": league_info.get("name", ""),
        "num_teams": league_info.get("num_teams"),
        "teams": teams_out,
    }


@router.post("/yahoo/resync/team")
def yahoo_resync_team(
    body: ResyncTeamRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Step 2 of a progressive resync — import one team's roster.

    Called once per team by the frontend after resync/start.
    Fetches the roster, resolves player names to FanGraphs IDs, and upserts
    the Team row. Returns counts for progress display.
    """
    from fantasai.models.league import Team
    from fantasai.services.name_resolver import resolve_player_names

    conn = user.yahoo_connection
    if not conn or not conn.league_key:
        raise HTTPException(status_code=400, detail="Run resync/start first")

    try:
        access_token = _get_valid_access_token(conn, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Token refresh failed: {exc}")

    team_key = body.team_key
    team_name = body.team_name
    manager_name = body.manager_name
    is_my_team = team_key == conn.team_key

    # Fetch and resolve roster
    from fantasai.services.yahoo_sync import _update_player_positions_from_yahoo
    roster_data = fetch_team_roster(access_token, team_key)
    roster_names = [p["name"] for p in roster_data]
    resolved = resolve_player_names(roster_names, db)
    roster_ids = [v for v in resolved.values() if v is not None]
    _update_player_positions_from_yahoo(db, roster_data, resolved)

    # Upsert team row — match by team_name, fall back to owner lookup for user's team
    team_row = db.query(Team).filter(
        Team.league_id == conn.league_key,
        Team.team_name == team_name,
    ).first()
    if team_row is None and is_my_team:
        team_row = db.query(Team).filter(
            Team.owner_user_id == user.id,
            Team.league_id == conn.league_key,
        ).first()
    if team_row is None:
        team_row = Team(league_id=conn.league_key, manager_name=manager_name)
        db.add(team_row)

    team_row.team_name = team_name
    team_row.manager_name = manager_name
    team_row.roster_names = roster_names
    team_row.roster = roster_ids
    if is_my_team:
        team_row.owner_user_id = user.id
        conn.last_synced = datetime.now(tz=timezone.utc)

    db.commit()

    unresolved = [name for name, pid in resolved.items() if pid is None]

    return {
        "team_name": team_name,
        "roster_count": len(roster_names),
        "resolved_count": len(roster_ids),
        "unresolved_names": unresolved,
"is_mine": is_my_team,
    }



@router.post("/yahoo/resync")
def yahoo_resync(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Legacy single-shot resync kept for backwards compatibility.

    Prefer the progressive resync/start + resync/team endpoints for UI use.
    """
    conn = user.yahoo_connection
    if not conn or not conn.encrypted_access_token:
        raise HTTPException(status_code=400, detail="No Yahoo connection found")
    try:
        access_token = _get_valid_access_token(conn, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Token refresh failed: {exc}")
    try:
        _import_yahoo_league(db, user, conn, access_token)
        conn.last_synced = datetime.now(tz=timezone.utc)
        db.commit()
        return {"success": True}
    except Exception as exc:
        db.rollback()
        return {"success": False, "error": str(exc)}


@router.get("/leagues")
def list_user_leagues(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return all Yahoo leagues owned by the user, with an is_active flag.

    Used by the frontend league switcher to show all leagues the user has
    synced.  The active league is whichever matches conn.league_key.
    """
    from fantasai.models.league import League

    conn = user.yahoo_connection
    active_key = conn.league_key if conn else None

    leagues = db.query(League).filter(League.owner_user_id == user.id).all()
    return [
        {
            "league_id": lg.league_id,
            "league_name": (lg.settings or {}).get("name", lg.league_id),
            "season": (lg.settings or {}).get("season"),
            "num_teams": (lg.settings or {}).get("num_teams"),
            "league_type": lg.league_type,
            "is_active": lg.league_id == active_key,
        }
        for lg in sorted(leagues, key=lambda x: (x.settings or {}).get("season", ""), reverse=True)
    ]


@router.post("/leagues/{league_id}/activate")
def activate_league(
    league_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Switch the user's active league to league_id.

    Updates conn.league_key and conn.team_key (to the user's team in that
    league), then returns the full league payload so the frontend can
    immediately update LeagueContext without a second request.
    """
    from fantasai.models.league import League, Team

    conn = user.yahoo_connection
    if not conn:
        raise HTTPException(status_code=400, detail="No Yahoo connection")

    league = db.query(League).filter(
        League.league_id == league_id,
        League.owner_user_id == user.id,
    ).first()
    if not league:
        raise HTTPException(status_code=404, detail="League not found")

    # Update active pointers
    conn.league_key = league_id

    # Find user's team in this league
    my_team = db.query(Team).filter(
        Team.owner_user_id == user.id,
        Team.league_id == league_id,
    ).first()
    if my_team and my_team.yahoo_team_key:
        conn.team_key = my_team.yahoo_team_key

    db.commit()

    # Return the full league payload (same shape as GET /auth/league)
    return _build_league_payload(user, league, db)


def _build_league_payload(user: User, league: "Any", db: Session) -> dict[str, Any]:
    """Build the full league payload shared by GET /auth/league and POST /auth/leagues/{id}/activate."""
    from fantasai.models.league import Team
    from fantasai.models.player import Player as PlayerModel

    all_teams = db.query(Team).filter(Team.league_id == league.league_id).all()

    all_roster_ids: set[int] = set()
    for t in all_teams:
        all_roster_ids.update(t.roster or [])

    player_name_map: dict[int, str] = {}
    if all_roster_ids:
        rows = (
            db.query(PlayerModel.player_id, PlayerModel.name)
            .filter(PlayerModel.player_id.in_(all_roster_ids))
            .all()
        )
        player_name_map = {r.player_id: r.name for r in rows}

    my_team = next((t for t in all_teams if t.owner_user_id == user.id), None)

    teams_out = [
        {
            "team_id": t.team_id,
            "team_name": t.team_name or t.manager_name or f"Team {t.team_id}",
            "manager_name": t.manager_name,
            "is_mine": t.owner_user_id == user.id,
            "roster": [
                {"player_id": pid, "name": player_name_map.get(pid, f"Player {pid}")}
                for pid in (t.roster or [])
            ],
            "roster_names": t.roster_names or [],
        }
        for t in all_teams
    ]

    return {
        "league_id": league.league_id,
        "league_name": (league.settings or {}).get("name", ""),
        "platform": league.platform,
        "sport": league.sport,
        "league_type": league.league_type,
        "num_teams": (league.settings or {}).get("num_teams"),
        "season": (league.settings or {}).get("season"),
        "keepers_per_team": (league.settings or {}).get("keepers_per_team", 0),
        "scoring_categories": league.scoring_categories or [],
        "roster_positions": league.roster_positions or [],
        "my_team_id": my_team.team_id if my_team else None,
        "teams": teams_out,
    }


@router.get("/league")
def get_league(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the user's active Yahoo league settings and all teams.

    Used by the frontend LeagueContext to auto-populate league info.
    Returns 404 if the user has no connected Yahoo league.
    """
    from fantasai.models.league import League

    conn = user.yahoo_connection
    if not conn or not conn.league_key:
        raise HTTPException(status_code=404, detail="No Yahoo league connected")

    league = db.query(League).filter(League.league_id == conn.league_key).first()
    if not league:
        raise HTTPException(status_code=404, detail="League not found in database")

    return _build_league_payload(user, league, db)
