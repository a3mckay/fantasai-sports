from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class RankingRead(BaseModel):
    """Schema for persisted Ranking rows (future use)."""
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    ranking_type: str
    period: str
    overall_rank: int
    position_rank: Optional[int]
    score: float
    category_contributions: dict
    blurb: Optional[str]


class PlayerRankingRead(BaseModel):
    """Schema for on-demand computed PlayerRanking objects from the scoring engine."""

    player_id: int
    name: str
    team: str
    positions: list[str]
    stat_type: str
    overall_rank: int
    score: float
    raw_score: float
    category_contributions: dict[str, float]
    blurb: Optional[str] = None
    # Injury / risk fields for display in the UI.
    injury_status: str = "active"
    risk_flag: Optional[str] = None
    risk_note: Optional[str] = None
    # Prospect fields — set for MiLB players injected via PAV scoring.
    is_prospect: bool = False
    pav_score: Optional[float] = None
    # Movement tracking: positive = moved up, negative = moved down, None = new entry.
    # Computed from RankingSnapshot: 7 days ago for projected modes, 1 day ago for current.
    rank_delta: Optional[int] = None
    # Public share token — used to serve the blurb card PNG without auth.
    share_token: Optional[str] = None
