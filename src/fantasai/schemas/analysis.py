"""Pydantic schemas for the analysis API endpoints.

Covers these features:
  1. Compare Players  — rank 2+ players head-to-head with optional context
  2. Evaluate Trade   — assess a trade proposal with talent-density awareness
  3. Find Me a Player — suggest one available player for a specific roster slot
  4. Build Trade      — generate fair trade proposals given target player(s)
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
    league_id: Optional[str] = Field(
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

    league_id: Optional[str] = Field(
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
# Feature 2b — Build Trade
# ---------------------------------------------------------------------------


class TradeBuildRequest(BaseModel):
    """Request body for the trade-builder endpoint."""

    league_id: Optional[str] = Field(
        default=None,
        description="League ID for category and roster context.",
    )
    my_team_id: Optional[int] = Field(
        default=None,
        description="Your team ID. Used to load your roster.",
    )
    their_team_id: Optional[int] = Field(
        default=None,
        description="Other team's ID. Used for roster-need analysis (optional).",
    )
    my_roster_player_ids: Optional[list[int]] = Field(
        default=None,
        description="Your full roster when no my_team_id is available.",
    )
    their_roster_player_ids: Optional[list[int]] = Field(
        default=None,
        description="Other team's roster when no their_team_id is available.",
    )
    target_player_ids: list[int] = Field(
        ...,
        min_length=1,
        description="Players you want to receive (FanGraphs IDfg).",
    )
    context: Optional[str] = Field(
        default=None,
        description='What the other manager is looking for, e.g. "He wants arms".',
    )
    value_tolerance: float = Field(
        default=0.0,
        ge=-1.0,
        le=1.0,
        description=(
            "Slider for value lopsidedness. "
            "-1.0 = only propose trades where you get fair/more value; "
            " 0.0 = target fair trades; "
            "+1.0 = willing to overpay significantly."
        ),
    )
    horizon: str = Field(
        default="season",
        pattern="^(week|month|season)$",
        description="Projection horizon for player values.",
    )
    custom_categories: Optional[list[str]] = Field(
        default=None,
        description="Custom scoring categories (used when no league_id).",
    )
    custom_league_type: Optional[str] = Field(
        default=None,
        description="Custom league type: h2h_categories, roto, or points.",
    )


class TradeSuggestionRead(BaseModel):
    """One proposed trade package returned by the build-trade endpoint."""

    label: str = Field(description='E.g. "Best 2-for-1", "Draft Pick Sweetener".')
    give_player_ids: list[int]
    give_picks: list[str] = Field(description="Pick strings on your side (same count as receive_picks).")
    receive_player_ids: list[int]
    receive_picks: list[str] = Field(description="Pick strings on their side.")
    give_value: float
    receive_value: float
    value_differential: float = Field(
        description="receive_value − give_value. Negative = you're overpaying."
    )
    fairness_score: float = Field(description="0–1, how close this is to your slider target.")
    positional_warnings: list[str] = Field(
        description="E.g. 'Trading X leaves you with no SS coverage.'",
    )
    respects_roster_needs: bool = Field(
        description="False for the Wildcard suggestion that ignores the other team's needs.",
    )
    fit_note: str = Field(
        default="",
        description="LLM-generated 2-3 sentence explanation of why this works for both teams.",
    )


class TradeBuildResponse(BaseModel):
    """Response for the build-trade endpoint."""

    suggestions: list[TradeSuggestionRead]
    target_value: float = Field(description="Density-adjusted value of the target player(s).")
    candidates_evaluated: int = Field(
        description="Total candidate packages evaluated before selection.",
    )


# ---------------------------------------------------------------------------
# Feature 3 — Find Me a Player
# ---------------------------------------------------------------------------


class FindPlayerRequest(BaseModel):
    """Request body for the find-player endpoint.

    At least one of ``position_slot`` or ``priority_categories`` must be
    provided (unless ``player_pool`` is ``"milb"``, in which case top
    prospects by PAV are returned regardless).
    """

    team_id: int
    position_slot: Optional[str] = Field(
        default=None,
        description='Position slot to fill, e.g. "SP", "RP", "OF", "C", "UTIL". Optional.',
    )
    priority_categories: list[str] = Field(
        default_factory=list,
        description='Scoring categories to bias toward, e.g. ["SB", "HR"]. Multi-select.',
    )
    player_pool: str = Field(
        default="mlb",
        pattern="^(mlb|milb|both)$",
        description='"mlb" (default), "milb" (prospects only, sorted by PAV), or "both".',
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
    search_params_label: str = Field(
        default="",
        description='Human-readable label of the search that produced this suggestion, e.g. "SS + SB".',
    )
    is_prospect: bool = Field(default=False, description="True if this is a MiLB prospect.")
    pav_score: Optional[float] = Field(default=None, description="PAV score for MiLB prospects.")


class FindPlayerResponse(BaseModel):
    """Response for the find-player endpoint."""

    suggestion: FindPlayerSuggestionRead
    milb_suggestion: Optional[FindPlayerSuggestionRead] = Field(
        default=None,
        description="Top MiLB prospect when player_pool='both'.",
    )
    all_suggestions: list[FindPlayerSuggestionRead] = Field(
        default_factory=list,
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
