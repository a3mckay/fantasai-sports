"""Scoring Grid service — fetches per-team weekly category stats from Yahoo and stores snapshots.

Strategy:
1. Get team metadata (keys/names) from the scoreboard (proven working).
2. Fetch each team's ACTUAL weekly stats via /team/{key}/stats?type=week&week=N&format=json,
   which returns the correct accumulated stats for the scoring period.

The scoreboard's team_stats field appears to return season-to-date or some aggregate,
not weekly stats. Individual team stat calls are the reliable alternative.
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
) -> dict[str, float]:
    """Fetch a single team's actual weekly stats via /team/{key}/stats?type=week&week=N."""
    from fantasai.services.matchup_service import _YAHOO_STAT_ID_TO_CAT

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
        # team_data = [{...metadata...}, {"team_stats": {...}}]
        stats_block = team_data[1].get("team_stats", {})
        stats_list = stats_block.get("stats", [])
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Unexpected team stats shape for %s: %s", team_key, exc)
        return {}

    stats: dict[str, float] = {}
    for stat_entry in (stats_list if isinstance(stats_list, list) else []):
        if not isinstance(stat_entry, dict) or "stat" not in stat_entry:
            continue
        stat = stat_entry["stat"]
        stat_id = str(stat.get("stat_id", ""))
        value_str = str(stat.get("value", ""))
        if stat_id and value_str and value_str not in ("-", ""):
            cat = _YAHOO_STAT_ID_TO_CAT.get(stat_id)
            if cat:
                try:
                    stats[cat] = float(value_str)
                except (TypeError, ValueError):
                    pass

    # ERA/WHIP are undefined when no innings pitched — strip to avoid 0.00 mislead
    ip = stats.get("IP")
    if ip is None or ip == 0:
        stats.pop("ERA", None)
        stats.pop("WHIP", None)

    return stats


def fetch_and_store_scoring_grid(
    db: "Session",
    league_key: str,
    access_token: str,
    week: Optional[int] = None,
) -> Optional[ScoringGridSnapshot]:
    """Fetch per-team weekly stats from Yahoo, upsert a ScoringGridSnapshot row."""

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

    # Phase 2: fetch each team's actual weekly stats individually
    team_stats: dict[str, dict] = {}
    for tm in teams_meta:
        tk = tm["team_key"]
        stats = _fetch_one_team_weekly_stats(access_token, tk, actual_week)
        team_stats[tk] = stats
        logger.debug("Team %s week %s stats: %s", tk, actual_week, stats)

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
