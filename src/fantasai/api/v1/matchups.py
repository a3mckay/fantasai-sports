"""Matchup Analyzer API — weekly H2H matchup projections and narratives."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from fantasai.api.deps import get_current_user, get_db
from fantasai.models.matchup import MatchupAnalysis
from fantasai.models.user import User, YahooConnection

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/matchups", tags=["matchups"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CategoryProjection(BaseModel):
    team1: float
    team2: float
    edge: str  # "team1" | "team2" | "toss_up"


class MatchupAnalysisRead(BaseModel):
    id: int
    league_id: str
    season: int
    week: int
    team1_key: str
    team2_key: str
    team1_name: str
    team2_name: str
    manager1_name: Optional[str]
    manager2_name: Optional[str]
    category_projections: dict[str, CategoryProjection]
    live_stats: Optional[dict]
    narrative: Optional[str]
    suggestions: list
    generated_at: Optional[str]

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_read(ma: MatchupAnalysis) -> MatchupAnalysisRead:
    """Convert a MatchupAnalysis ORM row to MatchupAnalysisRead."""
    raw_proj: dict = ma.category_projections or {}
    parsed: dict[str, CategoryProjection] = {}
    for cat, vals in raw_proj.items():
        if isinstance(vals, dict):
            parsed[cat] = CategoryProjection(
                team1=float(vals.get("team1", 0.0)),
                team2=float(vals.get("team2", 0.0)),
                edge=str(vals.get("edge", "toss_up")),
            )

    generated_at_str: Optional[str] = None
    if ma.generated_at is not None:
        generated_at_str = ma.generated_at.isoformat()

    return MatchupAnalysisRead(
        id=ma.id,
        league_id=ma.league_id,
        season=ma.season,
        week=ma.week,
        team1_key=ma.team1_key,
        team2_key=ma.team2_key,
        team1_name=ma.team1_name,
        team2_name=ma.team2_name,
        manager1_name=ma.manager1_name,
        manager2_name=ma.manager2_name,
        category_projections=parsed,
        live_stats=ma.live_stats,
        narrative=ma.narrative,
        suggestions=list(ma.suggestions or []),
        generated_at=generated_at_str,
    )


def _current_week(db: Session, league_id: str) -> int:
    """Return the most recent week stored for this league, or 1 if none."""
    from sqlalchemy import func

    result = (
        db.query(func.max(MatchupAnalysis.week))
        .filter(MatchupAnalysis.league_id == league_id)
        .scalar()
    )
    return result if result is not None else 1


def _get_league_id(user: User, db: Session) -> Optional[str]:
    """Return the user's active league_key from their YahooConnection, or None."""
    conn: Optional[YahooConnection] = (
        db.query(YahooConnection)
        .filter(YahooConnection.user_id == user.id)
        .first()
    )
    if conn and conn.league_key:
        return conn.league_key
    return None


def _run_refresh(league_id: str) -> None:
    """Background task: run the matchup analysis service for this league."""
    try:
        from fantasai.config import settings
        from fantasai.database import SessionLocal
        from fantasai.models.league import League
        from fantasai.models.user import YahooConnection
        from fantasai.services.matchup_service import analyze_league_matchups
        from fantasai.services.yahoo_sync import get_valid_access_token

        db = SessionLocal()
        try:
            league = db.query(League).filter(League.league_id == league_id).first()
            if not league:
                _log.warning("Matchup refresh: league %s not found", league_id)
                return

            conn = (
                db.query(YahooConnection)
                .filter(YahooConnection.league_key == league_id)
                .first()
            )
            if not conn:
                _log.warning("Matchup refresh: no YahooConnection for league %s", league_id)
                return

            access_token = get_valid_access_token(conn, db)
            analyze_league_matchups(
                db=db,
                league=league,
                access_token=access_token,
                anthropic_api_key=settings.anthropic_api_key,
            )
        finally:
            db.close()
    except Exception:
        _log.error(
            "Matchup analysis failed for league %s", league_id, exc_info=True
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[MatchupAnalysisRead])
def list_matchups(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[MatchupAnalysisRead]:
    """List current-week matchup analyses for the user's active league.

    Returns an empty list if no analysis exists yet — the client should
    prompt the user to click Refresh to generate one.
    """
    league_id = _get_league_id(user, db)
    if not league_id:
        return []

    week = _current_week(db, league_id)

    rows = (
        db.query(MatchupAnalysis)
        .filter(
            MatchupAnalysis.league_id == league_id,
            MatchupAnalysis.week == week,
        )
        .order_by(MatchupAnalysis.id)
        .all()
    )

    return [_to_read(row) for row in rows]


@router.post("/refresh", status_code=202)
def refresh_matchups(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Kick off a background matchup analysis for the user's active league.

    Returns 202 Accepted immediately; analysis runs in the background.
    """
    league_id = _get_league_id(user, db)
    if not league_id:
        return {
            "status": "no_league",
            "message": "No active league found. Connect Yahoo Fantasy first.",
        }

    background_tasks.add_task(_run_refresh, league_id)

    return {
        "status": "refreshing",
        "message": "Matchup analysis started in background",
    }
