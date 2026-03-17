from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, JSON, String, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasai.models.base import Base, TimestampMixin


class Player(TimestampMixin, Base):
    __tablename__ = "players"

    player_id: Mapped[int] = mapped_column(Integer, primary_key=True)  # FanGraphs IDfg
    name: Mapped[str] = mapped_column(String(200))
    team: Mapped[str] = mapped_column(String(10))
    positions: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["SS", "2B"]
    status: Mapped[str] = mapped_column(String(20), default="active")
    mlbam_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    fangraphs_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Birth year derived from pybaseball Age column; used for keeper-league
    # future-value multipliers. Stored as year so it's season-agnostic.
    birth_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    stats: Mapped[list[PlayerStats]] = relationship(back_populates="player")
    rolling_stats: Mapped[list[PlayerRollingStats]] = relationship(back_populates="player")


class PlayerStats(TimestampMixin, Base):
    __tablename__ = "player_stats"
    __table_args__ = (
        UniqueConstraint("player_id", "season", "week", "stat_type", name="uq_player_stats"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"))
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stat_type: Mapped[str] = mapped_column(String(20))  # "batting" or "pitching"
    counting_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    rate_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    advanced_stats: Mapped[dict] = mapped_column(JSON, default=dict)

    player: Mapped[Player] = relationship(back_populates="stats")


class PlayerRollingStats(TimestampMixin, Base):
    """Rolling-window stats for a player over a specific date range.

    Stores stats aggregated over 7, 14, 30, and 60-day windows fetched from
    Baseball Reference via pybaseball. Refreshed daily by the pipeline.
    Separate from PlayerStats (season-to-date) because the source, columns,
    and refresh cadence differ.
    """

    __tablename__ = "player_rolling_stats"
    __table_args__ = (
        UniqueConstraint(
            "player_id", "season", "window_days", "stat_type",
            name="uq_player_rolling_stats",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.player_id"), index=True)
    season: Mapped[int] = mapped_column(Integer, index=True)
    # Number of days in the window: 7, 14, 30, or 60
    window_days: Mapped[int] = mapped_column(Integer, index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    stat_type: Mapped[str] = mapped_column(String(20))  # "batting" or "pitching"
    counting_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    rate_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    # Rank among all players of this stat_type within this window (1 = best)
    window_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    player: Mapped[Player] = relationship(back_populates="rolling_stats")
