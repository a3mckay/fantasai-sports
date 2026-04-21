"""Scoring Grid service — fetches per-team weekly category stats from Yahoo and stores snapshots.

Primary approach: Yahoo's /league/{key}/teams/stats;type=week;week=N XML endpoint, which
returns all teams' actual accumulated stats for the requested scoring period.

The scoreboard endpoint (used by the matchup analyzer) is NOT used here because it returns
stats keyed as "team1"/"team2" within each matchup pair and silently drops any stat whose
Yahoo value string is "-" (not yet accumulated).  The team-stats endpoint avoids both issues.
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from fantasai.models.scoring_grid import ScoringGridSnapshot

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_SEASON = 2026


def _local_tag(elem) -> str:
    t = elem.tag
    return t.split("}")[-1] if "}" in t else t


def _fetch_team_weekly_stats_xml(
    access_token: str,
    league_key: str,
    week: Optional[int] = None,
) -> tuple[int, dict[str, dict], list[dict]]:
    """Fetch all teams' weekly stats from Yahoo's XML team-stats endpoint.

    Uses:  /league/{key}/teams/stats;type=week[;week=N]

    Returns (week_num, team_stats, teams_meta).
      team_stats  = {team_key: {category_name: float_value}}
      teams_meta  = [{team_key, team_name, manager_name}, ...]
    Returns (0, {}, []) on any failure so the caller can fall back.
    """
    from fantasai.services.matchup_service import _YAHOO_STAT_ID_TO_CAT
    from fantasai.services.yahoo_oauth import _yahoo_get

    path = f"league/{league_key}/teams/stats;type=week"
    if week is not None:
        path += f";week={week}"

    try:
        root = _yahoo_get(access_token, path)
    except Exception as exc:
        logger.warning(
            "Yahoo team-stats XML fetch failed for league %s week %s: %s",
            league_key, week, exc,
        )
        return 0, {}, []

    week_num: int = week or 0
    team_stats: dict[str, dict] = {}
    teams_meta: list[dict] = []
    seen_keys: set[str] = set()

    for team_elem in root.iter():
        if _local_tag(team_elem) != "team":
            continue

        team_key = ""
        team_name = ""
        manager_name = ""
        stats: dict[str, float] = {}

        for child in team_elem:
            ctag = _local_tag(child)

            if ctag == "team_key" and child.text:
                team_key = child.text.strip()

            elif ctag == "name" and child.text:
                team_name = child.text.strip()

            elif ctag == "managers":
                for mgr in child.iter():
                    if _local_tag(mgr) == "nickname" and mgr.text:
                        manager_name = mgr.text.strip()
                        break

            elif ctag == "team_stats":
                for ts_child in child:
                    ts_tag = _local_tag(ts_child)

                    if ts_tag == "week" and ts_child.text:
                        try:
                            wn = int(ts_child.text.strip())
                            if wn > 0:
                                week_num = wn
                        except ValueError:
                            pass

                    elif ts_tag == "stats":
                        for stat_elem in ts_child:
                            if _local_tag(stat_elem) != "stat":
                                continue
                            stat_id_str: Optional[str] = None
                            value_str: Optional[str] = None
                            for s in stat_elem:
                                s_tag = _local_tag(s)
                                if s_tag == "stat_id" and s.text:
                                    stat_id_str = s.text.strip()
                                elif s_tag == "value" and s.text:
                                    value_str = s.text.strip()
                            if stat_id_str and value_str and value_str not in ("-", ""):
                                cat = _YAHOO_STAT_ID_TO_CAT.get(stat_id_str)
                                if cat:
                                    try:
                                        stats[cat] = float(value_str)
                                    except (TypeError, ValueError):
                                        pass

        if team_key and team_key not in seen_keys:
            seen_keys.add(team_key)
            teams_meta.append({
                "team_key": team_key,
                "team_name": team_name,
                "manager_name": manager_name,
            })
            team_stats[team_key] = stats

    logger.info(
        "Yahoo team-stats XML: league=%s week=%s teams=%d",
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
    actual_week, team_stats, teams_meta = _fetch_team_weekly_stats_xml(
        access_token, league_key, week
    )

    if not actual_week or not team_stats:
        logger.warning(
            "No team-stats data from Yahoo for league %s week %s — "
            "XML endpoint returned week=%s teams=%d",
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
