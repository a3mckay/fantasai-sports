"""Yahoo league sync service.

Contains the core import logic and the background sync helpers used by both
the FastAPI BackgroundTasks (triggered on login) and the APScheduler job
(triggered every 2 hours).

Functions from auth.py that needed to be accessible here were moved to this
module to avoid circular imports:
  - _get_valid_access_token
  - _import_yahoo_league
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from fantasai.models.user import User, YahooConnection

_log = logging.getLogger(__name__)

# Don't sync more often than this when triggered by a login
_SYNC_THROTTLE_MINUTES = 30


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def should_sync(last_synced: Optional[datetime]) -> bool:
    """Return True if enough time has passed since the last sync."""
    if last_synced is None:
        return True
    if last_synced.tzinfo is None:
        last_synced = last_synced.replace(tzinfo=timezone.utc)
    threshold = datetime.now(tz=timezone.utc) - timedelta(minutes=_SYNC_THROTTLE_MINUTES)
    return last_synced < threshold


def get_valid_access_token(conn: "YahooConnection", db: "Session") -> str:
    """Return a valid Yahoo access token, refreshing automatically if expiring soon.

    Yahoo tokens expire after 1 hour.  Refreshes proactively if within 5 minutes
    of expiry.  Persists the new tokens to `conn` via db.flush().
    """
    from fantasai.services.encryption import decrypt_token, encrypt_token
    from fantasai.services.yahoo_oauth import _now_plus_seconds, refresh_access_token

    needs_refresh = (
        conn.token_expiry is None
        or conn.token_expiry.replace(tzinfo=timezone.utc if conn.token_expiry.tzinfo is None else conn.token_expiry.tzinfo)
        <= datetime.now(tz=timezone.utc) + timedelta(minutes=5)
    )

    if needs_refresh and conn.encrypted_refresh_token:
        _log.info("Yahoo token expiring — refreshing for connection %s", conn.id)
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


# ---------------------------------------------------------------------------
# League import (moved from auth.py)
# ---------------------------------------------------------------------------


def _update_player_positions_from_yahoo(
    db: "Session",
    roster_data: list[dict],
    resolved: dict[str, "int | None"],
) -> None:
    """Write Yahoo-sourced eligible positions back to Player.positions in the DB.

    Canonicalisation rules applied per entry before merging:
      • DH → Util   (DH is displayed as the Util slot in fantasy)
      • P  removed when SP or RP is already present (P is a generic pitcher
        slot — redundant and confusing alongside the specific SP/RP labels)
      • Util removed when the player has other, more specific positions
        (e.g. SS, 1B, OF) — Util is only kept for DH-only / utility players
        who have no real field position

    Two-way players (Ohtani) appear twice with different qualifiers; their
    batting and pitching eligible positions are merged into one deduplicated
    list so that Rankings can later filter by stat_type for display.
    """
    import re

    from fantasai.models.player import Player

    _PAREN = re.compile(r"\s*\([^)]*\)\s*$")
    _PITCHING = {"SP", "RP"}
    _FIELD_POSITIONS = {"C", "1B", "2B", "3B", "SS", "OF", "LF", "CF", "RF"}

    def _canonicalise(raw: list[str]) -> list[str]:
        # Step 1: DH → Util
        mapped = ["Util" if p == "DH" else p for p in raw]
        # Step 2: drop P when SP or RP present
        has_specific_pitcher = any(p in _PITCHING for p in mapped)
        if has_specific_pitcher:
            mapped = [p for p in mapped if p != "P"]
        # Step 3: drop Util when real field positions exist
        has_field_pos = any(p in _FIELD_POSITIONS for p in mapped)
        if has_field_pos:
            mapped = [p for p in mapped if p != "Util"]
        # Deduplicate while preserving order
        seen: set[str] = set()
        return [p for p in mapped if not (p in seen or seen.add(p))]  # type: ignore[func-returns-value]

    # Group by player_id, merging two-way player entries
    player_positions: dict[int, list[str]] = {}
    for entry in roster_data:
        raw_pos = entry.get("eligible_positions") or []
        if not raw_pos:
            continue
        name = entry["name"]
        player_id = resolved.get(name)
        if player_id is None:
            stripped = _PAREN.sub("", name).strip()
            player_id = resolved.get(stripped)
        if player_id is None:
            continue
        canonical = _canonicalise(raw_pos)
        existing = player_positions.setdefault(player_id, [])
        for pos in canonical:
            if pos not in existing:
                existing.append(pos)

    for player_id, positions in player_positions.items():
        if not positions:
            continue
        player = db.get(Player, player_id)
        if player is not None:
            player.positions = positions


def import_yahoo_league(
    db: "Session",
    user: "User",
    conn: "YahooConnection",
    access_token: str,
) -> None:
    """Pull the user's most recent MLB league + ALL team rosters from Yahoo and
    upsert them into the database.

    Called both during the initial OAuth callback and by the sync service.
    """
    from fantasai.models.league import League, Team
    from fantasai.services.name_resolver import resolve_player_names
    from fantasai.services.yahoo_oauth import (
        fetch_all_league_teams,
        fetch_league_settings,
        fetch_team_roster,
        fetch_user_mlb_leagues,
    )

    leagues = fetch_user_mlb_leagues(access_token)
    if not leagues:
        _log.info("No Yahoo MLB leagues found for user %s", user.id)
        return

    league_info = sorted(leagues, key=lambda x: x.get("season", ""), reverse=True)[0]
    league_key = league_info["league_key"]
    conn.league_key = league_key

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
    db.flush()

    all_teams = fetch_all_league_teams(access_token, league_key)
    _log.info("Syncing %d teams for league %s", len(all_teams), league_key)

    for team_info in all_teams:
        team_key = team_info["team_key"]
        is_my_team = team_info.get("yahoo_guid") == conn.yahoo_guid

        if is_my_team:
            conn.team_key = team_key

        roster_data = fetch_team_roster(access_token, team_key)
        roster_names = [p["name"] for p in roster_data]
        resolved = resolve_player_names(roster_names, db)
        roster_ids = [v for v in resolved.values() if v is not None]
        # Update Player.positions in the DB from Yahoo's eligible_positions data
        _update_player_positions_from_yahoo(db, roster_data, resolved)
        _log.debug(
            "Position update: %d roster entries processed for team %s",
            len(roster_data), team_key,
        )

        existing = db.query(Team).filter(
            Team.league_id == league_key,
            Team.team_name == team_info["name"],
        ).first()

        if existing is None and is_my_team:
            existing = db.query(Team).filter(
                Team.owner_user_id == user.id,
                Team.league_id == league_key,
            ).first()

        if existing is None:
            existing = Team(
                league_id=league_key,
                manager_name=team_info.get("manager_name", ""),
            )
            db.add(existing)

        existing.team_name = team_info["name"]
        existing.manager_name = team_info.get("manager_name", "")
        existing.roster_names = roster_names
        existing.roster = roster_ids
        if is_my_team:
            existing.owner_user_id = user.id

        _log.info(
            "Synced team '%s' (%s): %d players (%d resolved)",
            team_info["name"], team_key, len(roster_names), len(roster_ids),
        )

    # Player.positions in the DB have been updated from Yahoo's eligible_positions
    # data — bust the rankings cache so the next rankings request reflects the
    # new multi-position data immediately rather than waiting for TTL expiry.
    try:
        from fantasai.api.v1.recommendations import _RANKINGS_CACHE, _RANKINGS_RAW_CACHE
        _RANKINGS_CACHE.clear()
        _RANKINGS_RAW_CACHE.clear()
        _log.info("Rankings cache cleared after Yahoo sync — positions updated")
    except Exception:
        _log.debug("Could not clear rankings cache (non-fatal)", exc_info=True)


# ---------------------------------------------------------------------------
# Standalone sync functions (called from BackgroundTasks + scheduler)
# ---------------------------------------------------------------------------


def sync_user_yahoo(user_id: str) -> bool:
    """Sync one user's Yahoo league data. Returns True on success.

    Creates its own DB session — safe to call from a background thread or task.
    """
    from fantasai.database import SessionLocal
    from fantasai.models.user import User

    db: Session = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user or not user.yahoo_connection:
            _log.debug("sync_user_yahoo: no yahoo connection for user %s", user_id)
            return False

        conn = user.yahoo_connection
        access_token = get_valid_access_token(conn, db)
        import_yahoo_league(db, user, conn, access_token)
        conn.last_synced = datetime.now(tz=timezone.utc)
        db.commit()
        _log.info("Yahoo sync complete for user %s", user_id)
        return True
    except Exception:
        db.rollback()
        _log.warning("Yahoo sync failed for user %s", user_id, exc_info=True)
        return False
    finally:
        db.close()


def sync_all_yahoo_users() -> None:
    """Sync every user who has an active Yahoo connection.

    Called by the APScheduler every 2 hours.  A 1-second sleep between users
    keeps the request rate gentle on Yahoo's API.
    """
    from fantasai.database import SessionLocal
    from fantasai.models.user import YahooConnection

    db: Session = SessionLocal()
    try:
        connections = (
            db.query(YahooConnection)
            .filter(YahooConnection.encrypted_access_token.isnot(None))
            .all()
        )
        user_ids = [str(c.user_id) for c in connections]
    except Exception:
        _log.error("sync_all_yahoo_users: failed to query connections", exc_info=True)
        return
    finally:
        db.close()

    if not user_ids:
        return

    _log.info("Scheduled sync: %d Yahoo users", len(user_ids))
    for uid in user_ids:
        sync_user_yahoo(uid)
        time.sleep(1)
    _log.info("Scheduled sync complete")
