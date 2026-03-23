"""Authentication and Yahoo OAuth routes."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
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
    fetch_league_settings,
    fetch_team_roster,
    fetch_user_guid,
    fetch_user_mlb_leagues,
    fetch_user_team,
    generate_state,
    get_auth_url,
    refresh_access_token,
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
def verify(body: VerifyRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Verify a Firebase ID token and create/retrieve the corresponding User row.

    Called by the frontend immediately after Firebase sign-in.
    Returns the user profile and onboarding status.
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
    """Return a valid Yahoo access token, refreshing automatically if expired.

    Yahoo access tokens expire after 1 hour. This checks the stored expiry and
    refreshes proactively if within 5 minutes of expiry.
    """
    from datetime import timedelta

    needs_refresh = (
        conn.token_expiry is None
        or conn.token_expiry <= datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    )

    if needs_refresh and conn.encrypted_refresh_token:
        _log.info("Yahoo access token expired/expiring — refreshing for connection %s", conn.id)
        refresh_tok = decrypt_token(conn.encrypted_refresh_token)
        token_data = refresh_access_token(refresh_tok)
        access_token: str = token_data["access_token"]
        new_refresh = token_data.get("refresh_token", refresh_tok)
        expires_in = int(token_data.get("expires_in", 3600))

        conn.encrypted_access_token = encrypt_token(access_token)
        conn.encrypted_refresh_token = encrypt_token(new_refresh)
        conn.token_expiry = _now_plus_seconds(expires_in)
        db.flush()
        return access_token

    return decrypt_token(conn.encrypted_access_token)


def _import_yahoo_league(
    db: Session,
    user: User,
    conn: YahooConnection,
    access_token: str,
) -> None:
    """Pull the user's most recent MLB league + team from Yahoo and upsert into DB."""
    from fantasai.models.league import League, Team

    leagues = fetch_user_mlb_leagues(access_token)
    if not leagues:
        _log.info("No Yahoo MLB leagues found for user %s", user.id)
        return

    # Pick the most-recent-season league
    league_info = sorted(leagues, key=lambda x: x.get("season", ""), reverse=True)[0]
    league_key = league_info["league_key"]
    conn.league_key = league_key

    # Fetch detailed settings
    settings_data = fetch_league_settings(access_token, league_key)

    # Upsert League
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
    }
    db.flush()

    # Find the user's team
    if conn.yahoo_guid:
        team_info = fetch_user_team(access_token, league_key, conn.yahoo_guid)
        if team_info:
            team_key = team_info["team_key"]
            conn.team_key = team_key
            roster = fetch_team_roster(access_token, team_key)

            team = db.query(Team).filter(
                Team.owner_user_id == user.id,
                Team.league_id == league_key,
            ).first()
            if team is None:
                team = Team(
                    league_id=league_key,
                    manager_name=user.name or team_info.get("manager_name", ""),
                )
                db.add(team)
            team.owner_user_id = user.id
            team.manager_name = user.name or team_info.get("manager_name", "")
            team.roster = roster
            _log.info("Imported team %s with %d players", team_key, len(roster))


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


@router.post("/yahoo/resync")
def yahoo_resync(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Re-import league and team data from Yahoo using stored tokens.

    Returns a detailed result for debugging — safe to call at any time.
    """
    conn = user.yahoo_connection
    if not conn or not conn.encrypted_access_token:
        raise HTTPException(status_code=400, detail="No Yahoo connection found")

    try:
        access_token = _get_valid_access_token(conn, db)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Token refresh/decryption failed: {exc}")

    result: dict[str, Any] = {"steps": []}

    try:
        leagues = fetch_user_mlb_leagues(access_token)
        result["steps"].append(f"fetch_user_mlb_leagues: {len(leagues)} league(s) found")
        result["leagues"] = leagues
    except Exception as exc:
        result["steps"].append(f"fetch_user_mlb_leagues FAILED: {exc}")
        return result

    if not leagues:
        result["steps"].append("No leagues — import skipped")
        return result

    league_info = sorted(leagues, key=lambda x: x.get("season", ""), reverse=True)[0]
    league_key = league_info["league_key"]
    result["league_key"] = league_key

    try:
        settings_data = fetch_league_settings(access_token, league_key)
        result["steps"].append(f"fetch_league_settings: {len(settings_data.get('stat_categories', []))} cats, {len(settings_data.get('roster_positions', []))} positions")
        result["settings_data"] = settings_data
    except Exception as exc:
        result["steps"].append(f"fetch_league_settings FAILED: {exc}")

    from fantasai.models.league import League, Team  # noqa: PLC0415

    try:
        league = db.query(League).filter(League.league_id == league_key).first()
        if league is None:
            league = League(
                league_id=league_key,
                platform="yahoo",
                sport="mlb",
                league_type=league_info.get("scoring_type", "head"),
            )
            db.add(league)
            result["steps"].append("League row created")
        else:
            result["steps"].append("League row already exists — updating")

        league.owner_user_id = user.id
        league.scoring_categories = settings_data.get("stat_categories") or []
        league.roster_positions = settings_data.get("roster_positions") or []
        league.settings = {
            "num_teams": league_info.get("num_teams"),
            "name": league_info.get("name"),
            "season": league_info.get("season"),
        }
        conn.league_key = league_key
        db.flush()
        result["steps"].append("League flushed OK")
    except Exception as exc:
        db.rollback()
        result["steps"].append(f"League upsert FAILED: {exc}")
        return result

    if conn.yahoo_guid:
        try:
            team_info = fetch_user_team(access_token, league_key, conn.yahoo_guid)
            result["steps"].append(f"fetch_user_team: {'found' if team_info else 'not found'}")
            if team_info:
                team_key = team_info["team_key"]
                conn.team_key = team_key
                roster = fetch_team_roster(access_token, team_key)
                result["steps"].append(f"fetch_team_roster: {len(roster)} players")
                result["roster_sample"] = roster[:5]

                team = db.query(Team).filter(
                    Team.owner_user_id == user.id,
                    Team.league_id == league_key,
                ).first()
                if team is None:
                    team = Team(league_id=league_key, manager_name=user.name or "")
                    db.add(team)
                    result["steps"].append("Team row created")
                else:
                    result["steps"].append("Team row already exists — updating")
                team.owner_user_id = user.id
                team.manager_name = user.name or team_info.get("manager_name", "")
                team.roster = roster
        except Exception as exc:
            result["steps"].append(f"Team import FAILED: {exc}")

    try:
        db.commit()
        conn.last_synced = datetime.now(tz=timezone.utc)
        db.commit()
        result["steps"].append("Committed OK")
        result["success"] = True
    except Exception as exc:
        db.rollback()
        result["steps"].append(f"Commit FAILED: {exc}")
        result["success"] = False

    return result
