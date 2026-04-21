from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from fantasai.models.base import Base, TimestampMixin


class ScoringGridSnapshot(TimestampMixin, Base):
    __tablename__ = "scoring_grid_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league_id: Mapped[str] = mapped_column(String(100), ForeignKey("leagues.league_id"), index=True)
    season: Mapped[int] = mapped_column(Integer)
    week: Mapped[int] = mapped_column(Integer)

    # {team_key: {R: 22, HR: 5, ERA: 3.11, ...}}
    team_stats: Mapped[dict] = mapped_column(JSON, default=dict)

    # [{team_key, team_name, manager_name}]
    teams_meta: Mapped[list] = mapped_column(JSON, default=list)

    __table_args__ = (
        UniqueConstraint("league_id", "season", "week", name="uq_scoring_grid_league_season_week"),
    )
