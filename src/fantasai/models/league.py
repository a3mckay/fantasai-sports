from __future__ import annotations

from typing import Optional

from sqlalchemy import ForeignKey, JSON, String, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasai.models.base import Base, TimestampMixin


class League(TimestampMixin, Base):
    __tablename__ = "leagues"

    league_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    platform: Mapped[str] = mapped_column(String(20))  # "yahoo", "espn", etc.
    sport: Mapped[str] = mapped_column(String(10))  # "mlb"
    scoring_categories: Mapped[list] = mapped_column(JSON, default=list)
    league_type: Mapped[str] = mapped_column(String(30))  # "h2h_categories", "roto", "points"
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    roster_positions: Mapped[list] = mapped_column(JSON, default=list)
    owner_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    teams: Mapped[list[Team]] = relationship(back_populates="league")


class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    team_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league_id: Mapped[str] = mapped_column(ForeignKey("leagues.league_id"))
    yahoo_team_key: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    team_name: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    manager_name: Mapped[str] = mapped_column(String(200))
    roster: Mapped[list] = mapped_column(JSON, default=list)  # list of resolved FanGraphs player_ids (int)
    roster_names: Mapped[list] = mapped_column(JSON, default=list)  # original Yahoo player name strings
    owner_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    league: Mapped[Optional[League]] = relationship(back_populates="teams")
