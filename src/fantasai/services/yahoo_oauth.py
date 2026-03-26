"""Yahoo Fantasy OAuth 2.0 flow and Fantasy API helpers."""
from __future__ import annotations

import logging
import secrets
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from fantasai.config import settings

_log = logging.getLogger(__name__)

_YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
_YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
_YAHOO_FANTASY_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


def generate_state() -> str:
    """Generate a cryptographically secure CSRF state token."""
    return secrets.token_urlsafe(32)


def get_auth_url(state: str) -> str:
    """Return the Yahoo authorization URL the user should be redirected to."""
    params = {
        "client_id": settings.yahoo_client_id,
        "redirect_uri": settings.yahoo_redirect_uri,
        "response_type": "code",
        "state": state,
        "scope": "openid",  # Yahoo requires at least openid; Fantasy Sports read is granted by app perms
    }
    return f"{_YAHOO_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange an authorization code for access + refresh tokens.

    Returns a dict with: access_token, refresh_token, expires_in, token_type, xoauth_yahoo_guid
    """
    resp = httpx.post(
        _YAHOO_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.yahoo_redirect_uri,
        },
        auth=(settings.yahoo_client_id, settings.yahoo_client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Use the refresh token to get a new access token."""
    resp = httpx.post(
        _YAHOO_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        auth=(settings.yahoo_client_id, settings.yahoo_client_secret),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def token_expiry_from_response(token_data: dict[str, Any]) -> datetime:
    """Compute expiry datetime from token response (uses expires_in seconds)."""
    expires_in = int(token_data.get("expires_in", 3600))
    return datetime.now(tz=timezone.utc).replace(microsecond=0).__class__(
        *datetime.now(tz=timezone.utc).timetuple()[:6],
        tzinfo=timezone.utc,
    ).__class__(
        datetime.now(tz=timezone.utc).year,
        datetime.now(tz=timezone.utc).month,
        datetime.now(tz=timezone.utc).day,
        datetime.now(tz=timezone.utc).hour,
        datetime.now(tz=timezone.utc).minute,
        datetime.now(tz=timezone.utc).second + expires_in,
        tzinfo=timezone.utc,
    )


def _now_plus_seconds(seconds: int) -> datetime:
    from datetime import timedelta
    return datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Yahoo Fantasy API helpers
# ---------------------------------------------------------------------------

_NS = {
    "y": "http://fantasysports.yahooapis.com/fantasy/v2/base.rng",
}


def _yahoo_get(access_token: str, path: str) -> ET.Element:
    """Make a GET request to the Yahoo Fantasy API and return the XML root element."""
    url = f"{_YAHOO_FANTASY_BASE}/{path}"
    resp = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if not resp.is_success:
        _log.warning(
            "Yahoo API HTTP %s for %s — body: %s",
            resp.status_code, path, resp.text[:500],
        )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    _log.debug("Yahoo API response for %s:\n%s", path, resp.text[:2000])
    return root


def fetch_user_guid(access_token: str) -> Optional[str]:
    """Fetch the Yahoo user's GUID."""
    try:
        root = _yahoo_get(access_token, "users;use_login=1")
        # Try to find guid in the XML tree
        for elem in root.iter():
            if elem.tag.endswith("guid") and elem.text:
                return elem.text.strip()
    except Exception:
        _log.warning("Could not fetch Yahoo user GUID", exc_info=True)
    return None


def fetch_user_mlb_leagues(access_token: str) -> list[dict[str, Any]]:
    """Fetch the user's active MLB fantasy leagues.

    Returns a list of dicts with: league_key, name, num_teams, scoring_type, season.
    Raises on HTTP or parse errors so callers can surface the real failure reason.
    """
    root = _yahoo_get(
        access_token,
        "users;use_login=1/games;game_keys=mlb/leagues",
    )
    leagues: list[dict[str, Any]] = []
    for league_elem in root.iter():
        if not league_elem.tag.endswith("league"):
            continue
        # Extract child elements as a flat dict
        data: dict[str, str] = {}
        for child in league_elem:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if child.text:
                data[tag] = child.text.strip()
        if "league_key" in data:
            leagues.append(
                {
                    "league_key": data.get("league_key", ""),
                    "name": data.get("name", ""),
                    "num_teams": int(data.get("num_teams", 0)),
                    "scoring_type": data.get("scoring_type", "head"),
                    "season": data.get("season", ""),
                }
            )
    _log.info("fetch_user_mlb_leagues: found %d league(s)", len(leagues))
    return leagues


def fetch_league_settings(access_token: str, league_key: str) -> dict[str, Any]:
    """Fetch detailed league settings: scoring categories, roster positions, keeper count."""
    result: dict[str, Any] = {}
    try:
        root = _yahoo_get(access_token, f"league/{league_key}/settings")
        # Extract stat categories
        stat_categories: list[str] = []
        for stat_elem in root.iter():
            if stat_elem.tag.endswith("display_name") and stat_elem.text:
                stat_categories.append(stat_elem.text.strip())
        result["stat_categories"] = stat_categories

        # Extract roster positions
        roster_positions: list[str] = []
        for pos_elem in root.iter():
            if pos_elem.tag.endswith("position") and pos_elem.text:
                roster_positions.append(pos_elem.text.strip())
        # Filter out empty strings and purely-numeric Yahoo slot type codes (e.g. "0", "1")
        roster_positions = [p for p in roster_positions if p and not p.isdigit()]
        result["roster_positions"] = list(dict.fromkeys(roster_positions))  # deduplicate

        # Extract keeper count (keeper leagues only — 0 for non-keeper leagues)
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag in ("max_keeper_positions", "num_keeper_positions", "keepers_count") and elem.text:
                try:
                    result["num_keepers"] = int(elem.text.strip())
                except ValueError:
                    pass
    except Exception:
        _log.warning("Could not fetch league settings for %s", league_key, exc_info=True)
    return result


def fetch_all_league_teams(access_token: str, league_key: str) -> list[dict[str, Any]]:
    """Fetch all teams in a league.

    Returns a list of dicts with: team_key, name, manager_name, yahoo_guid
    """
    teams: list[dict[str, Any]] = []
    try:
        root = _yahoo_get(access_token, f"league/{league_key}/teams")
        for team_elem in root.iter():
            if not team_elem.tag.endswith("team"):
                continue
            data: dict[str, str] = {}
            for child in team_elem:
                tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if child.text:
                    data[tag] = child.text.strip()
            # Extract manager GUID
            guid = ""
            for manager_elem in team_elem.iter():
                if manager_elem.tag.endswith("guid") and manager_elem.text:
                    guid = manager_elem.text.strip()
                    break
            if data.get("team_key"):
                teams.append({
                    "team_key": data["team_key"],
                    "name": data.get("name", ""),
                    "manager_name": data.get("manager_display_name", ""),
                    "yahoo_guid": guid,
                })
    except Exception:
        _log.warning("Could not fetch teams for league %s", league_key, exc_info=True)
    return teams


def fetch_user_team(access_token: str, league_key: str, yahoo_guid: str) -> Optional[dict[str, Any]]:
    """Find the user's team within a league by their GUID.

    Returns dict with: team_key, name, manager_name or None if not found.
    """
    for team in fetch_all_league_teams(access_token, league_key):
        if team["yahoo_guid"] == yahoo_guid:
            return team
    return None


# Bench/injury slot labels that carry no useful position information and should
# be excluded from eligible_positions.  "Util" is intentionally kept — it is the
# only meaningful position token for DH-only players (e.g. Ohtani as a batter
# in leagues that use Util instead of DH) and is canonicalised in the sync step.
_ROSTER_SLOT_LABELS = {
    "BN", "IL", "IL10", "IL15", "IL60",
    "IR", "NA", "DL", "Hitters", "Pitchers",
}


def _local_tag(elem: "ET.Element") -> str:  # type: ignore[name-defined]
    """Return the local (namespace-stripped) tag name of an XML element."""
    tag = elem.tag
    return tag.split("}")[-1] if "}" in tag else tag


def fetch_team_roster(access_token: str, team_key: str) -> list[dict]:
    """Fetch a team's current roster including Yahoo-sourced eligible positions.

    Returns a list of dicts::

        [{"name": "Juan Soto", "eligible_positions": ["OF"]}, ...]

    ``eligible_positions`` reflects exactly what Yahoo considers playable in
    this league (e.g. ``["OF"]`` vs ``["LF", "CF", "RF"]`` depending on league
    settings).  Bench/injury slots (BN, IL, Util, …) are excluded.
    """
    roster: list[dict] = []
    seen_names: set[str] = set()
    try:
        root = _yahoo_get(access_token, f"team/{team_key}/roster")
        for elem in root.iter():
            if _local_tag(elem) != "player":
                continue
            # ── Player name ──────────────────────────────────────────────────
            name: str | None = None
            for child in elem.iter():
                if _local_tag(child) == "full" and child.text and child.text.strip():
                    name = child.text.strip()
                    break
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            # ── Eligible positions ────────────────────────────────────────────
            eligible: list[str] = []
            for child in elem.iter():
                if _local_tag(child) == "eligible_positions":
                    for pos_child in child:
                        pos = (pos_child.text or "").strip()
                        if pos and not pos.isdigit() and pos not in _ROSTER_SLOT_LABELS:
                            if pos not in eligible:
                                eligible.append(pos)
                    break  # only one eligible_positions block per player
            # ── Selected position (roster slot player is IN) ──────────────────
            selected_pos = ""
            for child in elem.iter():
                if _local_tag(child) == "selected_position":
                    for pc in child:
                        if _local_tag(pc) == "position" and pc.text:
                            selected_pos = pc.text.strip()
                    break
            # ── Injury status ─────────────────────────────────────────────────
            yahoo_status = ""
            for child in elem.iter():
                if _local_tag(child) == "status" and child.text and child.text.strip():
                    yahoo_status = child.text.strip()
                    break
            roster.append({
                "name": name,
                "eligible_positions": eligible,
                "selected_position": selected_pos,
                "yahoo_status": yahoo_status,
            })
    except Exception:
        _log.warning("Could not fetch roster for team %s", team_key, exc_info=True)
    return roster
