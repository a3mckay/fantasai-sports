"""Pydantic schemas for team evaluation, keeper planning, and league analysis."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Feature 1 — Team Evaluation
# ---------------------------------------------------------------------------


class TeamEvalRequest(BaseModel):
    """Request body for the team-eval endpoint.

    Accepts either a ``team_id`` (pulls roster from DB) or a raw
    ``player_ids`` list. At least one must be provided.
    """

    team_id: Optional[int] = Field(
        default=None,
        description="Team ID in the DB. Roster pulled automatically.",
    )
    player_ids: Optional[list[int]] = Field(
        default=None,
        description="FanGraphs IDfg values for the team's players. Use if no team_id.",
    )
    league_id: Optional[str] = Field(
        default=None,
        description="League ID for category context and percentile grading.",
    )
    context: Optional[str] = Field(
        default=None,
        description=(
            "Free-text user context, e.g. 'win-now mode' or 'punting stolen bases'. "
            "Passed verbatim to the LLM and used for keyword-based algorithmic adjustments."
        ),
    )
    ranking_type: str = Field(
        default="predictive",
        description='"predictive" (forward-looking) or "current" (season-to-date).',
    )
    custom_categories: Optional[list[str]] = Field(
        default=None,
        description="Custom scoring categories (used when no league_id).",
    )
    custom_league_type: Optional[str] = Field(
        default=None,
        description="Custom league type: h2h_categories, roto, or points.",
    )
    custom_roster_positions: Optional[list[str]] = Field(
        default=None,
        description="Custom roster positions (used when no league_id).",
    )

    @model_validator(mode="after")
    def require_team_or_players(self) -> "TeamEvalRequest":
        if self.team_id is None and not self.player_ids:
            raise ValueError("Provide either team_id or player_ids.")
        return self


class PositionGroupRead(BaseModel):
    """A position group summary for the team breakdown."""

    position: str
    players: list[str]
    group_score: float
    assessment: str  # "elite" | "solid" | "average" | "weak" | "empty"


class TeamEvalResponse(BaseModel):
    """Response for the team-eval endpoint."""

    overall_score: float
    letter_grade: str = Field(description="A–F overall team grade.")
    grade_percentile: float = Field(description="0–100 percentile vs league (or estimated).")
    category_strengths: dict[str, float]
    strong_categories: list[str]
    weak_categories: list[str]
    position_breakdown: list[PositionGroupRead]
    improvement_suggestions: list[str]
    pros: list[str]
    cons: list[str]
    analysis_blurb: str
    league_category_percentiles: Optional[dict[str, float]] = Field(
        default=None,
        description=(
            "Per-category percentile rank (0–100) vs other league teams. "
            "Provided when league_id is supplied; use for category bar display "
            "instead of raw z-score conversion."
        ),
    )


# ---------------------------------------------------------------------------
# Feature 2 — Keeper / Dynasty Evaluation
# ---------------------------------------------------------------------------


class KeeperEvalRequest(BaseModel):
    """Request body for the keeper-eval endpoint."""

    mode: str = Field(
        description=(
            '"evaluate_keepers": input players ARE the keepers — evaluate them and '
            'suggest what to draft. '
            '"plan_keepers": input is the full roster — app recommends who to keep and '
            'who to cut, then suggests draft targets.'
        ),
    )
    team_id: Optional[int] = Field(
        default=None,
        description="Team ID in DB. Roster pulled automatically.",
    )
    player_ids: Optional[list[int]] = Field(
        default=None,
        description="FanGraphs IDfg values. Use if no team_id.",
    )
    n_keepers: int = Field(
        default=5,
        description="Number of players to keep (plan_keepers mode only).",
    )
    league_id: Optional[str] = Field(
        default=None,
        description="League ID for category context and available pool.",
    )
    context: Optional[str] = Field(
        default=None,
        description=(
            "Free-text context, e.g. 'I prefer upside over floor' or "
            "'targeting a championship this year'."
        ),
    )
    custom_categories: Optional[list[str]] = Field(
        default=None,
        description="Custom scoring categories (used when no league_id).",
    )
    custom_league_type: Optional[str] = Field(
        default=None,
        description="Custom league type: h2h_categories, roto, or points.",
    )
    custom_roster_positions: Optional[list[str]] = Field(
        default=None,
        description="Custom roster positions (used when no league_id).",
    )
    n_teams: int = Field(
        default=12,
        description=(
            "Number of teams in the league. Used to compute the keeper threshold "
            "(n_teams × n_keepers_per_team) for rank-based grading. "
            "Defaults to 12 (standard league size)."
        ),
    )

    @model_validator(mode="after")
    def validate_mode_and_input(self) -> "KeeperEvalRequest":
        if self.mode not in ("evaluate_keepers", "plan_keepers"):
            raise ValueError('mode must be "evaluate_keepers" or "plan_keepers".')
        if self.team_id is None and not self.player_ids:
            raise ValueError("Provide either team_id or player_ids.")
        return self


class DraftProfileRead(BaseModel):
    """A recommended player profile to target in an upcoming draft."""

    priority: int
    position: str
    category_targets: list[str]
    rationale: str
    example_players: list[str] = Field(
        default_factory=list,
        description="Specific available players matching this profile (when data is available).",
    )


class PlayerSummaryRead(BaseModel):
    """Lightweight player summary for keeper lists."""

    player_id: int
    player_name: str
    positions: list[str]
    score: float


class KeeperEvalResponse(BaseModel):
    """Response for the keeper-eval endpoint."""

    mode: str
    keepers: list[PlayerSummaryRead]
    cuts: list[PlayerSummaryRead] = Field(
        default_factory=list,
        description="Players recommended to cut (plan_keepers mode only).",
    )
    keeper_foundation_grade: str = Field(description="A–F grade for the keeper core.")
    category_strengths: dict[str, float]
    category_gaps: list[str]
    position_gaps: list[str]
    draft_profiles: list[DraftProfileRead]
    pros: list[str]
    cons: list[str]
    analysis_blurb: str


# ---------------------------------------------------------------------------
# Feature 3 — Compare Teams
# ---------------------------------------------------------------------------


class ManualTeam(BaseModel):
    """A manually specified team with a name and roster player IDs."""

    name: str = Field(description="Team name for display.")
    player_ids: list[int] = Field(
        description="FanGraphs IDfg values for this team's roster.",
    )


class CompareTeamsRequest(BaseModel):
    """Request body for the compare-teams endpoint."""

    team_ids: Optional[list[int]] = Field(
        default=None,
        description="IDs of 2-6 teams from DB.",
    )
    manual_teams: Optional[list[ManualTeam]] = Field(
        default=None,
        description="Manual team input with player_ids.",
    )
    league_id: Optional[str] = Field(
        default=None,
        description="League ID for scoring category context.",
    )
    context: Optional[str] = Field(
        default=None,
        description="Free-text context, e.g. 'looking for trade targets'.",
    )
    include_trade_suggestions: bool = Field(
        default=True,
        description="Whether to surface potential trade opportunities between the teams.",
    )
    custom_categories: Optional[list[str]] = Field(
        default=None,
        description="Custom scoring categories (used when no league_id).",
    )
    custom_league_type: Optional[str] = Field(
        default=None,
        description="Custom league type: h2h_categories, roto, or points.",
    )

    @model_validator(mode="after")
    def require_team_ids_or_manual_teams(self) -> "CompareTeamsRequest":
        has_team_ids = self.team_ids is not None and len(self.team_ids) >= 2
        has_manual = self.manual_teams is not None and len(self.manual_teams) >= 2
        if not has_team_ids and not has_manual:
            raise ValueError(
                "Provide either team_ids (at least 2) or manual_teams (at least 2)."
            )
        return self


class TeamSnapshotRead(BaseModel):
    """Summary of a single team for comparison output."""

    team_id: int
    team_name: str
    power_score: float
    category_strengths: dict[str, float]
    strong_cats: list[str]
    weak_cats: list[str]
    top_players: list[str]


class TradeOpportunityRead(BaseModel):
    """A complementary trade opportunity between two teams."""

    team_a_id: int
    team_b_id: int
    team_a_gives_cats: list[str] = Field(description="Categories Team A can give (= B's need).")
    team_b_gives_cats: list[str] = Field(description="Categories Team B can give (= A's need).")
    suggested_give: Optional[str] = Field(default=None, description="Best player Team A could offer.")
    suggested_receive: Optional[str] = Field(default=None, description="Best player Team B could offer.")
    complementarity_score: float
    rationale: str


class CompareTeamsResponse(BaseModel):
    """Response for the compare-teams endpoint."""

    snapshots: list[TeamSnapshotRead] = Field(description="Teams sorted by power score, best first.")
    winner: int = Field(description="team_id of the strongest team.")
    trade_opportunities: list[TradeOpportunityRead]
    analysis_blurb: str


# ---------------------------------------------------------------------------
# Feature 4 — League Power Rankings
# ---------------------------------------------------------------------------


class LeaguePowerResponse(BaseModel):
    """Response for the league-power endpoint."""

    power_rankings: list[TeamSnapshotRead] = Field(description="All teams, best→worst.")
    tiers: dict[str, list[int]] = Field(
        description='{"contender": [team_ids], "middle": [...], "rebuilding": [...]}'
    )
    trade_opportunities: list[TradeOpportunityRead] = Field(
        description="Top 10 most complementary trade pairs in the league.",
    )
    analysis_blurb: str


# ---------------------------------------------------------------------------
# Feature 5 — Roster Analysis (Recommend a Player → Roster Analysis tab)
# ---------------------------------------------------------------------------


class WaiverUpgradeRead(BaseModel):
    """A waiver wire player that upgrades a weak roster slot."""

    player_id: int
    player_name: str
    positions: list[str]
    score: float
    category_impact: dict[str, float] = Field(default_factory=dict)


class TradeTargetRead(BaseModel):
    """A rostered player on another team that could upgrade a weak slot."""

    player_id: int
    player_name: str
    positions: list[str]
    score: float
    owner_team_name: str
    owner_team_id: int
    difficulty: str = Field(description='"possible" | "hard" | "unrealistic"')
    difficulty_reason: str


class RosterSlotRead(BaseModel):
    """One position group on the roster with its assessment and upgrade options."""

    position: str
    assessment: str = Field(description='"elite" | "solid" | "average" | "weak" | "empty"')
    players: list[str]
    group_score: float
    priority: int = Field(description="Lower = higher urgency. Used for display ordering.")
    waiver_upgrades: list[WaiverUpgradeRead] = Field(default_factory=list)
    trade_targets: list[TradeTargetRead] = Field(default_factory=list)


class RosterAnalysisResponse(BaseModel):
    """Full roster analysis with per-slot upgrade recommendations."""

    overall_grade: str
    overall_score: float
    grade_percentile: float
    weak_categories: list[str]
    strong_categories: list[str]
    category_strengths: dict[str, float]
    slots: list[RosterSlotRead]
