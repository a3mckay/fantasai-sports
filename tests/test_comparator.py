"""Tests for the player comparison engine.

All tests are pure — no DB, no mocks. We build CompareContext by hand
with synthetic PlayerRanking objects and validate algorithm behavior.
"""
from __future__ import annotations

import pytest

from fantasai.engine.scoring import PlayerRanking
from fantasai.brain.comparator import (
    CompareContext,
    _compute_adjusted_score,
    _parse_context_keywords,
    compare_players,
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


CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]


# ---------------------------------------------------------------------------
# Tests for _parse_context_keywords
# ---------------------------------------------------------------------------


class TestParseContextKeywords:
    def test_stolen_bases_matches_sb(self):
        result = _parse_context_keywords("I need stolen bases", CATEGORIES)
        assert "SB" in result

    def test_saves_matches_sv(self):
        result = _parse_context_keywords("targeting saves and closers", CATEGORIES)
        assert "SV" in result

    def test_multiple_categories(self):
        result = _parse_context_keywords("I need strikeouts and wins", CATEGORIES)
        assert "K" in result
        assert "W" in result

    def test_no_match_returns_empty(self):
        result = _parse_context_keywords("just do your best", CATEGORIES)
        assert len(result) == 0

    def test_category_not_in_league_ignored(self):
        # HLD not in our categories list → should not be matched
        limited_cats = ["R", "HR", "RBI", "SB", "AVG"]
        result = _parse_context_keywords("I need holds", limited_cats)
        assert "HLD" not in result

    def test_case_insensitive(self):
        result = _parse_context_keywords("STOLEN BASES please", CATEGORIES)
        assert "SB" in result


# ---------------------------------------------------------------------------
# Tests for _compute_adjusted_score
# ---------------------------------------------------------------------------


class TestComputeAdjustedScore:
    def test_no_boost_returns_sum_of_contributions(self):
        ranking = _make_ranking(
            1, "Player A", ["OF"], "batting", 3.0,
            {"R": 1.0, "HR": 1.5, "SB": 0.5},
        )
        score, cats = _compute_adjusted_score(ranking, ["R", "HR", "SB"], set())
        assert score == pytest.approx(3.0)
        assert cats["R"] == pytest.approx(1.0)

    def test_boosted_category_doubles_contribution(self):
        ranking = _make_ranking(
            1, "Player A", ["OF"], "batting", 2.0,
            {"R": 1.0, "SB": 1.0},
        )
        score, cats = _compute_adjusted_score(ranking, ["R", "SB"], {"SB"})
        # SB z-score 1.0 boosted to 2.0, R stays 1.0 → total 3.0
        assert score == pytest.approx(3.0)
        assert cats["SB"] == pytest.approx(2.0)
        assert cats["R"] == pytest.approx(1.0)

    def test_missing_category_treated_as_zero(self):
        ranking = _make_ranking(
            1, "Player A", ["OF"], "batting", 0.0,
            {"R": 1.0},
        )
        score, cats = _compute_adjusted_score(ranking, ["R", "HR", "SB"], set())
        assert cats.get("HR", 0.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Tests for compare_players
# ---------------------------------------------------------------------------


class TestComparePlayers:
    def _make_ctx(
        self,
        rankings: list[PlayerRanking],
        context: str | None = None,
    ) -> CompareContext:
        return CompareContext(
            player_rankings=rankings,
            scoring_categories=CATEGORIES,
            context=context,
            ranking_type="predictive",
        )

    def test_basic_ranking_by_score(self):
        r1 = _make_ranking(1, "Elite", ["OF"], "batting", 5.0, {"HR": 3.0, "R": 2.0})
        r2 = _make_ranking(2, "Average", ["OF"], "batting", 2.0, {"HR": 1.0, "R": 1.0})
        ctx = self._make_ctx([r2, r1])  # intentionally unordered

        results = compare_players(ctx)
        assert results[0].player_id == 1
        assert results[1].player_id == 2
        assert results[0].rank == 1
        assert results[1].rank == 2

    def test_context_changes_ranking(self):
        # Player A is better in SB, Player B is better overall
        r_a = _make_ranking(1, "Speedster", ["OF"], "batting", 2.0, {"SB": 2.0, "HR": 0.0})
        r_b = _make_ranking(2, "Slugger", ["1B"], "batting", 3.0, {"SB": 0.0, "HR": 3.0})
        ctx = self._make_ctx([r_a, r_b], context="I need stolen bases")

        results = compare_players(ctx)
        # With 2x SB boost, Speedster's adjusted score = 4.0, Slugger's = 3.0
        assert results[0].player_id == 1

    def test_no_context_ranks_by_raw_contributions(self):
        r1 = _make_ranking(1, "A", ["SP"], "pitching", 4.0, {"K": 2.0, "ERA": 2.0})
        r2 = _make_ranking(2, "B", ["SP"], "pitching", 3.0, {"K": 1.0, "ERA": 2.0})
        ctx = self._make_ctx([r1, r2])

        results = compare_players(ctx)
        assert results[0].player_id == 1

    def test_returns_empty_for_no_players(self):
        ctx = self._make_ctx([])
        assert compare_players(ctx) == []

    def test_single_player_returns_rank_1(self):
        r = _make_ranking(1, "Solo", ["C"], "batting", 1.0, {"HR": 1.0})
        ctx = self._make_ctx([r])
        results = compare_players(ctx)
        assert len(results) == 1
        assert results[0].rank == 1

    def test_category_scores_in_result(self):
        r = _make_ranking(1, "A", ["OF"], "batting", 2.0, {"HR": 1.5, "SB": 0.5})
        ctx = self._make_ctx([r])
        results = compare_players(ctx)
        assert "HR" in results[0].category_scores
        assert results[0].category_scores["HR"] == pytest.approx(1.5)
