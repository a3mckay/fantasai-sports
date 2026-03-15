from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, JSON, String, Integer, Float, Text, UniqueConstraint
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
