from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional, TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, JSON, String, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasai.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from fantasai.models.prospect import ProspectProfile


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
    # Injury risk profile — set manually or inferred from transaction history.
    # "fragile"        → chronically injury-prone (Glasnow, Seager): 0.70× IP/PA discount
    # "recent_surgery" → post-major-surgery risk (Wheeler): 0.80× discount
    risk_flag: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    risk_note: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    stats: Mapped[list[PlayerStats]] = relationship(back_populates="player")
    rolling_stats: Mapped[list[PlayerRollingStats]] = relationship(back_populates="player")
    injury_record: Mapped[Optional[InjuryRecord]] = relationship(
        back_populates="player", uselist=False
    )
    prospect_profile: Mapped[Optional[ProspectProfile]] = relationship(
        "ProspectProfile", back_populates="player", uselist=False
    )


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
    # "projection" = Steamer/consensus forward projections
    # "actual"     = real accumulated stats from FanGraphs (current season)
    data_source: Mapped[str] = mapped_column(String(20), default="projection")
    counting_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    rate_stats: Mapped[dict] = mapped_column(JSON, default=dict)
    advanced_stats: Mapped[dict] = mapped_column(JSON, default=dict)

    player: Mapped[Player] = relationship(back_populates="stats")


class InjuryRecord(Base):
    """Current injury / IL status for a player.

    One row per player (upserted on each sync). Cleared when a player is
    activated. Populated by ``POST /rankings/sync-injuries`` (MLB Stats API)
    or manually via ``POST /rankings/set-injury``.

    status values:
      "il_10"          — 10-Day IL
      "il_60"          — 60-Day IL
      "day_to_day"     — day-to-day, not on formal IL
      "out_for_season" — season-ending injury
    """

    __tablename__ = "injury_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.player_id"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30))  # see docstring
    injury_description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    # Expected return date (for IL players). NULL = unknown.
    return_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    player: Mapped[Player] = relationship(back_populates="injury_record")


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
