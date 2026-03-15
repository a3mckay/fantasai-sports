from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import ForeignKey, JSON, String, Integer, Float, Text, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from fantasai.models.base import Base


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.team_id"))
    rec_type: Mapped[str] = mapped_column(String(30))  # waiver_add, trade_target, etc.
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    action: Mapped[str] = mapped_column(String(200))
    rationale_blurb: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category_impact: Mapped[dict] = mapped_column(JSON, default=dict)
    priority_score: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
