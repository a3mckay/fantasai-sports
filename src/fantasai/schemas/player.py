from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict


class PlayerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    name: str
    team: str
    positions: list[str]
    status: str


class PlayerStatsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    player_id: int
    season: int
    week: Optional[int]
    stat_type: str
    counting_stats: dict
    rate_stats: dict
    advanced_stats: dict
