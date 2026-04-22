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
    if snap is None and week is not None:
        snap = get_scoring_grid_snapshot(db, league_key, week)

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


@router.get("/debug-raw")
def debug_raw_yahoo_response(
    week: Optional[int] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Debug: return raw Yahoo JSON for the teams;out=stats endpoint."""
    import httpx as _httpx
    conn, access_token = _get_conn_and_token(user, db)
    path = f"league/{conn.league_key}/teams;out=stats;type=week"
    if week is not None:
        path += f";week={week}"
    url = f"https://fantasysports.yahooapis.com/fantasy/v2/{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        with _httpx.Client(timeout=20.0) as client:
            resp = client.get(url, params={"format": "json"}, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


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
