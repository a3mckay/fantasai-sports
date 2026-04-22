"""Scoring Grid service — fetches per-team weekly category stats from Yahoo and stores snapshots.

Uses Yahoo's JSON teams endpoint: /league/{key}/teams;out=stats;type=week[;week=N]?format=json
which returns all teams with their accumulated stats for the requested scoring period.
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


def _fetch_team_weekly_stats(
    access_token: str,
    league_key: str,
    week: Optional[int] = None,
) -> tuple[int, dict[str, dict], list[dict]]:
    """Fetch all teams' weekly stats from Yahoo's JSON teams endpoint.

    Returns (week_num, team_stats, teams_meta).
      team_stats  = {team_key: {category_name: float_value}}
      teams_meta  = [{team_key, team_name, manager_name}, ...]
    Returns (0, {}, []) on any failure so the caller can fall back.
    """
    from fantasai.services.matchup_service import _YAHOO_STAT_ID_TO_CAT

    path = f"league/{league_key}/teams;out=stats;type=week"
    if week is not None:
        path += f";week={week}"

    url = f"{_YAHOO_BASE}/{path}"
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, params={"format": "json"}, headers=headers)
            resp.raise_for_status()
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
            "Unexpected Yahoo teams JSON shape for %s: %s",
            league_key, exc,
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

        # team_list[0] = list of metadata dicts; team_list[1] = {"team_stats": {...}}
        meta_list = team_list[0] if len(team_list) > 0 else []
        stats_container = team_list[1] if len(team_list) > 1 else {}

        team_key = ""
        team_name = ""
        manager_name = ""

        for item in meta_list:
            if not isinstance(item, dict):
                continue
            if "team_key" in item:
                team_key = item["team_key"]
            if "name" in item:
                team_name = item["name"]
            if "managers" in item:
                managers = item["managers"]
                if isinstance(managers, list) and managers:
                    mgr = managers[0].get("manager", {}) if isinstance(managers[0], dict) else {}
                    manager_name = mgr.get("nickname", "")
                elif isinstance(managers, dict):
                    mgr_list = managers.get("0", {})
                    if isinstance(mgr_list, dict):
                        mgr = mgr_list.get("manager", {})
                        manager_name = mgr.get("nickname", "") if isinstance(mgr, dict) else ""

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

                # Yahoo returns stats as a list of {"stat": {"stat_id": N, "value": V}}
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
        "Yahoo teams JSON: league=%s week=%s teams=%d",
        league_key, week_num, len(teams_meta),
    )
    return week_num, team_stats, teams_meta


def fetch_and_store_scoring_grid(
    db: "Session",
    league_key: str,
    access_token: str,
    week: Optional[int] = None,
) -> Optional[ScoringGridSnapshot]:
    """Fetch per-team weekly stats from Yahoo, upsert a ScoringGridSnapshot row.

    Returns the stored snapshot, or None on failure.
    """
    actual_week, team_stats, teams_meta = _fetch_team_weekly_stats(
        access_token, league_key, week
    )

    if not actual_week or not team_stats:
        logger.warning(
            "No team-stats data from Yahoo for league %s week %s — "
            "endpoint returned week=%s teams=%d",
            league_key, week, actual_week, len(team_stats),
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
