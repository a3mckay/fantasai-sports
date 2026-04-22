"""Scoring Grid service — fetches per-team weekly category stats from Yahoo and stores snapshots.

Approach: reuse fetch_league_scoreboard() (proven to work) to get all teams' stats from
each matchup, then pivot into a per-team dict. Falls back to a direct teams;out=stats
JSON call if the scoreboard approach yields no data.
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


def _fetch_via_scoreboard(
    access_token: str,
    league_key: str,
    week: Optional[int] = None,
) -> tuple[int, dict[str, dict], list[dict]]:
    """Build per-team weekly stats by pivoting scoreboard matchup data.

    The scoreboard endpoint is proven to work. Each matchup contains both teams'
    accumulated stats for the week; we pivot from matchup-pairs to per-team dicts.
    """
    from fantasai.services.matchup_service import fetch_league_scoreboard

    scoreboard = fetch_league_scoreboard(access_token, league_key, week)
    if not scoreboard:
        logger.warning(
            "Scoreboard returned no matchups for league %s week %s",
            league_key, week,
        )
        return 0, {}, []

    week_num: int = 0
    team_stats: dict[str, dict] = {}
    teams_meta: dict[str, dict] = {}

    for matchup in scoreboard:
        week_num = matchup.get("week") or week_num
        t1_key = matchup.get("team1_key", "")
        t2_key = matchup.get("team2_key", "")
        if not t1_key or not t2_key:
            continue

        if t1_key not in teams_meta:
            teams_meta[t1_key] = {
                "team_key": t1_key,
                "team_name": matchup.get("team1_name", ""),
                "manager_name": matchup.get("manager1_name", ""),
            }
        if t2_key not in teams_meta:
            teams_meta[t2_key] = {
                "team_key": t2_key,
                "team_name": matchup.get("team2_name", ""),
                "manager_name": matchup.get("manager2_name", ""),
            }

        if t1_key not in team_stats:
            team_stats[t1_key] = {}
        if t2_key not in team_stats:
            team_stats[t2_key] = {}

        for cat, vals in (matchup.get("live_stats") or {}).items():
            if isinstance(vals, dict):
                if "team1" in vals:
                    team_stats[t1_key][cat] = vals["team1"]
                if "team2" in vals:
                    team_stats[t2_key][cat] = vals["team2"]

    logger.info(
        "Scoreboard pivot: league=%s week=%s teams=%d",
        league_key, week_num, len(teams_meta),
    )
    return week_num, team_stats, list(teams_meta.values())


def _fetch_via_teams_endpoint(
    access_token: str,
    league_key: str,
    week: Optional[int] = None,
) -> tuple[int, dict[str, dict], list[dict]]:
    """Fallback: fetch teams;out=stats directly from Yahoo JSON API."""
    from fantasai.services.matchup_service import _YAHOO_STAT_ID_TO_CAT

    url = f"{_YAHOO_BASE}/league/{league_key}/teams;out=stats"
    params: dict[str, str] = {"format": "json", "type": "week"}
    if week is not None:
        params["week"] = str(week)

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, params=params, headers=headers)
            if not resp.is_success:
                logger.warning(
                    "Yahoo teams endpoint HTTP %s for league %s week %s: %s",
                    resp.status_code, league_key, week, resp.text[:500],
                )
                return 0, {}, []
            data = resp.json()
    except Exception as exc:
        logger.warning(
            "Yahoo teams JSON fetch failed for league %s week %s: %s",
            league_key, week, exc,
        )
        return 0, {}, []

    try:
        league_data = data["fantasy_content"]["league"]
        teams_container = league_data[-1]["teams"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "Unexpected Yahoo teams JSON shape for %s: %s — keys=%s",
            league_key, exc,
            list(data.get("fantasy_content", {}).keys()) if isinstance(data, dict) else "?",
        )
        return 0, {}, []

    week_num: int = week or 0
    team_stats: dict[str, dict] = {}
    teams_meta: list[dict] = []
    seen_keys: set[str] = set()

    for key, value in teams_container.items():
        if not key.isdigit():
            continue

        team_list = value.get("team", []) if isinstance(value, dict) else []
        if not team_list or not isinstance(team_list, list):
            continue

        meta_list = team_list[0] if len(team_list) > 0 else []
        stats_container = team_list[1] if len(team_list) > 1 else {}

        team_key = ""
        team_name = ""
        manager_name = ""

        for item in (meta_list if isinstance(meta_list, list) else []):
            if not isinstance(item, dict):
                continue
            if "team_key" in item:
                team_key = item["team_key"]
            if "name" in item:
                team_name = item["name"]
            if "managers" in item:
                managers = item["managers"]
                if isinstance(managers, list) and managers:
                    mgr = managers[0]
                    if isinstance(mgr, dict) and "manager" in mgr:
                        manager_name = mgr["manager"].get("nickname", "") or ""

        if not team_key or team_key in seen_keys:
            continue

        stats: dict[str, float] = {}

        if isinstance(stats_container, dict):
            team_stats_raw = stats_container.get("team_stats", {})
            if isinstance(team_stats_raw, dict):
                w_str = team_stats_raw.get("week")
                if w_str:
                    try:
                        wn = int(w_str)
                        if wn > 0:
                            week_num = wn
                    except (TypeError, ValueError):
                        pass

                stats_list = team_stats_raw.get("stats", [])
                if isinstance(stats_list, list):
                    for stat_entry in stats_list:
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

        seen_keys.add(team_key)
        teams_meta.append({
            "team_key": team_key,
            "team_name": team_name,
            "manager_name": manager_name,
        })
        team_stats[team_key] = stats

    logger.info(
        "Yahoo teams endpoint: league=%s week=%s teams=%d",
        league_key, week_num, len(teams_meta),
    )
    return week_num, team_stats, teams_meta


def fetch_and_store_scoring_grid(
    db: "Session",
    league_key: str,
    access_token: str,
    week: Optional[int] = None,
) -> Optional[ScoringGridSnapshot]:
    """Fetch per-team weekly stats from Yahoo, upsert a ScoringGridSnapshot row."""
    actual_week, team_stats, teams_meta = _fetch_via_scoreboard(
        access_token, league_key, week
    )

    if not actual_week or not team_stats:
        logger.info("Scoreboard approach yielded no data, trying teams endpoint")
        actual_week, team_stats, teams_meta = _fetch_via_teams_endpoint(
            access_token, league_key, week
        )

    if not actual_week or not team_stats:
        logger.warning(
            "Both approaches failed for league %s week %s",
            league_key, week,
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
