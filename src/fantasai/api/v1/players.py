from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.models.player import Player
from fantasai.schemas.player import PlayerRead

router = APIRouter(prefix="/players", tags=["players"])


@router.get("", response_model=list[PlayerRead])
def list_players(
    position: Optional[str] = Query(default=None, description="Filter by position, e.g. 'OF'"),
    team: Optional[str] = Query(default=None, description="Filter by team abbreviation, e.g. 'NYY'"),
    search: Optional[str] = Query(default=None, description="Search by partial name (case-insensitive)"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Player]:
    """List players from the database with optional filters."""
    q = db.query(Player)

    if team:
        q = q.filter(Player.team == team.upper())

    if search:
        q = q.filter(Player.name.ilike(f"%{search}%"))

    players = q.order_by(Player.name).offset(offset).limit(limit).all()

    if position:
        # JSON array contains — filter in Python (SQLite-compatible)
        pos = position.upper()
        players = [p for p in players if pos in (p.positions or [])]

    return players


@router.get("/{player_id}", response_model=PlayerRead)
def get_player(player_id: int, db: Session = Depends(get_db)) -> Player:
    """Get a single player by their FanGraphs ID."""
    player = db.get(Player, player_id)
    if not player:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found")
    return player
