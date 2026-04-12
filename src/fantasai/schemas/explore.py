from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class PavComponents(BaseModel):
    prospect_grade: float
    age_adj_performance: float
    vertical_velocity: float
    eta_proximity: float


class InjuryContext(BaseModel):
    """Current injury / IL status and chronic risk profile for a player."""
    status: Optional[str] = None          # "il_10", "il_60", "day_to_day", "out_for_season"
    description: Optional[str] = None     # free-text injury description
    expected_return: Optional[str] = None # ISO date string, e.g. "2026-04-22"
    risk_flag: Optional[str] = None       # "fragile" or "recent_surgery"
    risk_note: Optional[str] = None       # ≤80 char human-readable note


class ScheduleContext(BaseModel):
    """This-week schedule context for a player."""
    games_this_week: int = 0
    probable_starts: int = 0              # SP: total starts in Mon–Sun window
    future_starts: int = 0               # SP: starts not yet made this week
    opponent_teams: list[str] = []        # all opponents this week (pitchers: same as schedule opponents)
    today_opponent: Optional[str] = None  # team abbreviation for today's game
    today_is_home: Optional[bool] = None
    today_park: Optional[str] = None      # home team abbreviation (for park factor)
    today_park_factor: Optional[float] = None
    today_sp_name: Optional[str] = None   # opposing SP name (for batters)
    today_sp_throws: Optional[str] = None # "R", "L", "S"
    weather_hr_factor: float = 1.0
    weather_temp_f: float = 0.0
    weather_wind_mph: float = 0.0
    vegas_run_factor: float = 1.0
    week_context_text: Optional[str] = None  # pre-built human-readable summary


class PlayerContextResponse(BaseModel):
    player_id: int
    name: str
    team: str
    positions: list[str]
    stat_type: str          # "batting" or "pitching"
    mlbam_id: Optional[int] = None  # for headshot URL construction on frontend
    bats: Optional[str] = None      # "R", "L", "S" — batter handedness
    throws: Optional[str] = None    # "R", "L", "S" — pitcher handedness

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

    # Injury / health status
    injury: Optional[InjuryContext] = None

    # This week's schedule context
    schedule: Optional[ScheduleContext] = None


class ChatMessage(BaseModel):
    role: str    # "user" or "assistant"
    content: str


class ExploreChatRequest(BaseModel):
    player_ids: list[int]
    messages: list[ChatMessage] = []   # conversation history — last 5 pairs
    user_message: str
    league_id: Optional[str] = None
    my_team_id: Optional[int] = None   # user's own team_id — enables "help my team?" context
