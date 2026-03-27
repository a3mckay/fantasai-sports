"""MatchupAnalysis model — stores weekly H2H matchup projections and narratives."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from fantasai.models.base import Base, TimestampMixin


class MatchupAnalysis(TimestampMixin, Base):
    """Per-matchup weekly analysis for a fantasy league.

    Stores projected category totals for both teams, per-category projected
    winner, and a Claude-generated narrative. Refreshed daily by APScheduler.

    category_projections JSON shape:
      {
        "HR": {"team1": 12.3, "team2": 9.1, "edge": "team1"},
        "AVG": {"team1": 0.281, "team2": 0.295, "edge": "team2"},
        ...
      }

    live_stats JSON shape (populated mid-week from Yahoo scoreboard):
      {
        "HR": {"team1": 8, "team2": 6},
        "AVG": {"team1": 0.294, "team2": 0.271},
        ...
      }

    suggestions JSON shape:
      [{"type": "add"|"start", "player_name": str, "rationale": str, "category_impact": str}]
    """
    __tablename__ = "matchup_analyses"
    __table_args__ = (
        Index("ix_matchup_league_week", "league_id", "season", "week"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    league_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("leagues.league_id", ondelete="CASCADE"), nullable=False
    )
    season: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)

    # Yahoo team keys (e.g. "411.l.12345.t.1")
    team1_key: Mapped[str] = mapped_column(String(100), nullable=False)
    team2_key: Mapped[str] = mapped_column(String(100), nullable=False)
    team1_name: Mapped[str] = mapped_column(String(200), nullable=False)
    team2_name: Mapped[str] = mapped_column(String(200), nullable=False)
    manager1_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    manager2_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # Projected category totals and edge calls
    category_projections: Mapped[dict] = mapped_column(JSON, default=dict)

    # Live mid-week stats from Yahoo (None until week is underway)
    live_stats: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Claude-generated 3-5 sentence narrative
    narrative: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Roster move suggestions
    suggestions: Mapped[list] = mapped_column(JSON, default=list)

    # When this analysis was last computed
    generated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
