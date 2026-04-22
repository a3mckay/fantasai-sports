"""Scoring Grid service — fetches per-team weekly category stats from Yahoo and stores snapshots.

Strategy:
1. Get the correct stat_id → category_name mapping from the league's own settings.
2. Get team metadata (keys/names) from the scoreboard (proven working).
3. Fetch each team's weekly stats via /team/{key}/stats?type=week&week=N,
   using the league-specific stat_id map so values land in the right columns.
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

import httpx

from fantasai.models.scoring_grid import ScoringGridSnapshot

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_SEASON = 2026
_YAHOO_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

# Corrected fallback mapping (used when league settings fetch fails).
# Verified against Yahoo's individual /team/{key}/stats endpoint response.
_FALLBACK_STAT_ID_MAP: dict[str, str] = {
    "3":  "AVG",
    "4":  "OBP",
    "5":  "SLG",
    "6":  "BB",
    "7":  "R",
    "8":  "H",
    "12": "HR",
    "13": "RBI",
    "16": "SB",
    "17": "CS",
    "23": "IP",
    "24": "W",
    "25": "L",
    "26": "GS",
    "28": "SHO",
    "29": "SV",
    "31": "K",
    "32": "ERA",
    "33": "WHIP",
    "37": "K/9",
    "42": "HLD",
    "55": "H",
    "60": "OPS",
}


def _fetch_league_stat_id_map_xml(
    access_token: str,
    league_key: str,
) -> dict[str, str]:
    """Try to get stat_id map via the XML API (_yahoo_get), which is more reliable."""
    try:
        from fantasai.services.yahoo_oauth import _yahoo_get

        root = _yahoo_get(access_token, f"league/{league_key}/settings")
        stat_map: dict[str, str] = {}

        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag != "stat":
                continue
            stat_id = None
            display_name = None
            for child in elem:
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "stat_id" and child.text:
                    stat_id = child.text.strip()
                elif ctag == "display_name" and child.text:
                    display_name = child.text.strip()
            if stat_id and display_name:
                stat_map[stat_id] = display_name

        if stat_map:
            logger.info(
                "XML league stat map for %s: %d stats — %s", league_key, len(stat_map), stat_map
            )
        return stat_map
    except Exception as exc:
        logger.warning("XML league settings fetch failed for %s: %s", league_key, exc)
        return {}


def _fetch_league_stat_id_map(
    access_token: str,
    league_key: str,
) -> dict[str, str]:
    """Fetch stat_id → display_name from the league's settings.

    Returns a populated map on success, empty dict on any failure.
    The caller should fall back to _FALLBACK_STAT_ID_MAP when empty.
    """
    url = f"{_YAHOO_BASE}/league/{league_key}/settings"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params={"format": "json"}, headers=headers)
            if not resp.is_success:
                logger.warning(
                    "League settings HTTP %s for %s: %s",
                    resp.status_code, league_key, resp.text[:300],
                )
                return {}
            data = resp.json()
    except Exception as exc:
        logger.warning("League settings fetch failed for %s: %s", league_key, exc)
        return {}

    # Yahoo's settings JSON shape varies — try multiple paths
    stat_list: list = []
    try:
        league_data = data["fantasy_content"]["league"]
        settings_block = league_data[1]  # second element has settings content

        # Path A: settings_block["settings"][0]["stat_categories"]["stats"]["stat"]
        # Path B: settings_block["settings"]["stat_categories"]["stats"]["stat"]
        raw_settings = settings_block.get("settings")
        if isinstance(raw_settings, list) and raw_settings:
            raw_settings = raw_settings[0]

        if isinstance(raw_settings, dict):
            stat_cats = raw_settings.get("stat_categories", {}).get("stats", {})
            if isinstance(stat_cats, dict):
                stat_list = stat_cats.get("stat", [])
            elif isinstance(stat_cats, list):
                stat_list = stat_cats

        logger.info(
            "League settings raw keys for %s: settings type=%s stat_list len=%d",
            league_key, type(raw_settings).__name__, len(stat_list),
        )
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Could not parse league stat_categories for %s: %s", league_key, exc)
        return {}

    stat_map: dict[str, str] = {}
    for stat in (stat_list if isinstance(stat_list, list) else []):
        if not isinstance(stat, dict):
            continue
        stat_id = str(stat.get("stat_id", ""))
        display_name = (stat.get("display_name") or stat.get("name") or "").strip()
        if stat_id and display_name:
            stat_map[stat_id] = display_name

    if stat_map:
        logger.info("JSON league stat map for %s: %d stats — %s", league_key, len(stat_map), stat_map)
        return stat_map

    # JSON path failed — try XML
    return _fetch_league_stat_id_map_xml(access_token, league_key)


def _fetch_team_keys_from_scoreboard(
    access_token: str,
    league_key: str,
    week: Optional[int] = None,
) -> tuple[int, list[dict]]:
    """Use scoreboard to get team metadata (key, name, manager).

    Returns (week_num, teams_meta_list).
    """
    from fantasai.services.matchup_service import fetch_league_scoreboard

    scoreboard = fetch_league_scoreboard(access_token, league_key, week)
    if not scoreboard:
        return 0, []

    week_num = 0
    teams_meta: dict[str, dict] = {}
    for matchup in scoreboard:
        week_num = matchup.get("week") or week_num
        for prefix in (("team1", "manager1"), ("team2", "manager2")):
            tk = matchup.get(f"{prefix[0]}_key", "")
            if tk and tk not in teams_meta:
                teams_meta[tk] = {
                    "team_key": tk,
                    "team_name": matchup.get(f"{prefix[0]}_name", ""),
                    "manager_name": matchup.get(f"{prefix[1]}_name", ""),
                }

    return week_num, list(teams_meta.values())


def _fetch_one_team_weekly_stats(
    access_token: str,
    team_key: str,
    week: int,
    stat_id_map: dict[str, str],
) -> dict[str, float]:
    """Fetch a single team's actual weekly stats via /team/{key}/stats?type=week&week=N."""
    url = f"{_YAHOO_BASE}/team/{team_key}/stats"
    params = {"format": "json", "type": "week", "week": str(week)}
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params, headers=headers)
            if not resp.is_success:
                logger.warning(
                    "Yahoo team stats HTTP %s for team %s week %s: %s",
                    resp.status_code, team_key, week, resp.text[:300],
                )
                return {}
            data = resp.json()
    except Exception as exc:
        logger.warning("Yahoo team stats fetch failed for %s week %s: %s", team_key, week, exc)
        return {}

    try:
        team_data = data["fantasy_content"]["team"]
        stats_block = team_data[1].get("team_stats", {})
        stats_list = stats_block.get("stats", [])
        if isinstance(stats_list, dict):
            stats_list = stats_list.get("stat", [])
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Unexpected team stats shape for %s: %s", team_key, exc)
        return {}

    # Log raw stat_ids on first call so we can verify mapping
    raw = {
        str(s.get("stat", {}).get("stat_id", "")): str(s.get("stat", {}).get("value", ""))
        for s in (stats_list if isinstance(stats_list, list) else [])
        if isinstance(s, dict) and "stat" in s
    }
    logger.info("Raw stat_ids for team %s week %s: %s", team_key, week, raw)

    stats: dict[str, float] = {}
    for stat_entry in (stats_list if isinstance(stats_list, list) else []):
        if not isinstance(stat_entry, dict) or "stat" not in stat_entry:
            continue
        stat = stat_entry["stat"]
        stat_id = str(stat.get("stat_id", ""))
        value_str = str(stat.get("value", ""))
        if stat_id and value_str and value_str not in ("-", ""):
            cat = stat_id_map.get(stat_id)
            if cat:
                try:
                    stats[cat] = float(value_str)
                except (TypeError, ValueError):
                    pass

    return stats


def fetch_and_store_scoring_grid(
    db: "Session",
    league_key: str,
    access_token: str,
    week: Optional[int] = None,
) -> Optional[ScoringGridSnapshot]:
    """Fetch per-team weekly stats from Yahoo, upsert a ScoringGridSnapshot row."""

    # Phase 0: get the league's own stat_id → category name mapping
    stat_id_map = _fetch_league_stat_id_map(access_token, league_key)
    if not stat_id_map:
        logger.warning(
            "League settings fetch failed for %s; falling back to hardcoded map", league_key
        )
        stat_id_map = _FALLBACK_STAT_ID_MAP

    # Phase 1: get team metadata + week number from scoreboard
    week_num, teams_meta = _fetch_team_keys_from_scoreboard(
        access_token, league_key, week
    )

    if not teams_meta:
        logger.warning(
            "No team metadata from scoreboard for league %s week %s",
            league_key, week,
        )
        return None

    actual_week = week_num or week
    if not actual_week:
        logger.warning("Could not determine week number for league %s", league_key)
        return None

    # Phase 2: fetch each team's actual weekly stats using the league's stat map
    team_stats: dict[str, dict] = {}
    for tm in teams_meta:
        tk = tm["team_key"]
        stats = _fetch_one_team_weekly_stats(access_token, tk, actual_week, stat_id_map)
        team_stats[tk] = stats

    if not any(team_stats.values()):
        logger.warning(
            "All team stat fetches returned empty for league %s week %s",
            league_key, actual_week,
        )
        return None

    existing = (
        db.query(ScoringGridSnapshot)
        .filter(
            ScoringGridSnapshot.league_id == league_key,
            ScoringGridSnapshot.season == _SEASON,
            ScoringGridSnapshot.week == actual_week,
        )
        .first()
    )

    if existing:
        existing.team_stats = team_stats
        existing.teams_meta = teams_meta
        db.commit()
        db.refresh(existing)
        return existing

    snap = ScoringGridSnapshot(
        league_id=league_key,
        season=_SEASON,
        week=actual_week,
        team_stats=team_stats,
        teams_meta=teams_meta,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def get_scoring_grid_snapshot(
    db: "Session",
    league_key: str,
    week: int,
) -> Optional[ScoringGridSnapshot]:
    return (
        db.query(ScoringGridSnapshot)
        .filter(
            ScoringGridSnapshot.league_id == league_key,
            ScoringGridSnapshot.season == _SEASON,
            ScoringGridSnapshot.week == week,
        )
        .first()
    )


def get_max_stored_week(db: "Session", league_key: str) -> Optional[int]:
    from sqlalchemy import func
    return (
        db.query(func.max(ScoringGridSnapshot.week))
        .filter(
            ScoringGridSnapshot.league_id == league_key,
            ScoringGridSnapshot.season == _SEASON,
        )
        .scalar()
    )
