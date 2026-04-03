"""Ranking storage models: persisted blurb rankings and daily snapshots for movement tracking."""
from __future__ import annotations

from datetime import date, datetime, timezone

import secrets

from sqlalchemy import Date, DateTime, ForeignKey, JSON, String, Integer, Float, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional

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
    period: Mapped[str] = mapped_column(String(30))  # e.g. "2026-W12", "2026-season"
    overall_rank: Mapped[int] = mapped_column(Integer)
    position_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score: Mapped[float] = mapped_column(Float)
    category_contributions: Mapped[dict] = mapped_column(JSON, default=dict)
    blurb: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    league_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("leagues.league_id"), nullable=True
    )
    # Three-component formula outputs (populated for Rest of Season rankings)
    statcast_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    steamer_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    accum_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # 1=Tier1 sustained outperformer, 2=Tier2 single-season, 3=Tier3 small-sample; None=normal
    outperformer_flag: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # {metric: {pct, label, avg, value}} — passed into blurb prompts for percentile language
    percentile_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # Public share token — used to serve the blurb card PNG without auth
    share_token: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True,
        default=lambda: secrets.token_urlsafe(32),
    )


class BlurbBatch(Base):
    """Tracks Anthropic batch submissions for async blurb generation.

    One row per batch submission (one per mode per scheduled run).
    player_data stores the per-player ranking metadata needed to upsert
    Ranking rows when the batch completes.
    """
    __tablename__ = "blurb_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(20))        # "season" | "current" | "week" | "month"
    period: Mapped[str] = mapped_column(String(30))      # "2026-season" | "2026-current" etc.
    batch_id: Mapped[str] = mapped_column(String(128), unique=True)  # Anthropic batch_id
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | collected | failed
    player_count: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    collected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # {player_id_str: {ranking_type, overall_rank, score, stat_type, ...}}
    # stored so collect can upsert Ranking rows without re-running rankings
    player_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


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
    # Component scores from the three-component formula
    component_scores: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    outperformer_flag: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
