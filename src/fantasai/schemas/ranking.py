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
