"""Tests for the scoring engine."""
from __future__ import annotations

import pytest

from fantasai.adapters.base import NormalizedPlayerData
from fantasai.adapters.mlb import MLBAdapter
from fantasai.engine.projection import (
    HORIZON_CONFIGS,
    ProjectionHorizon,
    project_hitter_stats,
    project_pitcher_stats,
)
from fantasai.engine.scoring import (
    ScoringEngine,
    PlayerRanking,
    _get_scarcity_multiplier,
    _assign_position_ranks,
)


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


# ---------------------------------------------------------------------------
# Projection model tests
# ---------------------------------------------------------------------------


class TestProjectionHitterStats:
    def _batter(self, **adv) -> NormalizedPlayerData:
        return _make_batter(
            1, "Test Hitter", ["OF"],
            counting={"PA": 600, "HR": 30, "SB": 20, "BB": 70, "H": 160},
            rate={"AVG": 0.280, "OBP": 0.370, "SLG": 0.510, "BB%": 0.12, "K%": 0.22},
            advanced={"xBA": 0.275, "xwOBA": 0.380, "xSLG": 0.500,
                      "Barrel%": 10.0, "Spd": 6.0, **adv},
        )

    def test_counting_stats_scale_with_pa(self) -> None:
        """Doubling PA should roughly double all counting stats."""
        p = self._batter()
        week = project_hitter_stats(p, HORIZON_CONFIGS[ProjectionHorizon.WEEK])
        season = project_hitter_stats(p, HORIZON_CONFIGS[ProjectionHorizon.SEASON])
        ratio = HORIZON_CONFIGS[ProjectionHorizon.SEASON].hitter_pa / HORIZON_CONFIGS[ProjectionHorizon.WEEK].hitter_pa
        # HR, SB, BB should all scale proportionally (within 20% tolerance)
        for stat in ("HR", "SB", "BB"):
            assert abs(season[stat] / week[stat] - ratio) < ratio * 0.20, \
                f"{stat}: season={season[stat]:.2f}, week={week[stat]:.2f}, expected ratio ~{ratio:.1f}"

    def test_rate_stats_not_scaled_by_pa(self) -> None:
        """Rate stats (AVG, OBP, SLG) must stay in normal baseball ranges,
        not get multiplied by PA.  They legitimately differ across horizons
        because the talent/actual blend ratio differs (WEEK=35% talent vs
        SEASON=85% talent), but the values should always be sensible decimals."""
        p = self._batter()
        for hz in ProjectionHorizon:
            proj = project_hitter_stats(p, HORIZON_CONFIGS[hz])
            for stat in ("AVG", "OBP", "SLG"):
                assert 0.100 <= proj[stat] <= 1.000, \
                    f"{stat} at {hz} out of range: {proj[stat]}"
            # OPS can exceed 1.0 for elite hitters; just verify it's sane
            assert 0.300 <= proj["OPS"] <= 2.000

    def test_ops_equals_obp_plus_slg(self) -> None:
        p = self._batter()
        proj = project_hitter_stats(p, HORIZON_CONFIGS[ProjectionHorizon.SEASON])
        assert abs(proj["OPS"] - (proj["OBP"] + proj["SLG"])) < 1e-9

    def test_missing_advanced_stats_falls_back_to_actual(self) -> None:
        """Player with no xBA/xwOBA should fall back to actual AVG/OBP."""
        p = _make_batter(
            1, "No Advanced", ["1B"],
            counting={"PA": 500, "HR": 15, "SB": 5, "BB": 45, "H": 140},
            rate={"AVG": 0.260, "OBP": 0.330, "SLG": 0.420},
            advanced={},  # no advanced stats at all
        )
        proj = project_hitter_stats(p, HORIZON_CONFIGS[ProjectionHorizon.SEASON])
        assert abs(proj["AVG"] - 0.260) < 0.01
        assert proj["HR"] > 0

    def test_talent_weight_shifts_toward_advanced_metrics_for_season(self) -> None:
        """At SEASON horizon (85% talent), xBA should dominate over actual AVG."""
        p = _make_batter(
            1, "Regression Candidate", ["3B"],
            counting={"PA": 400, "HR": 10, "SB": 5, "BB": 30, "H": 120},
            rate={"AVG": 0.320, "OBP": 0.360, "SLG": 0.420},  # lucky actual
            advanced={"xBA": 0.250, "xwOBA": 0.310, "xSLG": 0.380,
                      "Barrel%": 5.0, "Spd": 4.0},  # talent says he'll regress
        )
        proj_season = project_hitter_stats(p, HORIZON_CONFIGS[ProjectionHorizon.SEASON])
        proj_week = project_hitter_stats(p, HORIZON_CONFIGS[ProjectionHorizon.WEEK])
        # Season projection should be closer to xBA (0.250); week closer to actual (0.320)
        assert proj_season["AVG"] < proj_week["AVG"]
        assert abs(proj_season["AVG"] - 0.250) < abs(proj_week["AVG"] - 0.250)


class TestProjectionPitcherStats:
    def _sp(self, **adv) -> NormalizedPlayerData:
        return _make_pitcher(
            2, "Test Starter", ["SP"],
            counting={"IP": 180.0, "W": 15, "SV": 0, "HLD": 0, "SO": 200},
            rate={"ERA": 3.20, "WHIP": 1.10, "K/9": 10.0, "BB/9": 2.5},
            advanced={"xERA": 3.10, "SIERA": 3.15, "xFIP": 3.20,
                      "SwStr%": 0.13, **adv},
        )

    def _rp(self, **adv) -> NormalizedPlayerData:
        return _make_pitcher(
            3, "Test Closer", ["RP"],
            counting={"IP": 65.0, "W": 3, "SV": 38, "HLD": 0, "SO": 85},
            rate={"ERA": 1.80, "WHIP": 0.85, "K/9": 11.8, "BB/9": 2.2},
            advanced={"xERA": 2.00, "SIERA": 2.10, "xFIP": 2.20,
                      "SwStr%": 0.16, **adv},
        )

    def test_rp_projected_ip_bounded_at_rp_config(self) -> None:
        """Reliever IP should equal config.rp_ip, not SP level."""
        rp = self._rp()
        proj = project_pitcher_stats(rp, HORIZON_CONFIGS[ProjectionHorizon.SEASON], is_sp=False)
        assert proj["IP"] == HORIZON_CONFIGS[ProjectionHorizon.SEASON].rp_ip

    def test_sp_projected_ip_at_sp_config(self) -> None:
        sp = self._sp()
        proj = project_pitcher_stats(sp, HORIZON_CONFIGS[ProjectionHorizon.SEASON], is_sp=True)
        assert proj["IP"] == HORIZON_CONFIGS[ProjectionHorizon.SEASON].sp_ip

    def test_qs_only_for_sp(self) -> None:
        sp = self._sp()
        rp = self._rp()
        sp_proj = project_pitcher_stats(sp, HORIZON_CONFIGS[ProjectionHorizon.SEASON], is_sp=True)
        rp_proj = project_pitcher_stats(rp, HORIZON_CONFIGS[ProjectionHorizon.SEASON], is_sp=False)
        assert sp_proj["QS"] > 0
        assert rp_proj["QS"] == 0.0

    def test_k_scales_with_ip(self) -> None:
        """Strikeout totals should be proportional to IP across horizons."""
        sp = self._sp()
        week_proj = project_pitcher_stats(sp, HORIZON_CONFIGS[ProjectionHorizon.WEEK], is_sp=True)
        season_proj = project_pitcher_stats(sp, HORIZON_CONFIGS[ProjectionHorizon.SEASON], is_sp=True)
        ip_ratio = HORIZON_CONFIGS[ProjectionHorizon.SEASON].sp_ip / HORIZON_CONFIGS[ProjectionHorizon.WEEK].sp_ip
        k_ratio = season_proj["K"] / week_proj["K"]
        assert abs(k_ratio - ip_ratio) < ip_ratio * 0.05  # within 5%

    def test_missing_advanced_falls_back_gracefully(self) -> None:
        p = _make_pitcher(
            4, "No Advanced", ["SP"],
            counting={"IP": 150.0, "W": 10, "SV": 0, "HLD": 0, "SO": 150},
            rate={"ERA": 4.00, "WHIP": 1.35, "K/9": 9.0, "BB/9": 3.0},
            advanced={},
        )
        proj = project_pitcher_stats(p, HORIZON_CONFIGS[ProjectionHorizon.SEASON], is_sp=True)
        assert proj["ERA"] == pytest.approx(4.00, rel=0.15)
        assert proj["K"] > 0

    def test_era_blend_weighted_toward_talent_for_season(self) -> None:
        """At SEASON horizon, xERA/SIERA should pull ERA away from lucky actual."""
        lucky = _make_pitcher(
            5, "Lucky ERA", ["SP"],
            counting={"IP": 160.0, "W": 12, "SV": 0, "HLD": 0, "SO": 160},
            rate={"ERA": 2.50, "WHIP": 1.05, "K/9": 9.0, "BB/9": 3.0},
            advanced={"xERA": 3.80, "SIERA": 3.90, "xFIP": 3.75, "SwStr%": 0.10},
        )
        proj_season = project_pitcher_stats(lucky, HORIZON_CONFIGS[ProjectionHorizon.SEASON], is_sp=True)
        proj_week = project_pitcher_stats(lucky, HORIZON_CONFIGS[ProjectionHorizon.WEEK], is_sp=True)
        # Season ERA should be closer to xERA (3.80); week ERA closer to actual (2.50)
        assert proj_season["ERA"] > proj_week["ERA"]


class TestPredictiveRankingsWithProjection:
    def setup_method(self):
        self.adapter = MLBAdapter()
        self.categories = ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"]
        self.engine = ScoringEngine(self.adapter, self.categories)

    def _elite_hitter(self, pid: int, name: str) -> NormalizedPlayerData:
        return _make_batter(
            pid, name, ["OF"],
            counting={"PA": 640, "HR": 42, "SB": 18, "BB": 90, "H": 175, "R": 115, "RBI": 105},
            rate={"AVG": 0.295, "OBP": 0.400, "SLG": 0.580, "BB%": 0.14, "K%": 0.20},
            advanced={"xBA": 0.290, "xwOBA": 0.420, "xSLG": 0.570,
                      "Barrel%": 14.0, "Spd": 5.5, "wRC+": 165},
        )

    def _elite_closer(self, pid: int, name: str) -> NormalizedPlayerData:
        return _make_pitcher(
            pid, name, ["RP"],
            counting={"IP": 65.0, "W": 3, "SV": 40, "HLD": 0, "SO": 88},
            rate={"ERA": 1.75, "WHIP": 0.80, "K/9": 12.2, "BB/9": 2.1},
            advanced={"xERA": 1.90, "SIERA": 2.00, "xFIP": 2.10,
                      "SwStr%": 0.18, "K-BB%": 0.32, "Stuff+": 145},
        )

    def _good_sp(self, pid: int, name: str) -> NormalizedPlayerData:
        return _make_pitcher(
            pid, name, ["SP"],
            counting={"IP": 195.0, "W": 16, "SV": 0, "HLD": 0, "SO": 220},
            rate={"ERA": 2.80, "WHIP": 1.00, "K/9": 10.2, "BB/9": 2.0},
            advanced={"xERA": 2.90, "SIERA": 2.95, "xFIP": 3.00,
                      "SwStr%": 0.145, "K-BB%": 0.28, "Stuff+": 135},
        )

    def _make_pool(self) -> list[NormalizedPlayerData]:
        """Realistic mixed pool: 4 elite hitters, 2 elite closers, 2 elite SPs,
        and a collection of average players to anchor z-scores."""
        players = []
        # Elite hitters
        for i, (name, extra) in enumerate([
            ("Soto", {"xwOBA": 0.440, "Barrel%": 16.0}),
            ("Acuna", {"xwOBA": 0.420, "Barrel%": 14.0, "Spd": 8.0}),
            ("Tucker", {"xwOBA": 0.400, "Barrel%": 12.0}),
            ("Ramirez", {"xwOBA": 0.375, "Barrel%": 9.0, "Spd": 6.5}),
        ]):
            base = self._elite_hitter(100 + i, name)
            base.advanced_stats.update(extra)
            players.append(base)
        # Elite closers
        for i, name in enumerate(["Miller", "Diaz"]):
            players.append(self._elite_closer(200 + i, name))
        # Elite SPs
        for i, name in enumerate(["Skenes", "Cole"]):
            players.append(self._good_sp(300 + i, name))
        # Average players (15 batters + 8 SPs + 5 RPs) to anchor distributions
        for i in range(15):
            players.append(_make_batter(
                400 + i, f"Avg Batter {i}", ["OF"],
                counting={"PA": 500, "HR": 20, "SB": 8, "BB": 50, "H": 130},
                rate={"AVG": 0.255, "OBP": 0.320, "SLG": 0.420, "BB%": 0.10, "K%": 0.23},
                advanced={"xBA": 0.252, "xwOBA": 0.330, "xSLG": 0.415,
                          "Barrel%": 7.0, "Spd": 4.5},
            ))
        for i in range(8):
            players.append(_make_pitcher(
                500 + i, f"Avg SP {i}", ["SP"],
                counting={"IP": 165.0, "W": 11, "SV": 0, "HLD": 0, "SO": 170},
                rate={"ERA": 4.10, "WHIP": 1.28, "K/9": 9.3, "BB/9": 3.0},
                advanced={"xERA": 4.00, "SIERA": 4.05, "xFIP": 4.10, "SwStr%": 0.10},
            ))
        for i in range(5):
            players.append(_make_pitcher(
                600 + i, f"Avg RP {i}", ["RP"],
                counting={"IP": 60.0, "W": 4, "SV": 10, "HLD": 5, "SO": 65},
                rate={"ERA": 3.80, "WHIP": 1.20, "K/9": 9.8, "BB/9": 3.2},
                advanced={"xERA": 3.70, "SIERA": 3.75, "xFIP": 3.80, "SwStr%": 0.11},
            ))
        return players

    def test_elite_hitter_beats_elite_closer_season(self) -> None:
        """An elite 6-category hitter should outscore an elite closer at SEASON horizon."""
        players = self._make_pool()
        rankings = self.engine.compute_predictive_rankings(
            2025, players=players, horizon=ProjectionHorizon.SEASON
        )
        by_name = {r.name: r for r in rankings}
        # Soto should beat Miller
        assert by_name["Soto"].score > by_name["Miller"].score, (
            f"Soto score={by_name['Soto'].score:.3f} should > Miller score={by_name['Miller'].score:.3f}"
        )

    def test_no_rp_in_top_4_season(self) -> None:
        """No reliever should be in the top 4 at full-season horizon.

        The new three-component formula can rank elite closers in top 5 when
        their Statcast profile is outstanding (elite ERA/SIERA/xERA + elite SwStr%).
        The top 4 should still be dominated by elite multi-category hitters and SPs.
        """
        players = self._make_pool()
        rankings = self.engine.compute_predictive_rankings(
            2025, players=players, horizon=ProjectionHorizon.SEASON
        )
        top4 = rankings[:4]
        for r in top4:
            assert "RP" not in r.positions, \
                f"Reliever {r.name} (rank {r.overall_rank}) should not be in top 4"

    def test_generational_sp_beats_avg_rp(self) -> None:
        """An elite SP (Skenes) should rank above any average reliever."""
        players = self._make_pool()
        rankings = self.engine.compute_predictive_rankings(
            2025, players=players, horizon=ProjectionHorizon.SEASON
        )
        by_name = {r.name: r for r in rankings}
        avg_rp_scores = [r.score for r in rankings if r.name.startswith("Avg RP")]
        assert by_name["Skenes"].score > max(avg_rp_scores)

    def test_season_rp_rank_better_than_week(self) -> None:
        """Elite relievers should rank higher at SEASON than at WEEK.

        The Rest-of-Season formula uses a Statcast composite component (35% weight)
        that explicitly rewards elite stuff (xERA/SIERA/SwStr%) independent of IP
        volume.  At WEEK, the old projection formula applies, which is more IP-driven.
        An elite closer therefore gets a meaningful Statcast boost at SEASON that it
        doesn't get at WEEK, so its SEASON rank should be better (lower number).
        """
        players = self._make_pool()
        week_rankings = self.engine.compute_predictive_rankings(
            2025, players=players, horizon=ProjectionHorizon.WEEK
        )
        season_rankings = self.engine.compute_predictive_rankings(
            2025, players=players, horizon=ProjectionHorizon.SEASON
        )
        miller_week = next(r for r in week_rankings if r.name == "Miller").overall_rank
        miller_season = next(r for r in season_rankings if r.name == "Miller").overall_rank
        assert miller_season <= miller_week, (
            f"Miller SEASON rank ({miller_season}) should be \u2264 WEEK rank ({miller_week}): "
            f"Statcast component in the RoS formula rewards elite closer stuff"
        )

    def test_lookback_unchanged(self) -> None:
        """Lookback rankings should not be affected by this change — baseline check."""
        players = self._make_pool()
        rankings = self.engine.compute_lookback_rankings(2025, players=players)
        assert len(rankings) == len(players)
        assert rankings[0].overall_rank == 1
        assert rankings[-1].overall_rank == len(players)
