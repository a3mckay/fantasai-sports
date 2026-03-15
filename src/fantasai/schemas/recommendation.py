from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class RecommendationRead(BaseModel):
    """ORM-backed recommendation (persisted to DB)."""

    model_config = ConfigDict(from_attributes=True)

    team_id: int
    rec_type: str
    player_id: int
    action: str
    rationale_blurb: Optional[str]
    category_impact: dict
    priority_score: float
    created_at: datetime
    expires_at: Optional[datetime]


class DropCandidateRead(BaseModel):
    """A suggested player to drop when making a waiver pickup."""

    player_id: int
    player_name: str
    positions: list[str]
    current_score: float
    category_contributions: dict[str, float]
    net_impact: float
    ip_warning: Optional[str] = None  # set when drop risks pitching floor


class WaiverRecommendationRead(BaseModel):
    """Rich waiver recommendation response — includes position fit, weak
    categories addressed, and drop suggestions."""

    player_id: int
    player_name: str
    team: str
    positions: list[str]
    priority_score: float
    category_impact: dict[str, float]
    fills_positions: list[str]
    weak_categories_addressed: list[str]
    drop_candidates: list[DropCandidateRead]
    action: str
    rationale_blurb: Optional[str] = None


class BuildPreferencesSchema(BaseModel):
    """User-provided build strategy preferences for waiver recommendations."""

    pitcher_strategy: str = Field(
        default="balanced",
        description='Pitcher build: "rp_heavy", "sp_heavy", or "balanced".',
    )
    punt_positions: list[str] = Field(
        default_factory=list,
        description='Positions to punt, e.g. ["C"].',
    )
    punt_categories: list[str] = Field(
        default_factory=list,
        description='Categories to punt, e.g. ["SB"].',
    )
    priority_targets: list[str] = Field(
        default_factory=list,
        description='Categories to prioritize, e.g. ["SV", "K"].',
    )


class StrategySuggestionRead(BaseModel):
    """Auto-detected build strategy suggestion with reasoning."""

    pitcher_strategy: str
    punt_positions: list[str]
    punt_categories: list[str]
    priority_targets: list[str]
    reasoning: dict[str, str]
    confidence: float
