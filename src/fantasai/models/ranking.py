"""Ranking storage models: persisted blurb rankings and daily snapshots for movement tracking."""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, JSON, String, Integer, Float, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fantasai.models.base import Base, TimestampMixin


class Ranking(TimestampMixin, Base):
    __tablename__ = "rankings"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "ranking_type", "period", "league_id", name="uq_ranking"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    ranking_type: Mapped[str] = mapped_column(String(20))  # "lookback" or "predictive"
    period: Mapped[str] = mapped_column(String(30))  # e.g. "2025-W12", "2025-season"
    overall_rank: Mapped[int] = mapped_column(Integer)
    position_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score: Mapped[float] = mapped_column(Float)
    category_contributions: Mapped[dict] = mapped_column(JSON, default=dict)
    blurb: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    league_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("leagues.league_id"), nullable=True
    )


class RankingSnapshot(TimestampMixin, Base):
    """One ranking position per player per mode per day.

    Used to compute movement arrows (↑3, ↓5) on the rankings UI.
    Stored daily for Current Rankings, weekly (Mondays) for projected modes.
    """
    __tablename__ = "ranking_snapshots"
    __table_args__ = (
        UniqueConstraint("player_id", "ranking_type", "horizon", "snapshot_date",
                         name="uq_ranking_snapshot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    ranking_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "current" | "predictive"
    horizon: Mapped[str] = mapped_column(String(20), nullable=False)       # "week" | "month" | "season" | "current"
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    overall_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    stat_type: Mapped[str] = mapped_column(String(10), nullable=False)     # "batting" | "pitching"
