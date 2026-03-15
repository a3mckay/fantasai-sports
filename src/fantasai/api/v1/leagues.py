"""League and team management API endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.models.league import League, Team
from fantasai.schemas.league import LeagueCreate, LeagueRead, TeamCreate, TeamRead

router = APIRouter(prefix="/leagues", tags=["leagues"])


# ---------------------------------------------------------------------------
# Leagues
# ---------------------------------------------------------------------------


@router.post("", response_model=LeagueRead, status_code=201)
def create_league(payload: LeagueCreate, db: Session = Depends(get_db)) -> League:
    """Create a new league.

    Idempotent on league_id — re-POSTing an existing league_id returns 409.
    """
    existing = db.get(League, payload.league_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"League '{payload.league_id}' already exists",
        )

    league = League(
        league_id=payload.league_id,
        platform=payload.platform,
        sport=payload.sport,
        scoring_categories=payload.scoring_categories,
        league_type=payload.league_type,
        settings=payload.settings,
        roster_positions=payload.roster_positions,
    )
    db.add(league)
    db.commit()
    db.refresh(league)
    return league


@router.get("", response_model=list[LeagueRead])
def list_leagues(db: Session = Depends(get_db)) -> list[League]:
    """List all leagues."""
    return db.query(League).order_by(League.league_id).all()


@router.get("/{league_id}", response_model=LeagueRead)
def get_league(league_id: str, db: Session = Depends(get_db)) -> League:
    """Get a league by ID."""
    league = db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail=f"League '{league_id}' not found")
    return league


@router.put("/{league_id}", response_model=LeagueRead)
def update_league(
    league_id: str, payload: LeagueCreate, db: Session = Depends(get_db)
) -> League:
    """Update an existing league's configuration."""
    league = db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail=f"League '{league_id}' not found")

    league.platform = payload.platform
    league.sport = payload.sport
    league.scoring_categories = payload.scoring_categories
    league.league_type = payload.league_type
    league.settings = payload.settings
    league.roster_positions = payload.roster_positions
    db.commit()
    db.refresh(league)
    return league


# ---------------------------------------------------------------------------
# Teams (nested under league)
# ---------------------------------------------------------------------------


@router.post("/{league_id}/teams", response_model=TeamRead, status_code=201)
def create_team(
    league_id: str, payload: TeamCreate, db: Session = Depends(get_db)
) -> Team:
    """Create a new team in a league."""
    league = db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail=f"League '{league_id}' not found")

    team = Team(
        league_id=league_id,
        manager_name=payload.manager_name,
        roster=payload.roster,
    )
    db.add(team)
    db.commit()
    db.refresh(team)
    return team


@router.get("/{league_id}/teams", response_model=list[TeamRead])
def list_teams(league_id: str, db: Session = Depends(get_db)) -> list[Team]:
    """List all teams in a league."""
    league = db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail=f"League '{league_id}' not found")
    return db.query(Team).filter(Team.league_id == league_id).all()


@router.get("/{league_id}/teams/{team_id}", response_model=TeamRead)
def get_team(league_id: str, team_id: int, db: Session = Depends(get_db)) -> Team:
    """Get a specific team."""
    team = db.get(Team, team_id)
    if not team or team.league_id != league_id:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found in league '{league_id}'")
    return team


@router.put("/{league_id}/teams/{team_id}/roster", response_model=TeamRead)
def update_roster(
    league_id: str,
    team_id: int,
    roster: list[int],
    db: Session = Depends(get_db),
) -> Team:
    """Replace a team's roster with a new list of player IDs.

    Accepts a JSON array of player_ids in the request body, e.g.:
      [12345, 23456, 34567]
    """
    team = db.get(Team, team_id)
    if not team or team.league_id != league_id:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found in league '{league_id}'")
    team.roster = roster
    db.commit()
    db.refresh(team)
    return team
