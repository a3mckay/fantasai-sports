"""ProspectProfile model — stores PAV inputs and computed scores for MiLB prospects."""
from __future__ import annotations

from datetime import datetime
from typing import Optional, TYPE_CHECKING

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasai.models.base import Base

if TYPE_CHECKING:
    from fantasai.models.player import Player


class ProspectProfile(Base):
    """One row per MiLB prospect.  Linked 1-to-1 to the Player row via player_id.

    All inputs are fetched automatically by the prospect_pipeline sync; no
    manual data entry is required.

    pav_score and proxy_mlb_rank are computed by pav_scorer and cached here so
    the ranking endpoint can inject prospects without re-running the formula
    on every request.
    """

    __tablename__ = "prospect_profiles"

    # Primary key = same as Player.player_id (FK, not auto-increment)
    player_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("players.player_id"), primary_key=True
    )

    # ---- MLB Stats API / pipeline data ----
    mlbam_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # Implied rank among all synced prospects (1 = best based on performance metrics).
    # Used as a proxy for scouting grade when no OFP is available.
    pipeline_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Explicit OFP grade (20–80 scale) from Pipeline/BA/FG if ever available.
    pipeline_grade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ba_grade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    fg_grade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Per-level stint data from the current season.
    # Hitters: [{level, games, ops, player_age}]
    # Pitchers: [{level, ip, era, k9, whip, player_age}]
    stints: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    # Number of levels advanced during the most recent full season (for velocity).
    levels_in_season: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Highest level reached (e.g. "Double-A", "Triple-A").
    highest_level: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Year the player was drafted / signed as an IFA (to compute years_pro).
    draft_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # ETA situation key fed to calculate_eta_proximity().
    eta_situation: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    # stat_type: "batting" or "pitching" (determines which PAV formula to use)
    stat_type: Mapped[str] = mapped_column(String(20), default="batting")

    # ---- Computed by PAV scorer ----
    pav_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    proxy_mlb_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    last_synced: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Relationships ----
    player: Mapped["Player"] = relationship("Player", back_populates="prospect_profile")
