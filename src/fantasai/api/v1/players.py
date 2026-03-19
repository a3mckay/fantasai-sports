from __future__ import annotations

import unicodedata
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.models.player import Player
from fantasai.schemas.player import PlayerRead

router = APIRouter(prefix="/players", tags=["players"])


def _normalize(text: str) -> str:
    """Fold accents and lowercase for accent-insensitive matching.

    Converts e.g. "José Ramírez" → "jose ramirez" so the search works
    regardless of whether the user types accented characters.
    """
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()


@router.get("", response_model=list[PlayerRead])
def list_players(
    position: Optional[str] = Query(default=None, description="Filter by position, e.g. 'OF'"),
    team: Optional[str] = Query(default=None, description="Filter by team abbreviation, e.g. 'NYY'"),
    search: Optional[str] = Query(default=None, description="Search by partial name (case-insensitive, accent-insensitive)"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Player]:
    """List players from the database with optional filters.

    Name search is accent-insensitive: typing "Jose Ramirez" finds "José Ramírez".
    """
    q = db.query(Player)

    if team:
        q = q.filter(Player.team == team.upper())

    players: list[Player] = []

    if search:
        norm_query = _normalize(search)
        # Try PostgreSQL unaccent() first; if the extension isn't available
        # (e.g. SQLite in tests) fall back to Python-level accent folding.
        try:
            players = (
                q.filter(func.unaccent(func.lower(Player.name)).contains(norm_query))
                .order_by(Player.name)
                .offset(offset)
                .limit(limit)
                .all()
            )
        except Exception:
            # unaccent() unavailable — roll back the failed transaction first
            # (PostgreSQL rejects all queries after an error until rollback),
            # then pull a broader set and filter in memory.
            db.rollback()
            base_q = db.query(Player)
            if team:
                base_q = base_q.filter(Player.team == team.upper())
            candidates = base_q.order_by(Player.name).limit(2000).all()
            players = [p for p in candidates if norm_query in _normalize(p.name)]
            players = players[offset: offset + limit]
    else:
        players = q.order_by(Player.name).offset(offset).limit(limit).all()

    if position:
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
