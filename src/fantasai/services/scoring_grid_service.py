"""Scoring Grid service — fetches per-team weekly category stats from Yahoo and stores snapshots."""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

from fantasai.models.scoring_grid import ScoringGridSnapshot
from fantasai.services.matchup_service import fetch_league_scoreboard

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_SEASON = 2026


def _build_team_stats(matchups: list[dict]) -> tuple[int, dict, list]:
    """Extract per-team stats from parsed matchup dicts.

    Returns (week_num, team_stats, teams_meta) where:
      team_stats  = {team_key: {category: value}}
      teams_meta  = [{team_key, team_name, manager_name}]
    """
    team_stats: dict[str, dict[str, float]] = {}
    teams_meta: dict[str, dict] = {}
    week_num = 0

    for matchup in matchups:
        t1_key = matchup.get("team1_key", "")
        t2_key = matchup.get("team2_key", "")
        week_num = matchup.get("week", week_num)

        for key, name, mgr in [
            (t1_key, matchup.get("team1_name", ""), matchup.get("manager1_name", "")),
            (t2_key, matchup.get("team2_name", ""), matchup.get("manager2_name", "")),
        ]:
            if not key:
                continue
            if key not in teams_meta:
                teams_meta[key] = {"team_key": key, "team_name": name, "manager_name": mgr or ""}
            if key not in team_stats:
                team_stats[key] = {}

        for cat, vals in matchup.get("live_stats", {}).items():
            if t1_key and "team1" in vals:
                team_stats.setdefault(t1_key, {})[cat] = vals["team1"]
            if t2_key and "team2" in vals:
                team_stats.setdefault(t2_key, {})[cat] = vals["team2"]

    return week_num, team_stats, list(teams_meta.values())


def fetch_and_store_scoring_grid(
    db: "Session",
    league_key: str,
    access_token: str,
    week: Optional[int] = None,
) -> Optional[ScoringGridSnapshot]:
    """Fetch Yahoo scoreboard for a week, extract per-team stats, upsert snapshot.

    If week is None, fetches the current week.
    Returns the stored/updated ScoringGridSnapshot, or None on failure.
    """
    matchups = fetch_league_scoreboard(access_token, league_key, week)
    if not matchups:
        logger.warning("No matchup data from Yahoo for league %s week %s", league_key, week)
        return None

    actual_week, team_stats, teams_meta = _build_team_stats(matchups)
    if not actual_week:
        logger.warning("Could not determine week number from Yahoo scoreboard for league %s", league_key)
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
