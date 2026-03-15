"""Tests for the scoring engine."""
from __future__ import annotations

import pytest

from fantasai.adapters.base import NormalizedPlayerData
from fantasai.engine.scoring import (
    ScoringEngine,
    PlayerRanking,
    _get_scarcity_multiplier,
    _assign_position_ranks,
)
from fantasai.adapters.mlb import MLBAdapter


def _make_batter(
    player_id: int,
    name: str,
    positions: list[str],
    counting: dict | None = None,
    rate: dict | None = None,
    advanced: dict | None = None,
) -> NormalizedPlayerData:
    return NormalizedPlayerData(
        player_id=player_id,
        name=name,
        team="NYY",
        positions=positions,
        stat_type="batting",
        counting_stats=counting or {},
        rate_stats=rate or {},
        advanced_stats=advanced or {},
    )


def _make_pitcher(
    player_id: int,
    name: str,
    positions: list[str],
    counting: dict | None = None,
    rate: dict | None = None,
    advanced: dict | None = None,
) -> NormalizedPlayerData:
    return NormalizedPlayerData(
        player_id=player_id,
        name=name,
        team="LAD",
        positions=positions,
        stat_type="pitching",
        counting_stats=counting or {},
        rate_stats=rate or {},
        advanced_stats=advanced or {},
    )


# --- Lookback Scoring Tests ---


class TestLookbackScoring:
    def setup_method(self):
        self.adapter = MLBAdapter()
        self.categories = ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"]
        self.engine = ScoringEngine(self.adapter, self.categories)

    def test_basic_ranking_order(self):
        """Player with better stats across categories should rank higher."""
        players = [
            _make_batter(1, "Good Hitter", ["OF"], counting={"R": 80, "HR": 30, "RBI": 90, "SB": 15}, rate={"AVG": .300, "OPS": .900}),
            _make_batter(2, "Average Hitter", ["OF"], counting={"R": 50, "HR": 15, "RBI": 55, "SB": 5}, rate={"AVG": .260, "OPS": .750}),
            _make_batter(3, "Bad Hitter", ["OF"], counting={"R": 30, "HR": 5, "RBI": 30, "SB": 1}, rate={"AVG": .220, "OPS": .620}),
        ]
        rankings = self.engine.compute_lookback_rankings(2025, players=players)
        assert rankings[0].name == "Good Hitter"
        assert rankings[-1].name == "Bad Hitter"

    def test_multi_category_contributor_beats_specialist(self):
        """Player contributing across 4 categories should beat a one-category star."""
        players = [
            _make_batter(1, "Balanced", ["OF"], counting={"R": 70, "HR": 20, "RBI": 70, "SB": 15}, rate={"AVG": .280, "OPS": .830}),
            _make_batter(2, "HR Only", ["OF"], counting={"R": 40, "HR": 45, "RBI": 40, "SB": 0}, rate={"AVG": .220, "OPS": .780}),
            _make_batter(3, "Filler", ["OF"], counting={"R": 50, "HR": 15, "RBI": 50, "SB": 5}, rate={"AVG": .250, "OPS": .730}),
        ]
        rankings = self.engine.compute_lookback_rankings(2025, players=players)
        assert rankings[0].name == "Balanced"

    def test_category_contributions_present(self):
        """Each ranking should include z-scores per category."""
        players = [
            _make_batter(1, "A", ["OF"], counting={"R": 70, "HR": 20, "RBI": 60, "SB": 10}, rate={"AVG": .280, "OPS": .820}),
            _make_batter(2, "B", ["OF"], counting={"R": 50, "HR": 10, "RBI": 40, "SB": 5}, rate={"AVG": .250, "OPS": .720}),
        ]
        rankings = self.engine.compute_lookback_rankings(2025, players=players)
        for r in rankings:
            assert len(r.category_contributions) > 0
            assert "HR" in r.category_contributions

    def test_era_lower_is_better(self):
        """Pitcher with lower ERA should get higher z-score for ERA category."""
        pitchers = [
            _make_pitcher(1, "Ace", ["SP"], counting={"IP": 180, "W": 15, "SV": 0, "SO": 200}, rate={"ERA": 2.50, "WHIP": 1.00}),
            _make_pitcher(2, "Average", ["SP"], counting={"IP": 150, "W": 10, "SV": 0, "SO": 140}, rate={"ERA": 4.00, "WHIP": 1.30}),
            _make_pitcher(3, "Bad", ["SP"], counting={"IP": 120, "W": 5, "SV": 0, "SO": 80}, rate={"ERA": 5.50, "WHIP": 1.60}),
        ]
        rankings = self.engine.compute_lookback_rankings(2025, players=pitchers)
        assert rankings[0].name == "Ace"
        # ERA contribution should be positive for the ace (lower ERA = better)
        assert rankings[0].category_contributions["ERA"] > 0

    def test_mixed_batters_and_pitchers(self):
        """Rankings should include both batters and pitchers."""
        players = [
            _make_batter(1, "Batter", ["OF"], counting={"R": 70, "HR": 25, "RBI": 80, "SB": 10}, rate={"AVG": .290, "OPS": .870}),
            _make_pitcher(2, "Pitcher", ["SP"], counting={"IP": 180, "W": 15, "SV": 0, "SO": 220}, rate={"ERA": 2.80, "WHIP": 1.05}),
        ]
        rankings = self.engine.compute_lookback_rankings(2025, players=players)
        assert len(rankings) == 2
        names = {r.name for r in rankings}
        assert "Batter" in names
        assert "Pitcher" in names

    def test_overall_rank_assigned(self):
        """Overall rank should be 1-indexed and contiguous."""
        players = [
            _make_batter(i, f"Player {i}", ["OF"], counting={"R": 50 + i, "HR": 10 + i}, rate={"AVG": .250})
            for i in range(5)
        ]
        rankings = self.engine.compute_lookback_rankings(2025, players=players)
        ranks = [r.overall_rank for r in rankings]
        assert sorted(ranks) == list(range(1, 6))

    def test_empty_players_returns_empty(self):
        rankings = self.engine.compute_lookback_rankings(2025, players=[])
        assert rankings == []

    def test_configurable_categories(self):
        """Engine should only score on configured categories."""
        engine = ScoringEngine(self.adapter, ["HR", "SB"])
        players = [
            _make_batter(1, "Power", ["1B"], counting={"HR": 40, "SB": 0}),
            _make_batter(2, "Speed", ["OF"], counting={"HR": 5, "SB": 40}),
            _make_batter(3, "Both", ["OF"], counting={"HR": 20, "SB": 20}),
        ]
        rankings = engine.compute_lookback_rankings(2025, players=players)
        # With only HR and SB, each player contributes to exactly those categories
        for r in rankings:
            cats = set(r.category_contributions.keys())
            assert cats == {"HR", "SB"}


# --- Predictive Scoring Tests ---


class TestPredictiveScoring:
    def setup_method(self):
        self.adapter = MLBAdapter()
        self.engine = ScoringEngine(
            self.adapter,
            ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"],
        )

    def test_high_xwoba_ranks_higher(self):
        """Batter with better underlying metrics should rank higher predictively."""
        players = [
            _make_batter(
                1, "Unlucky Good", ["OF"],
                advanced={"xwOBA": .380, "xBA": .300, "xSLG": .520, "Barrel%": 15.0, "HardHit%": 45.0, "wRC+": 140, "EV": 92.0, "LD%": 25.0, "Spd": 6.0},
                rate={"BB%": 10.0, "K%": 18.0},
            ),
            _make_batter(
                2, "Lucky Bad", ["OF"],
                advanced={"xwOBA": .300, "xBA": .240, "xSLG": .380, "Barrel%": 5.0, "HardHit%": 30.0, "wRC+": 95, "EV": 86.0, "LD%": 18.0, "Spd": 4.0},
                rate={"BB%": 6.0, "K%": 28.0},
            ),
            _make_batter(
                3, "Average", ["OF"],
                advanced={"xwOBA": .330, "xBA": .265, "xSLG": .440, "Barrel%": 9.0, "HardHit%": 37.0, "wRC+": 110, "EV": 89.0, "LD%": 21.0, "Spd": 5.0},
                rate={"BB%": 8.0, "K%": 22.0},
            ),
        ]
        rankings = self.engine.compute_predictive_rankings(2025, players=players)
        assert rankings[0].name == "Unlucky Good"
        assert rankings[-1].name == "Lucky Bad"

    def test_pitcher_stuff_plus_matters(self):
        """Pitcher with better Stuff+ and xERA should rank higher."""
        pitchers = [
            _make_pitcher(
                1, "Nasty Stuff", ["SP"],
                advanced={"xERA": 2.80, "xFIP": 3.00, "SIERA": 2.90, "Stuff+": 130, "CSW%": 32.0, "K-BB%": 25.0, "SwStr%": 14.0, "GB%": 50.0, "HardHit%": 28.0, "Barrel%": 4.0},
                rate={"K%": 30.0, "BB%": 5.0},
            ),
            _make_pitcher(
                2, "Mediocre", ["SP"],
                advanced={"xERA": 4.50, "xFIP": 4.60, "SIERA": 4.40, "Stuff+": 90, "CSW%": 26.0, "K-BB%": 10.0, "SwStr%": 9.0, "GB%": 42.0, "HardHit%": 40.0, "Barrel%": 10.0},
                rate={"K%": 20.0, "BB%": 10.0},
            ),
        ]
        rankings = self.engine.compute_predictive_rankings(2025, players=pitchers)
        assert rankings[0].name == "Nasty Stuff"

    def test_predictive_has_contributions(self):
        players = [
            _make_batter(
                1, "A", ["OF"],
                advanced={"xwOBA": .350, "Barrel%": 10.0, "HardHit%": 40.0, "wRC+": 120},
                rate={"BB%": 9.0, "K%": 20.0},
            ),
            _make_batter(
                2, "B", ["OF"],
                advanced={"xwOBA": .310, "Barrel%": 6.0, "HardHit%": 33.0, "wRC+": 100},
                rate={"BB%": 7.0, "K%": 24.0},
            ),
        ]
        rankings = self.engine.compute_predictive_rankings(2025, players=players)
        for r in rankings:
            assert len(r.category_contributions) > 0


# --- Positional Scarcity Tests ---


class TestPositionalScarcity:
    def test_catcher_gets_boost(self):
        assert _get_scarcity_multiplier(["C"]) > _get_scarcity_multiplier(["1B"])

    def test_multi_position_takes_highest(self):
        """Player eligible at C and 1B should get the C boost."""
        mult = _get_scarcity_multiplier(["C", "1B"])
        assert mult == _get_scarcity_multiplier(["C"])

    def test_empty_positions_returns_1(self):
        assert _get_scarcity_multiplier([]) == 1.0

    def test_catcher_beats_comparable_1b(self):
        """A catcher and 1B with identical stats — catcher should rank higher."""
        adapter = MLBAdapter()
        engine = ScoringEngine(adapter, ["HR", "AVG"])
        stats = {"HR": 20}
        rate = {"AVG": .270}
        players = [
            _make_batter(1, "Catcher", ["C"], counting=stats, rate=rate),
            _make_batter(2, "First Base", ["1B"], counting=stats, rate=rate),
            _make_batter(3, "Filler", ["OF"], counting={"HR": 10}, rate={"AVG": .250}),
        ]
        rankings = engine.compute_lookback_rankings(2025, players=players)
        catcher_rank = next(r for r in rankings if r.name == "Catcher")
        fb_rank = next(r for r in rankings if r.name == "First Base")
        assert catcher_rank.score > fb_rank.score


# --- Position Rank Tests ---


class TestPositionRanks:
    def test_position_ranks_assigned(self):
        rankings = [
            PlayerRanking(player_id=1, name="A", team="X", positions=["OF"], stat_type="batting", score=10.0),
            PlayerRanking(player_id=2, name="B", team="X", positions=["OF"], stat_type="batting", score=8.0),
            PlayerRanking(player_id=3, name="C", team="X", positions=["SS"], stat_type="batting", score=6.0),
        ]
        _assign_position_ranks(rankings)
        assert rankings[0].position_rank == 1  # OF1
        assert rankings[1].position_rank == 2  # OF2
        assert rankings[2].position_rank == 1  # SS1


# ---------------------------------------------------------------------------
# Tests: ScoringEngine.compute_window_rankings
# ---------------------------------------------------------------------------

WINDOW_CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]


def _make_window_batter(
    player_id: int,
    name: str,
    counting: dict | None = None,
    rate: dict | None = None,
) -> dict:
    return {
        "player_id": player_id,
        "name": name,
        "team": "NYY",
        "positions": ["1B"],
        "stat_type": "batting",
        "counting_stats": counting or {"R": 5, "HR": 2, "RBI": 6, "SB": 0},
        "rate_stats": rate or {"AVG": 0.280},
    }


def _make_window_pitcher(
    player_id: int,
    name: str,
    counting: dict | None = None,
    rate: dict | None = None,
) -> dict:
    return {
        "player_id": player_id,
        "name": name,
        "team": "BOS",
        "positions": ["SP"],
        "stat_type": "pitching",
        "counting_stats": counting or {"W": 1, "SV": 0, "K": 12, "IP": 14.0},
        "rate_stats": rate or {"ERA": 2.80, "WHIP": 1.10},
    }


class TestComputeWindowRankings:
    def setup_method(self) -> None:
        from fantasai.adapters.mlb import MLBAdapter
        self.engine = ScoringEngine(MLBAdapter(), WINDOW_CATEGORIES)

    def test_returns_player_ranking_objects(self) -> None:
        recs = [
            _make_window_batter(1, "Slugger"),
            _make_window_pitcher(2, "Ace"),
        ]
        result = self.engine.compute_window_rankings(recs)
        assert len(result) == 2
        assert all(isinstance(r, PlayerRanking) for r in result)

    def test_sorted_best_first(self) -> None:
        """Better-performing players should rank first."""
        recs = [
            _make_window_batter(1, "Average", counting={"HR": 1, "R": 3, "RBI": 4, "SB": 0}),
            _make_window_batter(2, "Slugger", counting={"HR": 8, "R": 12, "RBI": 14, "SB": 2}),
        ]
        result = self.engine.compute_window_rankings(recs)
        assert result[0].name == "Slugger"
        assert result[0].overall_rank == 1
        assert result[1].name == "Average"
        assert result[1].overall_rank == 2

    def test_assigns_overall_rank(self) -> None:
        recs = [_make_window_batter(i, f"Player{i}") for i in range(1, 6)]
        result = self.engine.compute_window_rankings(recs)
        ranks = sorted(r.overall_rank for r in result)
        assert ranks == [1, 2, 3, 4, 5]

    def test_batters_and_pitchers_ranked_together(self) -> None:
        """Batters and pitchers share the same final rank list."""
        recs = [
            _make_window_batter(1, "Batter"),
            _make_window_pitcher(2, "Pitcher"),
        ]
        result = self.engine.compute_window_rankings(recs)
        stat_types = {r.stat_type for r in result}
        assert "batting" in stat_types
        assert "pitching" in stat_types

    def test_empty_input_returns_empty(self) -> None:
        result = self.engine.compute_window_rankings([])
        assert result == []

    def test_single_player_gets_rank_one(self) -> None:
        result = self.engine.compute_window_rankings([_make_window_batter(1, "Solo")])
        assert result[0].overall_rank == 1
        # Single-player pool: all z-scores are 0 (std=0 guard)
        assert result[0].score == 0.0 or result[0].score == pytest.approx(0.0, abs=0.1)

    def test_category_contributions_populated(self) -> None:
        recs = [
            _make_window_batter(1, "A", counting={"HR": 5, "R": 10, "RBI": 8, "SB": 3}),
            _make_window_batter(2, "B", counting={"HR": 1, "R": 2, "RBI": 2, "SB": 0}),
        ]
        result = self.engine.compute_window_rankings(recs)
        # Player A should have positive HR contribution vs negative for B
        a = next(r for r in result if r.name == "A")
        b = next(r for r in result if r.name == "B")
        assert a.category_contributions.get("HR", 0) > 0
        assert b.category_contributions.get("HR", 0) < 0

    def test_era_inverted_lower_is_better(self) -> None:
        """ERA is lower-is-better — pitcher with lower ERA should score higher."""
        recs = [
            _make_window_pitcher(1, "Ace", rate={"ERA": 1.80, "WHIP": 0.95}),
            _make_window_pitcher(2, "Mediocre", rate={"ERA": 5.40, "WHIP": 1.50}),
        ]
        result = self.engine.compute_window_rankings(recs)
        ace = next(r for r in result if r.name == "Ace")
        mediocre = next(r for r in result if r.name == "Mediocre")
        assert ace.score > mediocre.score
