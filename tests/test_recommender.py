"""Tests for the waiver wire recommender.

All tests are pure — no DB, no mocks. We build WaiverContext by hand
with synthetic PlayerRanking objects and validate algorithm behavior.
"""
from __future__ import annotations

import pytest

from fantasai.engine.scoring import PlayerRanking
from fantasai.brain.recommender import (
    BuildPreferences,
    Recommender,
    WaiverContext,
    _apply_build_preferences,
    _compute_team_strengths,
    _identify_weak_categories,
    _compute_need_weights,
    _get_pitcher_strategy_position_multiplier,
    _player_eligible_for_slot,
    _check_position_fit,
    _score_available_player,
    _find_drop_candidates,
)


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_ranking(
    player_id: int,
    name: str,
    positions: list[str],
    stat_type: str,
    score: float,
    contributions: dict[str, float],
    team: str = "NYY",
) -> PlayerRanking:
    return PlayerRanking(
        player_id=player_id,
        name=name,
        team=team,
        positions=positions,
        stat_type=stat_type,
        overall_rank=0,
        position_rank=0,
        score=score,
        raw_score=score,
        category_contributions=contributions,
    )


def _make_context(
    roster_ids: list[int],
    all_rankings: list[PlayerRanking],
    predictive_rankings: list[PlayerRanking] | None = None,
    all_rostered_ids: set[int] | None = None,
    league_type: str = "h2h_categories",
    scoring_categories: list[str] | None = None,
    roster_positions: list[str] | None = None,
    max_acquisitions: int = 4,
    build_preferences: BuildPreferences | None = None,
) -> WaiverContext:
    if predictive_rankings is None:
        predictive_rankings = all_rankings  # same as lookback by default
    if all_rostered_ids is None:
        all_rostered_ids = set(roster_ids)
    if scoring_categories is None:
        scoring_categories = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]
    if roster_positions is None:
        roster_positions = [
            "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF", "Util", "Util",
            "SP", "SP", "RP", "RP", "P", "P", "P",
            "BN", "BN", "BN", "BN", "BN",
        ]
    return WaiverContext(
        team_id=1,
        roster_player_ids=roster_ids,
        league_type=league_type,
        scoring_categories=scoring_categories,
        roster_positions=roster_positions,
        max_acquisitions_remaining=max_acquisitions,
        all_rankings=all_rankings,
        predictive_rankings=predictive_rankings,
        all_rostered_ids=all_rostered_ids,
        build_preferences=build_preferences,
    )


# ---------------------------------------------------------------------------
# Tests: _compute_team_strengths
# ---------------------------------------------------------------------------


class TestComputeTeamStrengths:
    def test_sums_zscores_correctly(self):
        roster = [
            _make_ranking(1, "A", ["OF"], "batting", 5.0, {"HR": 1.5, "RBI": 0.8}),
            _make_ranking(2, "B", ["OF"], "batting", 3.0, {"HR": 0.5, "RBI": 1.2}),
        ]
        strengths = _compute_team_strengths(roster, ["HR", "RBI", "SB"])
        assert strengths["HR"] == pytest.approx(2.0)
        assert strengths["RBI"] == pytest.approx(2.0)
        assert strengths["SB"] == pytest.approx(0.0)  # neither player has SB

    def test_missing_category_returns_zero(self):
        roster = [
            _make_ranking(1, "A", ["OF"], "batting", 5.0, {"HR": 2.0}),
        ]
        strengths = _compute_team_strengths(roster, ["HR", "AVG"])
        assert strengths["AVG"] == pytest.approx(0.0)

    def test_empty_roster(self):
        strengths = _compute_team_strengths([], ["HR", "RBI"])
        assert strengths["HR"] == pytest.approx(0.0)
        assert strengths["RBI"] == pytest.approx(0.0)

    def test_negative_zscores_sum_correctly(self):
        roster = [
            _make_ranking(1, "A", ["OF"], "batting", 1.0, {"AVG": -0.5}),
            _make_ranking(2, "B", ["OF"], "batting", 1.0, {"AVG": -1.0}),
        ]
        strengths = _compute_team_strengths(roster, ["AVG"])
        assert strengths["AVG"] == pytest.approx(-1.5)


# ---------------------------------------------------------------------------
# Tests: _identify_weak_categories
# ---------------------------------------------------------------------------


class TestIdentifyWeakCategories:
    def test_roto_bottom_third_are_weak(self):
        strengths = {"R": 5.0, "HR": 3.0, "RBI": 1.0, "SB": -1.0, "AVG": 4.0, "OPS": 2.0}
        weak, punted = _identify_weak_categories(strengths, "roto")
        # Bottom third of 6 = 2 categories. SB (-1.0) and RBI (1.0) are weakest.
        assert "SB" in weak
        assert "RBI" in weak
        assert len(weak) == 2
        assert punted == []

    def test_h2h_punt_detection(self):
        strengths = {"R": 5.0, "HR": 3.0, "SB": -4.0, "AVG": 2.0}
        weak, punted = _identify_weak_categories(strengths, "h2h_categories")
        assert "SB" in punted
        assert "SB" not in weak

    def test_h2h_weak_excludes_punted(self):
        strengths = {"R": 5.0, "HR": 3.0, "RBI": 1.0, "SB": -4.0, "AVG": 4.0, "ERA": 2.0}
        weak, punted = _identify_weak_categories(strengths, "h2h_categories")
        assert "SB" in punted
        assert "SB" not in weak
        # Non-punted sorted: RBI(1.0), ERA(2.0), HR(3.0), AVG(4.0), R(5.0)
        # Bottom half of 5 = 2
        assert "RBI" in weak

    def test_empty_strengths(self):
        weak, punted = _identify_weak_categories({}, "roto")
        assert weak == []
        assert punted == []

    def test_single_category(self):
        strengths = {"HR": 2.0}
        weak, punted = _identify_weak_categories(strengths, "roto")
        assert weak == ["HR"]  # bottom third of 1 = 1
        assert punted == []

    def test_all_punted_h2h(self):
        strengths = {"R": -4.0, "HR": -5.0, "SB": -6.0}
        weak, punted = _identify_weak_categories(strengths, "h2h_categories")
        assert len(punted) == 3
        assert weak == []


# ---------------------------------------------------------------------------
# Tests: _compute_need_weights
# ---------------------------------------------------------------------------


class TestComputeNeedWeights:
    def test_weak_cats_get_higher_weight(self):
        strengths = {"HR": 5.0, "RBI": 0.5, "SB": -0.5}
        weights = _compute_need_weights(strengths, weak_categories=["RBI", "SB"], punted_categories=[])
        assert weights["HR"] == 1.0  # strong cat baseline
        assert weights["RBI"] >= 1.5  # weak gets boosted
        assert weights["SB"] >= 1.5

    def test_punted_cats_get_zero_weight(self):
        strengths = {"HR": 5.0, "SB": -4.0}
        weights = _compute_need_weights(strengths, weak_categories=[], punted_categories=["SB"])
        assert weights["SB"] == 0.0
        assert weights["HR"] == 1.0

    def test_strong_cats_get_baseline(self):
        strengths = {"HR": 5.0, "R": 4.0}
        weights = _compute_need_weights(strengths, weak_categories=[], punted_categories=[])
        assert weights["HR"] == 1.0
        assert weights["R"] == 1.0

    def test_weak_weight_caps_at_three(self):
        strengths = {"SB": -10.0}
        weights = _compute_need_weights(strengths, weak_categories=["SB"], punted_categories=[])
        assert weights["SB"] == 3.0  # capped


# ---------------------------------------------------------------------------
# Tests: _player_eligible_for_slot / _check_position_fit
# ---------------------------------------------------------------------------


class TestPositionEligibility:
    def test_exact_position_match(self):
        assert _player_eligible_for_slot(["1B"], "1B") is True
        assert _player_eligible_for_slot(["SS"], "SS") is True

    def test_position_mismatch(self):
        assert _player_eligible_for_slot(["1B"], "SS") is False

    def test_util_accepts_any_hitter(self):
        assert _player_eligible_for_slot(["OF"], "Util") is True
        assert _player_eligible_for_slot(["C"], "Util") is True
        assert _player_eligible_for_slot(["1B"], "Util") is True

    def test_util_rejects_pitchers(self):
        assert _player_eligible_for_slot(["SP"], "Util") is False
        assert _player_eligible_for_slot(["RP"], "Util") is False

    def test_p_slot_accepts_sp_and_rp(self):
        assert _player_eligible_for_slot(["SP"], "P") is True
        assert _player_eligible_for_slot(["RP"], "P") is True

    def test_p_slot_rejects_hitters(self):
        assert _player_eligible_for_slot(["OF"], "P") is False

    def test_of_slot_accepts_of_positions(self):
        assert _player_eligible_for_slot(["LF"], "OF") is True
        assert _player_eligible_for_slot(["CF"], "OF") is True
        assert _player_eligible_for_slot(["RF"], "OF") is True
        assert _player_eligible_for_slot(["OF"], "OF") is True

    def test_bench_accepts_anyone(self):
        assert _player_eligible_for_slot(["SP"], "BN") is True
        assert _player_eligible_for_slot(["OF"], "BN") is True
        assert _player_eligible_for_slot(["C"], "BN") is True

    def test_pitcher_cannot_fill_hitter_slot(self):
        assert _player_eligible_for_slot(["SP"], "1B") is False
        assert _player_eligible_for_slot(["RP"], "OF") is False

    def test_multi_position_player(self):
        assert _player_eligible_for_slot(["1B", "OF"], "1B") is True
        assert _player_eligible_for_slot(["1B", "OF"], "OF") is True
        assert _player_eligible_for_slot(["1B", "OF"], "Util") is True
        assert _player_eligible_for_slot(["1B", "OF"], "SS") is False


class TestCheckPositionFit:
    def test_returns_fillable_slots(self):
        roster = ["C", "1B", "SS", "OF", "Util", "SP", "RP", "P", "BN"]
        fills = _check_position_fit(["OF"], roster)
        assert "OF" in fills
        assert "Util" in fills
        assert "BN" in fills
        assert "SP" not in fills

    def test_pitcher_fills_pitcher_slots(self):
        roster = ["SP", "RP", "P", "BN"]
        fills = _check_position_fit(["SP"], roster)
        assert "SP" in fills
        assert "P" in fills
        assert "BN" in fills
        assert "RP" not in fills

    def test_deduplicates_slots(self):
        roster = ["OF", "OF", "OF", "BN", "BN"]
        fills = _check_position_fit(["OF"], roster)
        assert fills.count("OF") == 1
        assert fills.count("BN") == 1


# ---------------------------------------------------------------------------
# Tests: _score_available_player
# ---------------------------------------------------------------------------


class TestScoreAvailablePlayer:
    def test_blends_lookback_and_predictive(self):
        lb = _make_ranking(1, "A", ["OF"], "batting", 5.0, {"HR": 2.0, "RBI": 1.0})
        pr = _make_ranking(1, "A", ["OF"], "batting", 7.0, {"HR": 3.0, "RBI": 2.0})
        weights = {"HR": 1.0, "RBI": 1.0}
        score, impact = _score_available_player(lb, pr, weights)
        # lookback_score = 2.0*1 + 1.0*1 = 3.0
        # predictive_score = 3.0*1 + 2.0*1 = 5.0
        # blended = 0.3 * 3.0 + 0.7 * 5.0 = 0.9 + 3.5 = 4.4
        assert score == pytest.approx(4.4)

    def test_predictive_only(self):
        pr = _make_ranking(1, "A", ["OF"], "batting", 7.0, {"HR": 3.0})
        weights = {"HR": 1.0}
        score, impact = _score_available_player(None, pr, weights)
        # lookback = 0, predictive = 3.0 * 1.0 = 3.0
        # blended = 0.3 * 0 + 0.7 * 3.0 = 2.1
        assert score == pytest.approx(2.1)

    def test_lookback_only(self):
        lb = _make_ranking(1, "A", ["OF"], "batting", 5.0, {"HR": 2.0})
        weights = {"HR": 1.0}
        score, impact = _score_available_player(lb, None, weights)
        assert score == pytest.approx(0.6)  # 0.3 * 2.0 = 0.6

    def test_category_impact_is_blended(self):
        lb = _make_ranking(1, "A", ["OF"], "batting", 5.0, {"HR": 2.0})
        pr = _make_ranking(1, "A", ["OF"], "batting", 7.0, {"HR": 4.0})
        weights = {"HR": 1.0}
        _, impact = _score_available_player(lb, pr, weights)
        # HR impact = 0.3 * 2.0 + 0.7 * 4.0 = 0.6 + 2.8 = 3.4
        assert impact["HR"] == pytest.approx(3.4)

    def test_need_weighting_amplifies_weak_cats(self):
        lb = _make_ranking(1, "A", ["OF"], "batting", 5.0, {"HR": 1.0, "SB": 1.0})
        weights = {"HR": 1.0, "SB": 2.5}  # SB is a weak category
        score, _ = _score_available_player(lb, lb, weights)
        # Each cat contribution: HR = 1.0*1.0=1.0, SB = 1.0*2.5=2.5
        # Total need-weighted = 3.5 for both lookback and predictive
        # Blended = 0.3*3.5 + 0.7*3.5 = 3.5
        assert score == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# Tests: _find_drop_candidates
# ---------------------------------------------------------------------------


class TestFindDropCandidates:
    def test_weakest_player_suggested_first(self):
        roster = [
            _make_ranking(1, "Star", ["OF"], "batting", 8.0, {"HR": 2.0, "RBI": 2.0}),
            _make_ranking(2, "OK Guy", ["1B"], "batting", 3.0, {"HR": 0.5, "RBI": 0.5}),
            _make_ranking(3, "Scrub", ["OF"], "batting", 1.0, {"HR": -0.5, "RBI": -0.5}),
        ]
        need_weights = {"HR": 1.0, "RBI": 1.0}
        drops = _find_drop_candidates(
            roster, add_player_score=5.0, add_player_contributions={},
            need_weights=need_weights,
            roster_positions=["OF", "OF", "1B", "Util", "BN"],
        )
        assert drops[0].player_name == "Scrub"

    def test_cannot_drop_only_catcher_when_add_is_not_catcher(self):
        roster = [
            _make_ranking(1, "Only Catcher", ["C"], "batting", 1.0, {"HR": -0.5}),
            _make_ranking(2, "OF Guy", ["OF"], "batting", 2.0, {"HR": 0.5}),
        ]
        need_weights = {"HR": 1.0}
        drops = _find_drop_candidates(
            roster, add_player_score=5.0, add_player_contributions={},
            need_weights=need_weights,
            roster_positions=["C", "OF", "BN"],
            add_player_positions=["OF"],  # adding an OF, not a C
        )
        # Only Catcher should be protected — only OF Guy is droppable
        droppable_names = [d.player_name for d in drops]
        assert "Only Catcher" not in droppable_names
        assert "OF Guy" in droppable_names

    def test_can_drop_only_catcher_when_add_is_catcher(self):
        roster = [
            _make_ranking(1, "Only Catcher", ["C"], "batting", 1.0, {"HR": -0.5}),
            _make_ranking(2, "OF Guy", ["OF"], "batting", 2.0, {"HR": 0.5}),
        ]
        need_weights = {"HR": 1.0}
        drops = _find_drop_candidates(
            roster, add_player_score=5.0, add_player_contributions={},
            need_weights=need_weights,
            roster_positions=["C", "OF", "BN"],
            add_player_positions=["C"],  # adding a C, so dropping C is safe
        )
        droppable_names = [d.player_name for d in drops]
        assert "Only Catcher" in droppable_names

    def test_net_impact_calculated_correctly(self):
        roster = [
            _make_ranking(1, "Drop Me", ["OF"], "batting", 1.0, {"HR": 0.5}),
        ]
        need_weights = {"HR": 1.0}
        drops = _find_drop_candidates(
            roster, add_player_score=5.0, add_player_contributions={},
            need_weights=need_weights,
            roster_positions=["OF", "BN"],
            add_player_positions=["OF"],  # incoming player covers the OF slot
        )
        assert len(drops) == 1
        # net_impact = add_score(5.0) - drop_contribution(0.5 * 1.0 = 0.5) = 4.5
        assert drops[0].net_impact == pytest.approx(4.5)

    def test_empty_roster_returns_empty(self):
        drops = _find_drop_candidates(
            [], add_player_score=5.0, add_player_contributions={},
            need_weights={"HR": 1.0},
            roster_positions=["OF"],
        )
        assert drops == []

    def test_respects_max_candidates(self):
        roster = [
            _make_ranking(i, f"Player {i}", ["OF"], "batting", float(i), {"HR": float(i) * 0.1})
            for i in range(10)
        ]
        drops = _find_drop_candidates(
            roster, add_player_score=10.0, add_player_contributions={},
            need_weights={"HR": 1.0},
            roster_positions=["OF", "OF", "OF", "BN", "BN", "BN", "BN", "BN", "BN", "BN"],
            max_candidates=3,
        )
        assert len(drops) <= 3


# ---------------------------------------------------------------------------
# Tests: Recommender.get_waiver_recommendations (integration)
# ---------------------------------------------------------------------------


class TestWaiverRecommendations:
    def _build_standard_scenario(self):
        """Build a standard test scenario with a team and available players."""
        # My roster: 3 hitters, 2 pitchers
        roster_hitters = [
            _make_ranking(1, "My 1B", ["1B"], "batting", 4.0,
                          {"R": 1.0, "HR": 1.5, "RBI": 1.0, "SB": -0.5, "AVG": 0.5}),
            _make_ranking(2, "My OF", ["OF"], "batting", 3.0,
                          {"R": 0.5, "HR": 0.5, "RBI": 0.5, "SB": 0.0, "AVG": 1.0}),
            _make_ranking(3, "My SS", ["SS"], "batting", 2.0,
                          {"R": 0.0, "HR": -0.5, "RBI": 0.0, "SB": 1.0, "AVG": 0.5}),
        ]
        roster_pitchers = [
            _make_ranking(4, "My SP", ["SP"], "pitching", 5.0,
                          {"W": 1.0, "SV": 0.0, "K": 1.5, "ERA": 1.0, "WHIP": 0.8}),
            _make_ranking(5, "My RP", ["RP"], "pitching", 3.0,
                          {"W": 0.0, "SV": 1.0, "K": 0.5, "ERA": 0.5, "WHIP": 0.5}),
        ]

        # Available players (not on any team)
        available = [
            _make_ranking(101, "SB Specialist", ["OF"], "batting", 6.0,
                          {"R": 0.5, "HR": -0.5, "RBI": -0.5, "SB": 3.0, "AVG": 0.5}),
            _make_ranking(102, "Power Hitter", ["1B", "OF"], "batting", 7.0,
                          {"R": 1.0, "HR": 2.5, "RBI": 2.0, "SB": -1.0, "AVG": -0.5}),
            _make_ranking(103, "Ace SP", ["SP"], "pitching", 8.0,
                          {"W": 2.0, "SV": 0.0, "K": 2.5, "ERA": 1.5, "WHIP": 1.2}),
            _make_ranking(104, "Closer", ["RP"], "pitching", 4.0,
                          {"W": 0.0, "SV": 2.5, "K": 0.5, "ERA": 0.5, "WHIP": 0.5}),
        ]

        all_rankings = roster_hitters + roster_pitchers + available
        roster_ids = [1, 2, 3, 4, 5]
        all_rostered = {1, 2, 3, 4, 5}

        categories = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]
        return roster_ids, all_rankings, all_rostered, categories

    def test_returns_limited_results(self):
        roster_ids, all_rankings, all_rostered, cats = self._build_standard_scenario()
        context = _make_context(roster_ids, all_rankings, all_rankings, all_rostered,
                                scoring_categories=cats)
        rec = Recommender(cats)
        results = rec.get_waiver_recommendations(context, limit=2)
        assert len(results) <= 2

    def test_respects_max_acquisitions_zero(self):
        roster_ids, all_rankings, all_rostered, cats = self._build_standard_scenario()
        context = _make_context(roster_ids, all_rankings, all_rankings, all_rostered,
                                scoring_categories=cats, max_acquisitions=0)
        rec = Recommender(cats)
        results = rec.get_waiver_recommendations(context)
        assert results == []

    def test_recommendations_ordered_by_priority(self):
        roster_ids, all_rankings, all_rostered, cats = self._build_standard_scenario()
        context = _make_context(roster_ids, all_rankings, all_rankings, all_rostered,
                                scoring_categories=cats)
        rec = Recommender(cats)
        results = rec.get_waiver_recommendations(context)
        for i in range(len(results) - 1):
            assert results[i].priority_score >= results[i + 1].priority_score

    def test_excludes_already_rostered_players(self):
        roster_ids, all_rankings, all_rostered, cats = self._build_standard_scenario()
        context = _make_context(roster_ids, all_rankings, all_rankings, all_rostered,
                                scoring_categories=cats)
        rec = Recommender(cats)
        results = rec.get_waiver_recommendations(context)
        rostered_ids = set(roster_ids)
        for r in results:
            assert r.player_id not in rostered_ids

    def test_includes_drop_suggestion(self):
        roster_ids, all_rankings, all_rostered, cats = self._build_standard_scenario()
        context = _make_context(roster_ids, all_rankings, all_rankings, all_rostered,
                                scoring_categories=cats)
        rec = Recommender(cats)
        results = rec.get_waiver_recommendations(context)
        # At least one recommendation should have a drop candidate
        has_drops = any(len(r.drop_candidates) > 0 for r in results)
        assert has_drops

    def test_action_string_format(self):
        roster_ids, all_rankings, all_rostered, cats = self._build_standard_scenario()
        context = _make_context(roster_ids, all_rankings, all_rankings, all_rostered,
                                scoring_categories=cats)
        rec = Recommender(cats)
        results = rec.get_waiver_recommendations(context)
        for r in results:
            assert r.action.startswith("Add ")
            assert "(" in r.action  # contains position

    def test_no_available_players_returns_empty(self):
        roster_ids, all_rankings, _, cats = self._build_standard_scenario()
        # Mark ALL players as rostered
        all_rostered = {r.player_id for r in all_rankings}
        context = _make_context(roster_ids, all_rankings, all_rankings, all_rostered,
                                scoring_categories=cats)
        rec = Recommender(cats)
        results = rec.get_waiver_recommendations(context)
        assert results == []

    def test_blend_favors_predictive(self):
        """Player with good predictive stats should rank higher than one with only good lookback."""
        roster = [
            _make_ranking(1, "My Guy", ["OF"], "batting", 2.0,
                          {"HR": 0.0, "RBI": 0.0}),
        ]
        # Player A: great lookback, bad predictive
        a_lookback = _make_ranking(101, "Past Hero", ["OF"], "batting", 8.0,
                                   {"HR": 3.0, "RBI": 3.0})
        a_predictive = _make_ranking(101, "Past Hero", ["OF"], "batting", 1.0,
                                     {"HR": -1.0, "RBI": -1.0})
        # Player B: bad lookback, great predictive
        b_lookback = _make_ranking(102, "Future Star", ["OF"], "batting", 1.0,
                                   {"HR": -1.0, "RBI": -1.0})
        b_predictive = _make_ranking(102, "Future Star", ["OF"], "batting", 8.0,
                                     {"HR": 3.0, "RBI": 3.0})

        all_lb = roster + [a_lookback, b_lookback]
        all_pr = roster + [a_predictive, b_predictive]
        context = _make_context(
            [1], all_lb, all_pr, {1},
            scoring_categories=["HR", "RBI"],
        )
        rec = Recommender(["HR", "RBI"])
        results = rec.get_waiver_recommendations(context)
        # Future Star should rank higher due to 70% predictive weighting
        assert results[0].player_name == "Future Star"

    def test_multi_position_player_gets_bonus(self):
        """Multi-position player should score higher than single-position with same stats."""
        roster = [
            _make_ranking(1, "My Guy", ["OF"], "batting", 2.0, {"HR": 0.0}),
        ]
        single = _make_ranking(101, "Single Pos", ["1B"], "batting", 5.0, {"HR": 2.0})
        multi = _make_ranking(102, "Multi Pos", ["1B", "OF"], "batting", 5.0, {"HR": 2.0})

        all_rankings = roster + [single, multi]
        context = _make_context(
            [1], all_rankings, all_rankings, {1},
            scoring_categories=["HR"],
        )
        rec = Recommender(["HR"])
        results = rec.get_waiver_recommendations(context)
        # Multi-position should rank first due to flexibility bonus
        assert results[0].player_name == "Multi Pos"

    def test_addresses_weak_categories(self):
        """Recommendations should list which weak categories the player helps."""
        # Team is weak in SB
        roster = [
            _make_ranking(1, "Power Only", ["OF"], "batting", 5.0,
                          {"HR": 2.0, "SB": -1.0}),
        ]
        avail = _make_ranking(101, "Speed Guy", ["OF"], "batting", 4.0,
                              {"HR": -0.5, "SB": 3.0})

        all_rankings = roster + [avail]
        context = _make_context(
            [1], all_rankings, all_rankings, {1},
            scoring_categories=["HR", "SB"],
        )
        rec = Recommender(["HR", "SB"])
        results = rec.get_waiver_recommendations(context)
        assert len(results) >= 1
        assert "SB" in results[0].weak_categories_addressed

    def test_roto_league_type(self):
        """Roto leagues should not have punted categories."""
        roster_ids, all_rankings, all_rostered, cats = self._build_standard_scenario()
        context = _make_context(roster_ids, all_rankings, all_rankings, all_rostered,
                                scoring_categories=cats, league_type="roto")
        rec = Recommender(cats, league_type="roto")
        results = rec.get_waiver_recommendations(context)
        # Should still return recommendations
        assert len(results) > 0


# ---------------------------------------------------------------------------
# Tests: _apply_build_preferences
# ---------------------------------------------------------------------------


class TestApplyBuildPreferences:
    def test_punt_categories_zeroes_weight(self):
        weights = {"HR": 1.5, "SB": 2.0, "AVG": 1.0}
        weak = ["SB", "AVG"]
        punted = []
        prefs = BuildPreferences(punt_categories=["SB"])
        weights, weak, punted = _apply_build_preferences(weights, weak, punted, prefs)
        assert weights["SB"] == 0.0
        assert "SB" in punted
        assert "SB" not in weak

    def test_punt_categories_merged_with_autodetect(self):
        weights = {"HR": 1.0, "SB": 0.0, "AVG": 1.0}
        weak = ["AVG"]
        punted = ["SB"]  # auto-detected
        prefs = BuildPreferences(punt_categories=["AVG"])  # user also punts AVG
        weights, weak, punted = _apply_build_preferences(weights, weak, punted, prefs)
        assert weights["AVG"] == 0.0
        assert "SB" in punted
        assert "AVG" in punted
        assert "AVG" not in weak

    def test_pitcher_strategy_rp_heavy_boosts_sv(self):
        weights = {"SV": 1.0, "W": 1.0, "K": 1.0}
        prefs = BuildPreferences(pitcher_strategy="rp_heavy")
        weights, _, _ = _apply_build_preferences(weights, [], [], prefs)
        assert weights["SV"] == pytest.approx(1.5)
        assert weights["W"] == pytest.approx(0.5)
        assert weights["K"] == pytest.approx(1.0)  # unchanged

    def test_pitcher_strategy_sp_heavy_boosts_wins(self):
        weights = {"SV": 1.0, "W": 1.0, "K": 1.0, "QS": 1.0}
        prefs = BuildPreferences(pitcher_strategy="sp_heavy")
        weights, _, _ = _apply_build_preferences(weights, [], [], prefs)
        assert weights["W"] == pytest.approx(1.3)
        assert weights["K"] == pytest.approx(1.3)
        assert weights["SV"] == pytest.approx(0.5)

    def test_priority_targets_multiplies_weight(self):
        weights = {"SV": 1.0, "K": 1.0}
        prefs = BuildPreferences(priority_targets=["SV"])
        weights, _, _ = _apply_build_preferences(weights, [], [], prefs)
        assert weights["SV"] == pytest.approx(1.5)
        assert weights["K"] == pytest.approx(1.0)

    def test_priority_stacks_with_weak(self):
        weights = {"SB": 2.0}  # already boosted as weak cat
        prefs = BuildPreferences(priority_targets=["SB"])
        weights, _, _ = _apply_build_preferences(weights, ["SB"], [], prefs)
        assert weights["SB"] == pytest.approx(3.0)  # 2.0 * 1.5

    def test_balanced_strategy_no_change(self):
        weights = {"SV": 1.0, "W": 1.0}
        prefs = BuildPreferences(pitcher_strategy="balanced")
        weights, _, _ = _apply_build_preferences(weights, [], [], prefs)
        assert weights["SV"] == pytest.approx(1.0)
        assert weights["W"] == pytest.approx(1.0)

    def test_default_preferences_no_change(self):
        weights = {"HR": 1.5, "SB": 2.0}
        prefs = BuildPreferences()  # all defaults
        original = dict(weights)
        weights, _, _ = _apply_build_preferences(weights, ["SB"], [], prefs)
        assert weights == original


class TestPitcherStrategyPositionMultiplier:
    def test_rp_heavy_boosts_rp(self):
        prefs = BuildPreferences(pitcher_strategy="rp_heavy")
        assert _get_pitcher_strategy_position_multiplier(["RP"], prefs) == pytest.approx(1.10)

    def test_rp_heavy_penalizes_sp(self):
        prefs = BuildPreferences(pitcher_strategy="rp_heavy")
        assert _get_pitcher_strategy_position_multiplier(["SP"], prefs) == pytest.approx(0.90)

    def test_sp_heavy_boosts_sp(self):
        prefs = BuildPreferences(pitcher_strategy="sp_heavy")
        assert _get_pitcher_strategy_position_multiplier(["SP"], prefs) == pytest.approx(1.10)

    def test_balanced_no_change(self):
        prefs = BuildPreferences(pitcher_strategy="balanced")
        assert _get_pitcher_strategy_position_multiplier(["SP"], prefs) == pytest.approx(1.0)

    def test_no_prefs_no_change(self):
        assert _get_pitcher_strategy_position_multiplier(["SP"], None) == pytest.approx(1.0)

    def test_hitter_unaffected(self):
        prefs = BuildPreferences(pitcher_strategy="rp_heavy")
        assert _get_pitcher_strategy_position_multiplier(["OF"], prefs) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tests: Build Preferences integration with full recommender
# ---------------------------------------------------------------------------


class TestBuildPreferencesIntegration:
    def _build_pitcher_scenario(self):
        """Build scenario with SP and RP available players."""
        roster = [
            _make_ranking(1, "My OF", ["OF"], "batting", 3.0,
                          {"HR": 1.0, "SV": 0.0, "W": 0.0, "K": 0.0}),
            _make_ranking(2, "My RP", ["RP"], "pitching", 3.0,
                          {"HR": 0.0, "SV": 1.0, "W": 0.0, "K": 0.5}),
        ]
        avail_sp = _make_ranking(101, "Volume SP", ["SP"], "pitching", 5.0,
                                 {"HR": 0.0, "SV": 0.0, "W": 2.0, "K": 2.0})
        avail_rp = _make_ranking(102, "Elite Closer", ["RP"], "pitching", 5.0,
                                 {"HR": 0.0, "SV": 2.0, "W": 0.0, "K": 2.0})

        all_rankings = roster + [avail_sp, avail_rp]
        return [1, 2], all_rankings, {1, 2}

    def test_rp_heavy_prefers_closer_over_sp(self):
        roster_ids, all_rankings, rostered = self._build_pitcher_scenario()
        prefs = BuildPreferences(pitcher_strategy="rp_heavy")
        context = _make_context(
            roster_ids, all_rankings, all_rankings, rostered,
            scoring_categories=["HR", "SV", "W", "K"],
            build_preferences=prefs,
        )
        rec = Recommender(["HR", "SV", "W", "K"])
        results = rec.get_waiver_recommendations(context)
        assert len(results) >= 2
        assert results[0].player_name == "Elite Closer"

    def test_sp_heavy_prefers_sp_over_closer(self):
        roster_ids, all_rankings, rostered = self._build_pitcher_scenario()
        prefs = BuildPreferences(pitcher_strategy="sp_heavy")
        context = _make_context(
            roster_ids, all_rankings, all_rankings, rostered,
            scoring_categories=["HR", "SV", "W", "K"],
            build_preferences=prefs,
        )
        rec = Recommender(["HR", "SV", "W", "K"])
        results = rec.get_waiver_recommendations(context)
        assert len(results) >= 2
        assert results[0].player_name == "Volume SP"

    def test_punt_position_filters_catchers(self):
        """Punt C position should exclude catcher-only players from recs."""
        roster = [
            _make_ranking(1, "My OF", ["OF"], "batting", 3.0, {"HR": 1.0}),
        ]
        avail_c = _make_ranking(101, "Good Catcher", ["C"], "batting", 6.0, {"HR": 2.0})
        avail_of = _make_ranking(102, "Good OF", ["OF"], "batting", 5.0, {"HR": 1.5})

        all_rankings = roster + [avail_c, avail_of]
        prefs = BuildPreferences(punt_positions=["C"])
        context = _make_context(
            [1], all_rankings, all_rankings, {1},
            scoring_categories=["HR"],
            build_preferences=prefs,
        )
        rec = Recommender(["HR"])
        results = rec.get_waiver_recommendations(context)
        rec_names = [r.player_name for r in results]
        assert "Good Catcher" not in rec_names
        assert "Good OF" in rec_names

    def test_punt_position_allows_multi_position(self):
        """Player with C + 1B should NOT be filtered when punting C."""
        roster = [
            _make_ranking(1, "My OF", ["OF"], "batting", 3.0, {"HR": 1.0}),
        ]
        avail = _make_ranking(101, "C/1B Guy", ["C", "1B"], "batting", 6.0, {"HR": 2.0})

        all_rankings = roster + [avail]
        prefs = BuildPreferences(punt_positions=["C"])
        context = _make_context(
            [1], all_rankings, all_rankings, {1},
            scoring_categories=["HR"],
            build_preferences=prefs,
        )
        rec = Recommender(["HR"])
        results = rec.get_waiver_recommendations(context)
        assert any(r.player_name == "C/1B Guy" for r in results)

    def test_punt_position_removes_drop_protection(self):
        """With punt C, the sole catcher should be droppable."""
        roster = [
            _make_ranking(1, "Bad Catcher", ["C"], "batting", 0.5, {"HR": -1.0}),
            _make_ranking(2, "Good OF", ["OF"], "batting", 5.0, {"HR": 2.0}),
        ]
        avail = _make_ranking(101, "Great OF", ["OF"], "batting", 8.0, {"HR": 3.0})

        all_rankings = roster + [avail]
        prefs = BuildPreferences(punt_positions=["C"])
        context = _make_context(
            [1, 2], all_rankings, all_rankings, {1, 2},
            scoring_categories=["HR"],
            roster_positions=["C", "OF", "Util", "BN"],
            build_preferences=prefs,
        )
        rec = Recommender(["HR"])
        results = rec.get_waiver_recommendations(context)
        assert len(results) >= 1
        drop_names = [d.player_name for d in results[0].drop_candidates]
        assert "Bad Catcher" in drop_names

    def test_none_preferences_same_as_no_preferences(self):
        """build_preferences=None should produce same results as omitting it."""
        roster = [_make_ranking(1, "My OF", ["OF"], "batting", 3.0, {"HR": 1.0})]
        avail = _make_ranking(101, "Good OF", ["OF"], "batting", 6.0, {"HR": 2.0})
        all_rankings = roster + [avail]

        context_none = _make_context([1], all_rankings, all_rankings, {1},
                                     scoring_categories=["HR"])
        context_none.build_preferences = None

        context_default = _make_context([1], all_rankings, all_rankings, {1},
                                        scoring_categories=["HR"])
        context_default.build_preferences = BuildPreferences()

        rec = Recommender(["HR"])
        results_none = rec.get_waiver_recommendations(context_none)
        results_default = rec.get_waiver_recommendations(context_default)

        assert len(results_none) == len(results_default)
        if results_none:
            assert results_none[0].priority_score == results_default[0].priority_score


# ---------------------------------------------------------------------------
# Tests: pitcher floor / IP constraint
# ---------------------------------------------------------------------------


class TestPitcherFloor:
    """Drop candidate pitcher-floor checks — hard floor (count) + soft floor (IP)."""

    def _sp(self, pid: int, name: str, score: float) -> "PlayerRanking":
        return _make_ranking(pid, name, ["SP"], "pitching", score, {"ERA": -0.5, "K": 0.5})

    def _rp(self, pid: int, name: str, score: float) -> "PlayerRanking":
        return _make_ranking(pid, name, ["RP"], "pitching", score, {"SV": 1.0})

    def test_hard_floor_skips_drop_below_min_pitchers(self):
        """Dropping the only pitcher below MIN_ROSTER_PITCHERS should be skipped."""
        # 2 pitchers on roster — dropping one would leave 1 (below floor of 3)
        roster = [
            self._sp(1, "SP One", 3.0),
            self._sp(2, "SP Two", 2.0),
            _make_ranking(3, "Good OF", ["OF"], "batting", 5.0, {"HR": 1.0}),
        ]
        # Adding another hitter, not a pitcher
        drops = _find_drop_candidates(
            roster, add_player_score=6.0, add_player_contributions={},
            need_weights={"ERA": 1.0, "K": 1.0},
            roster_positions=["SP", "SP", "OF", "BN"],
            add_player_positions=["OF"],
        )
        drop_names = [d.player_name for d in drops]
        # Both pitchers should be skipped (would leave 0 or 1 pitcher, below floor=3)
        assert "SP One" not in drop_names
        assert "SP Two" not in drop_names

    def test_hard_floor_allows_drop_when_enough_pitchers_remain(self):
        """Dropping a pitcher is OK when MIN_ROSTER_PITCHERS would still be met."""
        roster = [
            self._sp(1, "SP One", 3.0),
            self._sp(2, "SP Two", 2.0),
            self._sp(3, "SP Three", 1.0),  # weakest SP
            self._rp(4, "RP One", 4.0),
            _make_ranking(5, "OF Guy", ["OF"], "batting", 5.0, {"HR": 1.0}),
        ]
        # 4 pitchers; dropping the weakest leaves 3 (exactly MIN_ROSTER_PITCHERS)
        drops = _find_drop_candidates(
            roster, add_player_score=6.0, add_player_contributions={},
            need_weights={"SV": 1.0},
            roster_positions=["SP", "SP", "SP", "RP", "OF", "BN"],
            add_player_positions=["OF"],
        )
        drop_names = [d.player_name for d in drops]
        # The weakest SP (SP Three, score=1.0) should be allowed
        assert "SP Three" in drop_names

    def test_hard_floor_incoming_pitcher_counts(self):
        """If the incoming add is a pitcher, they offset the count."""
        # 3 pitchers on roster, adding a 4th pitcher — dropping one still leaves 3
        roster = [
            self._sp(1, "SP One", 3.0),
            self._sp(2, "SP Two", 2.0),
            self._sp(3, "SP Three", 1.0),
        ]
        drops = _find_drop_candidates(
            roster, add_player_score=8.0, add_player_contributions={},
            need_weights={"K": 1.0},
            roster_positions=["SP", "SP", "SP", "BN"],
            add_player_positions=["SP"],  # incoming is also a pitcher
        )
        # SP Three (worst) should be a valid drop — incoming SP replaces them
        drop_names = [d.player_name for d in drops]
        assert "SP Three" in drop_names

    def test_soft_floor_ip_warning_when_below_threshold(self):
        """When team_pitcher_ip data is present, warn if remaining IP < MIN_WEEKLY_IP."""
        roster = [
            self._sp(1, "SP One", 3.0),
            self._sp(2, "SP Two", 2.0),
            self._sp(3, "SP Three", 1.0),
            self._rp(4, "RP One", 4.0),
        ]
        # Dropping SP Three (pid=3) with ~5 IP would leave 3+4=7 IP → below floor (15.0)
        team_pitcher_ip = {1: 7.0, 2: 3.0, 3: 5.0, 4: 4.0}  # total 14 IP without SP Three
        drops = _find_drop_candidates(
            roster, add_player_score=6.0, add_player_contributions={},
            need_weights={"SV": 1.0},
            roster_positions=["SP", "SP", "SP", "RP", "BN"],
            add_player_positions=["OF"],
            team_pitcher_ip=team_pitcher_ip,
        )
        sp_three = next((d for d in drops if d.player_name == "SP Three"), None)
        assert sp_three is not None
        assert sp_three.ip_warning is not None
        assert "IP" in sp_three.ip_warning

    def test_soft_floor_no_warning_when_ip_above_threshold(self):
        """No warning when remaining IP after drop is >= MIN_WEEKLY_IP."""
        roster = [
            self._sp(1, "SP One", 3.0),
            self._sp(2, "SP Two", 2.0),
            self._sp(3, "SP Three", 1.0),
            self._rp(4, "RP One", 4.0),
        ]
        # Remaining IP after dropping SP Three (pid=3) = 20 + 18 + 10 = 48 IP → above floor
        team_pitcher_ip = {1: 20.0, 2: 18.0, 3: 10.0, 4: 15.0}
        drops = _find_drop_candidates(
            roster, add_player_score=6.0, add_player_contributions={},
            need_weights={"SV": 1.0},
            roster_positions=["SP", "SP", "SP", "RP", "BN"],
            add_player_positions=["OF"],
            team_pitcher_ip=team_pitcher_ip,
        )
        sp_three = next((d for d in drops if d.player_name == "SP Three"), None)
        assert sp_three is not None
        assert sp_three.ip_warning is None

    def test_no_ip_warning_for_hitter_drop(self):
        """ip_warning is never set when dropping a hitter."""
        roster = [
            _make_ranking(1, "OF Guy", ["OF"], "batting", 2.0, {"HR": 0.5}),
            self._sp(2, "SP One", 3.0),
            self._sp(3, "SP Two", 3.5),
            self._sp(4, "SP Three", 4.0),
        ]
        team_pitcher_ip = {2: 5.0, 3: 5.0, 4: 5.0}
        drops = _find_drop_candidates(
            roster, add_player_score=6.0, add_player_contributions={},
            need_weights={"HR": 1.0},
            roster_positions=["OF", "SP", "SP", "SP", "BN"],
            add_player_positions=["OF"],
            team_pitcher_ip=team_pitcher_ip,
        )
        of_drop = next((d for d in drops if d.player_name == "OF Guy"), None)
        assert of_drop is not None
        assert of_drop.ip_warning is None
