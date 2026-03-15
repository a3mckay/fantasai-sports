"""Integration test: adapter → pipeline → scoring engine, end-to-end."""
from __future__ import annotations

from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from fantasai.adapters.base import NormalizedPlayerData
from fantasai.engine.pipeline import sync_players
from fantasai.engine.scoring import ScoringEngine


def _make_realistic_data() -> list[NormalizedPlayerData]:
    """Create a set of players that resembles real FanGraphs output."""
    return [
        NormalizedPlayerData(
            player_id=1,
            name="Aaron Judge",
            team="NYY",
            positions=["OF", "DH"],
            stat_type="batting",
            counting_stats={"R": 90, "HR": 40, "RBI": 100, "SB": 5, "PA": 550},
            rate_stats={"AVG": 0.290, "OPS": 0.950, "BB%": 15.0, "K%": 25.0},
            advanced_stats={
                "xwOBA": 0.400, "xBA": 0.285, "xSLG": 0.580,
                "Barrel%": 20.0, "HardHit%": 50.0, "EV": 93.0,
                "wRC+": 170, "LD%": 22.0, "Spd": 4.5,
            },
        ),
        NormalizedPlayerData(
            player_id=2,
            name="Juan Soto",
            team="NYM",
            positions=["OF"],
            stat_type="batting",
            counting_stats={"R": 95, "HR": 35, "RBI": 90, "SB": 8, "PA": 600},
            rate_stats={"AVG": 0.280, "OPS": 0.920, "BB%": 18.0, "K%": 20.0},
            advanced_stats={
                "xwOBA": 0.390, "xBA": 0.278, "xSLG": 0.540,
                "Barrel%": 17.0, "HardHit%": 47.0, "EV": 91.5,
                "wRC+": 160, "LD%": 24.0, "Spd": 4.0,
            },
        ),
        NormalizedPlayerData(
            player_id=3,
            name="Adley Rutschman",
            team="BAL",
            positions=["C"],
            stat_type="batting",
            counting_stats={"R": 60, "HR": 20, "RBI": 70, "SB": 2, "PA": 500},
            rate_stats={"AVG": 0.265, "OPS": 0.810, "BB%": 12.0, "K%": 19.0},
            advanced_stats={
                "xwOBA": 0.340, "xBA": 0.260, "xSLG": 0.450,
                "Barrel%": 10.0, "HardHit%": 38.0, "EV": 89.0,
                "wRC+": 125, "LD%": 20.0, "Spd": 3.5,
            },
        ),
        NormalizedPlayerData(
            player_id=10,
            name="Corbin Burnes",
            team="BAL",
            positions=["SP"],
            stat_type="pitching",
            counting_stats={"IP": 180, "W": 14, "SV": 0, "SO": 200},
            rate_stats={"ERA": 3.10, "WHIP": 1.08, "K%": 28.0, "BB%": 6.0},
            advanced_stats={
                "xERA": 3.20, "xFIP": 3.30, "SIERA": 3.15,
                "Stuff+": 110, "CSW%": 30.0, "K-BB%": 22.0,
                "SwStr%": 13.0, "GB%": 48.0, "HardHit%": 32.0, "Barrel%": 6.0,
            },
        ),
        NormalizedPlayerData(
            player_id=11,
            name="Average Pitcher",
            team="TEX",
            positions=["SP"],
            stat_type="pitching",
            counting_stats={"IP": 150, "W": 8, "SV": 0, "SO": 130},
            rate_stats={"ERA": 4.20, "WHIP": 1.28, "K%": 22.0, "BB%": 8.0},
            advanced_stats={
                "xERA": 4.30, "xFIP": 4.40, "SIERA": 4.20,
                "Stuff+": 95, "CSW%": 26.0, "K-BB%": 14.0,
                "SwStr%": 10.0, "GB%": 43.0, "HardHit%": 38.0, "Barrel%": 9.0,
            },
        ),
    ]


class TestEndToEnd:
    """Integration: mock adapter → pipeline → scoring."""

    def test_pipeline_then_lookback(self, db: Session) -> None:
        """Data flows from adapter through pipeline and produces valid rankings."""
        data = _make_realistic_data()
        adapter = MagicMock()
        adapter.fetch_player_data.return_value = data

        # Pipeline: persist to DB
        result = sync_players(db, adapter, season=2025)
        assert len(result) == 5

        # Scoring: rank the same data
        categories = ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"]
        engine = ScoringEngine(adapter, categories)
        rankings = engine.compute_lookback_rankings(2025, players=result)

        assert len(rankings) == 5
        # All ranks should be assigned
        assert all(r.overall_rank > 0 for r in rankings)
        # Ranks should be contiguous 1-5
        ranks = sorted(r.overall_rank for r in rankings)
        assert ranks == [1, 2, 3, 4, 5]
        # Each ranking should have category contributions
        assert all(len(r.category_contributions) > 0 for r in rankings)

    def test_pipeline_then_predictive(self, db: Session) -> None:
        """Predictive rankings should also work end-to-end."""
        data = _make_realistic_data()
        adapter = MagicMock()
        adapter.fetch_player_data.return_value = data

        result = sync_players(db, adapter, season=2025)

        categories = ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"]
        engine = ScoringEngine(adapter, categories)
        rankings = engine.compute_predictive_rankings(2025, players=result)

        assert len(rankings) == 5
        # Top batter should be Judge or Soto (best underlying metrics)
        top_batter = next(r for r in rankings if r.stat_type == "batting")
        assert top_batter.name in ("Aaron Judge", "Juan Soto")
        # Top pitcher should be Burnes (better stuff)
        top_pitcher = next(r for r in rankings if r.stat_type == "pitching")
        assert top_pitcher.name == "Corbin Burnes"

    def test_catcher_scarcity_integration(self, db: Session) -> None:
        """Catcher positional scarcity should visibly affect score vs raw_score."""
        data = _make_realistic_data()
        adapter = MagicMock()
        adapter.fetch_player_data.return_value = data

        categories = ["R", "HR", "RBI", "SB", "AVG", "OPS"]
        engine = ScoringEngine(adapter, categories)
        rankings = engine.compute_lookback_rankings(2025, players=data)

        rutschman = next(r for r in rankings if r.name == "Adley Rutschman")
        # Scarcity multiplier should make score differ from raw_score
        assert rutschman.score != rutschman.raw_score
        # The multiplier for C is 1.15, so |score| > |raw_score|
        assert abs(rutschman.score) > abs(rutschman.raw_score)
