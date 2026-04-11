from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class PavComponents(BaseModel):
    prospect_grade: float
    age_adj_performance: float
    vertical_velocity: float
    eta_proximity: float


class PlayerContextResponse(BaseModel):
    player_id: int
    name: str
    team: str
    positions: list[str]
    stat_type: str          # "batting" or "pitching"
    mlbam_id: Optional[int] = None  # for headshot URL construction on frontend

    # 2026 season actuals (primary)
    actual_stats: dict = {}
    # Steamer rest-of-season projections (secondary)
    projection_stats: Optional[dict] = None

    # Overall ranking
    overall_rank: Optional[int] = None
    rank_score: Optional[float] = None
    rank_list_name: str = "Predictive Rankings (Rest of Season)"

    # PAV — only for prospects
    is_prospect: bool = False
    pav_score: Optional[float] = None
    pav_components: Optional[PavComponents] = None

    # League ownership
    owned_by: Optional[str] = None   # team name if owned, None if available


class ChatMessage(BaseModel):
    role: str    # "user" or "assistant"
    content: str


class ExploreChatRequest(BaseModel):
    player_ids: list[int]
    messages: list[ChatMessage] = []   # conversation history — last 5 pairs
    user_message: str
    league_id: Optional[str] = None
