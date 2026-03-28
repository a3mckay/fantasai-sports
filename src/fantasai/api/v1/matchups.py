"""Matchup Analyzer API — weekly H2H matchup projections and narratives."""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx
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
    is_user_matchup: bool = False

    model_config = ConfigDict(from_attributes=True)


class PowerRankingEntry(BaseModel):
    rank: int
    team_key: str
    team_name: str
    wins: int
    losses: int
    ties: int
    win_pct: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_read(
    ma: MatchupAnalysis,
    user_team_key: Optional[str] = None,
) -> MatchupAnalysisRead:
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

    is_user_matchup = bool(
        user_team_key
        and (ma.team1_key == user_team_key or ma.team2_key == user_team_key)
    )

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
        is_user_matchup=is_user_matchup,
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

    # Look up the user's own team key so we can flag their matchup
    conn: Optional[YahooConnection] = (
        db.query(YahooConnection)
        .filter(YahooConnection.user_id == user.id)
        .first()
    )
    user_team_key: Optional[str] = conn.team_key if conn else None

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

    return [_to_read(row, user_team_key=user_team_key) for row in rows]


@router.get("/debug/raw-scoreboard")
def debug_raw_scoreboard(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return the raw Yahoo scoreboard JSON and our parsed result.

    Use this to debug why fetch_league_scoreboard returns empty.
    """
    from fantasai.services.matchup_service import fetch_league_scoreboard
    from fantasai.services.yahoo_sync import get_valid_access_token

    league_id = _get_league_id(user, db)
    if not league_id:
        return {"error": "no_league", "league_id": None}

    conn: Optional[YahooConnection] = (
        db.query(YahooConnection)
        .filter(YahooConnection.league_key == league_id)
        .first()
    )
    if not conn:
        return {"error": "no_yahoo_connection", "league_id": league_id}

    # Fetch raw JSON directly so we can return it unmodified
    try:
        access_token = get_valid_access_token(conn, db)
    except Exception as exc:
        return {"error": f"token_error: {exc}", "league_id": league_id}

    url = f"https://fantasysports.yahooapis.com/fantasy/v2/league/{league_id}/scoreboard"
    raw_response: Any = None
    http_status: int = 0
    http_error: Optional[str] = None

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                url,
                params={"format": "json"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            http_status = resp.status_code
            try:
                raw_response = resp.json()
            except Exception:
                raw_response = resp.text
    except Exception as exc:
        http_error = str(exc)

    # Also run the parser to see what it produces
    parsed_matchups: list[dict] = []
    parse_error: Optional[str] = None
    if raw_response and not http_error:
        try:
            parsed_matchups = fetch_league_scoreboard(
                access_token=access_token,
                league_key=league_id,
            )
        except Exception as exc:
            parse_error = str(exc)

    return {
        "league_id": league_id,
        "yahoo_url": url,
        "http_status": http_status,
        "http_error": http_error,
        "raw_response": raw_response,
        "parsed_matchup_count": len(parsed_matchups),
        "parsed_matchups": parsed_matchups,
        "parse_error": parse_error,
    }


@router.get("/power-rankings", response_model=list[PowerRankingEntry])
def power_rankings(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[PowerRankingEntry]:
    """Simulate each team vs every other team this week. Returns teams sorted by win%."""
    from fantasai.services.matchup_service import compare_category_projections

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
        .all()
    )
    if not rows:
        return []

    # Collect category list from the first row that has projection data
    categories: list[str] = []
    for row in rows:
        if row.category_projections:
            categories = list(row.category_projections.keys())
            break

    # Build {team_key: {category: projected_value}} from stored category_projections.
    # category_projections is {"HR": {"team1": x, "team2": y, "edge": "..."}, ...}
    team_stats: dict[str, dict[str, float]] = {}
    team_names: dict[str, str] = {}

    for row in rows:
        cat_proj = row.category_projections or {}
        t1_stats = {
            cat: float(vals.get("team1", 0.0))
            for cat, vals in cat_proj.items()
            if isinstance(vals, dict)
        }
        t2_stats = {
            cat: float(vals.get("team2", 0.0))
            for cat, vals in cat_proj.items()
            if isinstance(vals, dict)
        }
        if row.team1_key not in team_stats:
            team_stats[row.team1_key] = t1_stats
            team_names[row.team1_key] = row.team1_name or row.team1_key
        if row.team2_key not in team_stats:
            team_stats[row.team2_key] = t2_stats
            team_names[row.team2_key] = row.team2_name or row.team2_key

    all_keys = list(team_stats.keys())
    record: dict[str, dict] = {k: {"wins": 0, "losses": 0, "ties": 0} for k in all_keys}

    for i, tk_a in enumerate(all_keys):
        for tk_b in all_keys[i + 1:]:
            comparison = compare_category_projections(
                team_stats[tk_a], team_stats[tk_b], categories
            )
            a_wins = sum(1 for v in comparison.values() if v["edge"] == "team1")
            b_wins = sum(1 for v in comparison.values() if v["edge"] == "team2")
            if a_wins > b_wins:
                record[tk_a]["wins"] += 1
                record[tk_b]["losses"] += 1
            elif b_wins > a_wins:
                record[tk_b]["wins"] += 1
                record[tk_a]["losses"] += 1
            else:
                record[tk_a]["ties"] += 1
                record[tk_b]["ties"] += 1

    entries: list[PowerRankingEntry] = []
    for tk, rec in record.items():
        total = rec["wins"] + rec["losses"] + rec["ties"]
        win_pct = (rec["wins"] + 0.5 * rec["ties"]) / total if total > 0 else 0.0
        entries.append(PowerRankingEntry(
            rank=0,
            team_key=tk,
            team_name=team_names.get(tk, tk),
            wins=rec["wins"],
            losses=rec["losses"],
            ties=rec["ties"],
            win_pct=round(win_pct, 3),
        ))

    entries.sort(key=lambda e: (-e.win_pct, -e.wins, e.team_name))
    for i, entry in enumerate(entries, start=1):
        entry.rank = i

    return entries


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
