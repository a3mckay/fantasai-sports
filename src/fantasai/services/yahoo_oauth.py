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
    resp.raise_for_status()
    return ET.fromstring(resp.text)


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

    Returns a list of dicts with: league_key, name, num_teams, scoring_type, season
    """
    leagues: list[dict[str, Any]] = []
    try:
        root = _yahoo_get(
            access_token,
            "users;use_login=1/games;game_keys=mlb/leagues",
        )
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
    except Exception:
        _log.warning("Could not fetch Yahoo leagues", exc_info=True)
    return leagues


def fetch_league_settings(access_token: str, league_key: str) -> dict[str, Any]:
    """Fetch detailed league settings: scoring categories, roster positions."""
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
        result["roster_positions"] = list(dict.fromkeys(roster_positions))  # deduplicate
    except Exception:
        _log.warning("Could not fetch league settings for %s", league_key, exc_info=True)
    return result


def fetch_user_team(access_token: str, league_key: str, yahoo_guid: str) -> Optional[dict[str, Any]]:
    """Find the user's team within a league by their GUID.

    Returns dict with: team_key, name, manager_name or None if not found.
    """
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
            # Check manager GUID
            for manager_elem in team_elem.iter():
                if manager_elem.tag.endswith("guid") and manager_elem.text:
                    if manager_elem.text.strip() == yahoo_guid:
                        return {
                            "team_key": data.get("team_key", ""),
                            "name": data.get("name", ""),
                            "manager_name": data.get("manager_display_name", ""),
                        }
    except Exception:
        _log.warning("Could not fetch teams for league %s", league_key, exc_info=True)
    return None


def fetch_team_roster(access_token: str, team_key: str) -> list[str]:
    """Fetch a team's current roster as a list of player name strings.

    Returns player names (roster stored as JSON in the Team model).
    """
    roster: list[str] = []
    try:
        root = _yahoo_get(access_token, f"team/{team_key}/roster")
        for player_elem in root.iter():
            if player_elem.tag.endswith("full") and player_elem.text:
                name = player_elem.text.strip()
                if name and name not in roster:
                    roster.append(name)
    except Exception:
        _log.warning("Could not fetch roster for team %s", team_key, exc_info=True)
    return roster
