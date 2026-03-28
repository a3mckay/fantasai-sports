"""Transaction model — stores graded league transactions (adds, drops, trades)."""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasai.models.base import Base, TimestampMixin


# Numeric score per letter grade (GPA-style, 0–4.3)
GRADE_SCORES: dict[str, float] = {
    "A+": 4.3, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F":  0.0,
}

GRADE_LETTERS = list(GRADE_SCORES.keys())


def _share_token() -> str:
    return secrets.token_urlsafe(32)


class Transaction(TimestampMixin, Base):
    """A graded league transaction fetched from Yahoo Fantasy.

    transaction_type: "add", "drop", "trade"

    participants JSON shape:
      add/drop: [{"manager_name": str, "team_key": str, "team_name": str,
                  "player_id": int | None, "player_name": str,
                  "action": "add" | "drop"}]
      trade:    [{"manager_name": str, "team_key": str, "team_name": str,
                  "players_added": [{"player_id": int|None, "player_name": str}],
                  "players_dropped": [{"player_id": int|None, "player_name": str}]}]
    """
    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_league_id", "league_id"),
        Index("ix_transactions_graded_at", "graded_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Dedup key — Yahoo's own transaction ID string
    yahoo_transaction_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)

    league_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("leagues.league_id", ondelete="CASCADE"), nullable=False
    )
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False)  # add|drop|trade

    # Full participant + player data as JSON (see docstring above)
    participants: Mapped[list] = mapped_column(JSON, default=list)

    # Grade
    grade_letter: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    grade_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    grade_rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Shareable card
    card_image_path: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    share_token: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, default=_share_token
    )

    # Timing
    yahoo_timestamp: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    graded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Lookback grade — set 4+ weeks after transaction based on actual player performance
    lookback_grade_letter: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    lookback_grade_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lookback_grade_rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lookback_graded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set True when importing historical transactions so they never surface in the ticker
    is_backfill: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
