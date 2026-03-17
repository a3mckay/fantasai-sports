"""Pydantic schemas for the analysis API endpoints.

Covers three features:
  1. Compare Players  — rank 2+ players head-to-head with optional context
  2. Evaluate Trade   — assess a trade proposal with talent-density awareness
  3. Find Me a Player — suggest one available player for a specific roster slot
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Feature 1 — Compare Players
# ---------------------------------------------------------------------------


class CompareRequest(BaseModel):
    """Request body for the compare-players endpoint."""

    player_ids: list[int] = Field(
        ...,
        min_length=2,
        description="FanGraphs IDfg values for players to compare. Minimum 2.",
    )
    context: Optional[str] = Field(
        default=None,
        description='Optional user context, e.g. "I need stolen bases".',
    )
    league_id: Optional[int] = Field(
        default=None,
        description="League ID for category context. Uses default categories if omitted.",
    )
    custom_categories: Optional[list[str]] = Field(
        default=None,
        description="Custom scoring categories (used when no league_id).",
    )
    custom_league_type: Optional[str] = Field(
        default=None,
        description="Custom league type: h2h_categories, roto, or points.",
    )
    ranking_type: str = Field(
        default="predictive",
        description='"predictive" (forward-looking) or "current" (season-to-date).',
    )
    horizon: str = Field(
        default="season",
        pattern="^(week|month|season)$",
        description="Projection horizon for predictive rankings: week, month, or season.",
    )


class ComparePlayerResultRead(BaseModel):
    """A single player's result in a head-to-head comparison."""

    player_id: int
    player_name: str
    team: str
    positions: list[str]
    rank: int
    composite_score: float
    category_scores: dict[str, float]
    stat_type: str
    overall_rank: int = 0   # rank among all players — used for percentile display


class CompareResponse(BaseModel):
    """Response for the compare-players endpoint."""

    ranked_players: list[ComparePlayerResultRead]
    analysis_blurb: str
    context_applied: Optional[str] = Field(
        default=None,
        description="Echoes which categories were boosted based on user context.",
    )


# ---------------------------------------------------------------------------
# Feature 2 — Evaluate Trade
# ---------------------------------------------------------------------------


class TradeSide(BaseModel):
    """One side of a trade proposal."""

    player_ids: list[int] = Field(
        default_factory=list,
        description="FanGraphs IDfg values for players on this side.",
    )
    draft_picks: list[str] = Field(
        default_factory=list,
        description='Draft picks, e.g. ["2025 1st round", "2026 2nd round"].',
    )


class TradeRequest(BaseModel):
    """Request body for the trade-evaluation endpoint."""

    league_id: Optional[int] = Field(
        default=None,
        description="League ID for category context. Uses default categories if omitted.",
    )
    team_id: Optional[int] = Field(
        default=None,
        description="The team making the trade (your team). Required if no roster_player_ids.",
    )
    roster_player_ids: Optional[list[int]] = Field(
        default=None,
        description="Your full roster player IDs (when no team_id in DB).",
    )
    giving: TradeSide = Field(description="Assets your team is giving away.")
    receiving: TradeSide = Field(description="Assets your team is receiving.")
    context: Optional[str] = Field(
        default=None,
        description="Optional motivation context, e.g. 'need saves for the stretch run'.",
    )
    custom_categories: Optional[list[str]] = Field(
        default=None,
        description="Custom scoring categories (used when no league_id).",
    )
    custom_league_type: Optional[str] = Field(
        default=None,
        description="Custom league type: h2h_categories, roto, or points.",
    )
    horizon: str = Field(
        default="season",
        pattern="^(week|month|season)$",
        description=(
            "Projection horizon for trade evaluation: week (next 7 days), "
            "month (next 30 days), or season (full remaining season). "
            "Defaults to season — use 'week' or 'month' for deadline trades."
        ),
    )


class TradeResponse(BaseModel):
    """Response for the trade-evaluation endpoint."""

    verdict: str = Field(
        description='"favor_receive" (good for you), "favor_give" (bad for you), or "fair".',
    )
    confidence: float = Field(description="0.0–1.0 confidence in the verdict.")
    value_differential: float = Field(
        description="Talent-density-adjusted value difference. Positive = favor receive.",
    )
    raw_value_differential: float = Field(
        description="Raw score total difference (no density adjustment), for transparency.",
    )
    talent_density_note: str = Field(
        description="Human-readable explanation of talent concentration differences.",
    )
    category_impact: dict[str, float] = Field(
        description="Per-category value delta. Positive = you improve in that category.",
    )
    give_value: float = Field(description="Density-adjusted total value of giving side.")
    receive_value: float = Field(description="Density-adjusted total value of receiving side.")
    pros: list[str]
    cons: list[str]
    analysis_blurb: str


# ---------------------------------------------------------------------------
# Feature 3 — Find Me a Player
# ---------------------------------------------------------------------------


class FindPlayerRequest(BaseModel):
    """Request body for the find-player endpoint."""

    team_id: int
    position_slot: str = Field(
        description='Position slot to fill, e.g. "SP", "RP", "OF", "C", "UTIL".',
    )
    context: Optional[str] = Field(
        default=None,
        description='Optional context to guide the suggestion, e.g. "targeting saves".',
    )
    extra_exclude_ids: list[int] = Field(
        default_factory=list,
        description="Additional player IDs to exclude from suggestions.",
    )


class FindPlayerSuggestionRead(BaseModel):
    """A single player suggestion (current or historical)."""

    model_config = ConfigDict(from_attributes=True)

    player_id: int
    player_name: str
    positions: list[str]
    priority_score: float
    category_impact: dict[str, float]
    blurb: Optional[str] = None
    created_at: datetime


class FindPlayerResponse(BaseModel):
    """Response for the find-player endpoint."""

    suggestion: FindPlayerSuggestionRead
    all_suggestions: list[FindPlayerSuggestionRead] = Field(
        description="Full suggestion history for this team + position, newest first.",
    )


# ---------------------------------------------------------------------------
# Feature 4 — Extract Players from Screenshot
# ---------------------------------------------------------------------------


class ExtractPlayersRequest(BaseModel):
    """Request body for the extract-players endpoint."""

    image_base64: str = Field(description="Base64-encoded image data.")
    image_type: str = Field(
        default="image/jpeg",
        description="MIME type of the image.",
    )


class ExtractPlayersResponse(BaseModel):
    """Response for the extract-players endpoint."""

    player_names: list[str] = Field(
        description="Extracted player names from the image.",
    )
