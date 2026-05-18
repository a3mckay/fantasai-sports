"""League Scoring Grid API — per-team weekly category stats for H2H grid view."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasai.api.deps import get_current_user, get_db
from fantasai.models.user import User, YahooConnection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scoring-grid", tags=["scoring-grid"])

_LOWER_IS_BETTER = ["ERA", "WHIP"]


def _get_conn_and_token(user: User, db: Session):
    from fantasai.services.yahoo_sync import get_valid_access_token

    conn: Optional[YahooConnection] = (
        db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    )
    if not conn or not conn.league_key:
        raise HTTPException(status_code=404, detail="No Yahoo league connected")
    access_token = get_valid_access_token(conn, db)
    return conn, access_token


@router.get("")
def get_scoring_grid(
    week: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return per-team category stats for a given week (defaults to current week).

    available_weeks is always 1..current_week so the frontend can show the full
    navigation range even if some historical weeks haven't been fetched yet.
    """
    from fantasai.models.league import League, Team
    from fantasai.services.scoring_grid_service import (
        fetch_and_store_scoring_grid,
        get_max_stored_week,
        get_scoring_grid_snapshot,
    )

    conn, access_token = _get_conn_and_token(user, db)
    league_key = conn.league_key

    # Always fetch live from Yahoo and update the cache — ensures fresh stats
    # even when navigating to a previously-stored week.  If Yahoo fails, fall
    # back to whatever we have in the DB.
    snap = fetch_and_store_scoring_grid(db, league_key, access_token, week=week)
    if snap is None:
        # Fall back to DB cache regardless of whether week was specified
        fallback_week = week or (get_max_stored_week(db, league_key))
        if fallback_week:
            snap = get_scoring_grid_snapshot(db, league_key, fallback_week)

    if snap is None:
        raise HTTPException(status_code=503, detail="Could not fetch scoring data from Yahoo")

    # available_weeks = 1..current_week (current = max week we've ever stored)
    max_week = get_max_stored_week(db, league_key) or snap.week
    current_week = max(max_week, snap.week)
    available_weeks = list(range(1, current_week + 1))

    league = db.get(League, league_key)
    categories = league.scoring_categories if league else []

    my_team = (
        db.query(Team)
        .filter(Team.league_id == league_key, Team.owner_user_id == user.id)
        .first()
    )
    my_team_key = my_team.yahoo_team_key if my_team else None

    teams = [
        {**t, "is_mine": t.get("team_key") == my_team_key}
        for t in (snap.teams_meta or [])
    ]

    return {
        "week": snap.week,
        "current_week": current_week,
        "available_weeks": available_weeks,
        "categories": categories,
        "lower_is_better": _LOWER_IS_BETTER,
        "teams": teams,
        "team_stats": snap.team_stats or {},
        "my_team_key": my_team_key,
    }


def _run_refresh(league_key: str, week: Optional[int]) -> None:
    try:
        from fantasai.database import SessionLocal
        from fantasai.models.user import YahooConnection
        from fantasai.services.scoring_grid_service import fetch_and_store_scoring_grid
        from fantasai.services.yahoo_sync import get_valid_access_token

        db = SessionLocal()
        try:
            conn = db.query(YahooConnection).filter(YahooConnection.league_key == league_key).first()
            if not conn:
                return
            access_token = get_valid_access_token(conn, db)
            fetch_and_store_scoring_grid(db, league_key, access_token, week=week)
        finally:
            db.close()
    except Exception:
        logger.warning("Background scoring grid refresh failed for %s", league_key, exc_info=True)


@router.get("/debug-scoreboard")
def debug_scoreboard(
    week: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Debug: return parsed scoreboard data and pivoted team stats."""
    from fantasai.services.matchup_service import fetch_league_scoreboard
    conn, access_token = _get_conn_and_token(user, db)
    scoreboard = fetch_league_scoreboard(access_token, conn.league_key, week)
    # Build per-team stats pivot (same logic as _fetch_via_scoreboard)
    team_stats: dict = {}
    for m in scoreboard:
        t1, t2 = m.get("team1_key", ""), m.get("team2_key", "")
        if t1 not in team_stats:
            team_stats[t1] = {}
        if t2 not in team_stats:
            team_stats[t2] = {}
        for cat, vals in (m.get("live_stats") or {}).items():
            if isinstance(vals, dict):
                if "team1" in vals:
                    team_stats[t1][cat] = vals["team1"]
                if "team2" in vals:
                    team_stats[t2][cat] = vals["team2"]
    return {"scoreboard": scoreboard, "team_stats_pivot": team_stats}


@router.get("/debug-raw")
def debug_raw_yahoo_response(
    week: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Debug: show league stat_id map + raw team stats for first team."""
    import httpx as _httpx
    from fantasai.services.scoring_grid_service import _fetch_league_stat_id_map
    conn, access_token = _get_conn_and_token(user, db)

    stat_map = _fetch_league_stat_id_map(access_token, conn.league_key)

    # Also fetch raw team stats for the user's own team if available
    from fantasai.models.league import Team
    my_team = (
        db.query(Team)
        .filter(Team.league_id == conn.league_key, Team.owner_user_id == user.id)
        .first()
    )
    raw_team_stats = None
    if my_team and my_team.yahoo_team_key:
        url = f"https://fantasysports.yahooapis.com/fantasy/v2/team/{my_team.yahoo_team_key}/stats"
        params: dict = {"format": "json", "type": "week"}
        if week is not None:
            params["week"] = str(week)
        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            with _httpx.Client(timeout=15.0) as client:
                resp = client.get(url, params=params, headers=headers)
                raw_team_stats = resp.json() if resp.is_success else {"error": resp.text[:500]}
        except Exception as exc:
            raw_team_stats = {"error": str(exc)}

    return {
        "league_stat_id_map": stat_map,
        "my_team_key": my_team.yahoo_team_key if my_team else None,
        "my_team_raw_stats": raw_team_stats,
    }


@router.get("/season-record")
def get_season_record(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return cumulative all-play category W-L-T standings.

    For every stored week, every team is compared against every other team
    across all scoring categories (all-play format), summed across all weeks.
    This matches the visual scoring grid's H2H logic and produces standings
    where schedule luck is eliminated — only raw category performance counts.

    Categories that are display-only (H/AB, Batting, Pitching, H) are excluded.
    """
    from fantasai.models.league import League, Team
    from fantasai.models.scoring_grid import ScoringGridSnapshot
    from fantasai.services.scoring_grid_service import _SEASON

    conn, _ = _get_conn_and_token(user, db)
    league_key = conn.league_key

    league = db.get(League, league_key)
    categories = league.scoring_categories if league else []
    lower_is_better = set(_LOWER_IS_BETTER)

    # Exclude display-only / non-scoring categories (mirrors frontend HIDE_CATS)
    _IGNORE_CATS = {"H/AB", "Batting", "Pitching", "H"}
    active_cats = [c for c in categories if c not in _IGNORE_CATS]

    my_team = (
        db.query(Team)
        .filter(Team.league_id == league_key, Team.owner_user_id == user.id)
        .first()
    )
    my_team_key = my_team.yahoo_team_key if my_team else None

    # All stored weekly snapshots for this league, oldest first
    snapshots = (
        db.query(ScoringGridSnapshot)
        .filter(
            ScoringGridSnapshot.league_id == league_key,
            ScoringGridSnapshot.season == _SEASON,
        )
        .order_by(ScoringGridSnapshot.week)
        .all()
    )

    # Seed records from team metadata across all snapshots
    records: dict[str, dict] = {}
    for snap in snapshots:
        for tm in snap.teams_meta or []:
            tk = tm.get("team_key")
            if tk and tk not in records:
                records[tk] = {
                    "team_key": tk,
                    "team_name": tm.get("team_name", tk),
                    "manager_name": tm.get("manager_name"),
                    "wins": 0, "losses": 0, "ties": 0,
                    "is_mine": tk == my_team_key,
                }

    # All-play: compare every team against every other team for each week
    weeks_counted = []
    for snap in snapshots:
        team_stats = snap.team_stats or {}
        team_keys = [k for k in team_stats if k in records]
        if len(team_keys) < 2:
            continue
        weeks_counted.append(snap.week)

        for i, t1_key in enumerate(team_keys):
            for t2_key in team_keys[i + 1:]:
                t1_stats = team_stats.get(t1_key, {})
                t2_stats = team_stats.get(t2_key, {})
                if not t1_stats or not t2_stats:
                    continue

                for cat in active_cats:
                    v1 = t1_stats.get(cat)
                    v2 = t2_stats.get(cat)
                    if v1 is None or v2 is None:
                        continue
                    invert = cat in lower_is_better
                    if v1 == v2:
                        records[t1_key]["ties"] += 1
                        records[t2_key]["ties"] += 1
                    elif (v1 > v2) != invert:
                        records[t1_key]["wins"] += 1
                        records[t2_key]["losses"] += 1
                    else:
                        records[t2_key]["wins"] += 1
                        records[t1_key]["losses"] += 1

    # Sort: most wins first, then fewest losses
    standings = sorted(records.values(), key=lambda r: (-r["wins"], r["losses"]))
    for i, row in enumerate(standings, 1):
        row["rank"] = i
        total = row["wins"] + row["losses"] + row["ties"]
        row["win_pct"] = round(row["wins"] / total, 3) if total else 0.0

    return {
        "standings": standings,
        "categories_count": len(active_cats),
        "weeks_counted": weeks_counted,
        "my_team_key": my_team_key,
    }


@router.post("/refresh")
def refresh_scoring_grid(
    week: Optional[int] = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conn, _ = _get_conn_and_token(user, db)
    background_tasks.add_task(_run_refresh, conn.league_key, week)
    return {"status": "refresh queued"}
