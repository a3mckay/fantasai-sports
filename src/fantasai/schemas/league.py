from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LeagueCreate(BaseModel):
    league_id: str
    platform: str
    sport: str
    scoring_categories: list[str]
    league_type: str
    settings: dict
    roster_positions: list[str]


class LeagueRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    league_id: str
    platform: str
    sport: str
    scoring_categories: list[str]
    league_type: str
    settings: dict
    roster_positions: list[str]


class TeamCreate(BaseModel):
    league_id: str
    manager_name: str
    roster: list[int] = []


class TeamRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    team_id: int
    league_id: str
    manager_name: str
    roster: list[int]
