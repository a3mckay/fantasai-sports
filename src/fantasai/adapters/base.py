from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedPlayerData:
    """Common format for player data across all sports."""

    player_id: int
    name: str
    team: str
    positions: list[str]
    status: str = "active"
    stat_type: str = ""  # "batting" or "pitching"
    counting_stats: dict[str, float] = field(default_factory=dict)
    rate_stats: dict[str, float] = field(default_factory=dict)
    advanced_stats: dict[str, float] = field(default_factory=dict)
    # Birth year derived from the Age column in source data (season - age).
    # Used for keeper-league future-value multipliers.
    birth_year: int | None = None


class SportAdapter(ABC):
    """Interface that all sport adapters must implement."""

    @abstractmethod
    def fetch_player_data(
        self, season: int, week: int | None = None
    ) -> list[NormalizedPlayerData]:
        """Fetch and normalize player data for a season/week."""
        ...

    @abstractmethod
    def get_positions(self) -> list[str]:
        """Return valid positions for this sport."""
        ...

    @abstractmethod
    def get_available_stats(self) -> list[str]:
        """Return all stat categories available for scoring."""
        ...

    @abstractmethod
    def get_predictive_stats(self) -> list[str]:
        """Return stats used for predictive rankings."""
        ...

    @abstractmethod
    def normalize_stats(
        self, raw_data: Any, stat_type: str = "batting"
    ) -> list[NormalizedPlayerData]:
        """Convert raw sport-specific data to common format.

        Args:
            raw_data: Sport-specific raw data (e.g., DataFrame from pybaseball).
            stat_type: Type of stats being normalized ("batting" or "pitching").
        """
        ...

    @abstractmethod
    def get_schedule(self, season: int, week: int) -> list[dict]:
        """Return matchup schedule for a given week."""
        ...
