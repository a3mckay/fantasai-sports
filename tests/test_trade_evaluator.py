"""Tests for the trade evaluator.

All tests are pure — no DB, no mocks. Build TradeContext by hand with
synthetic PlayerRanking objects and validate algorithm behavior.
"""
from __future__ import annotations

import pytest

from fantasai.engine.scoring import PlayerRanking
from fantasai.brain.trade_evaluator import (
    STAR_POWER_WEIGHT,
    TradeContext,
    _adjusted_side_value,
    _compute_category_impact,
    _parse_pros_cons,
    _pick_value,
    evaluate_trade,
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
    contributions: dict[str, float] | None = None,
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
        category_contributions=contributions or {},
    )


CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]


def _make_ctx(
    giving: list[PlayerRanking],
    receiving: list[PlayerRanking],
    giving_picks: list[str] | None = None,
    receiving_picks: list[str] | None = None,
    has_keepers: bool = False,
    player_ages: dict[int, int] | None = None,
) -> TradeContext:
    return TradeContext(
        giving_rankings=giving,
        receiving_rankings=receiving,
        giving_picks=giving_picks or [],
        receiving_picks=receiving_picks or [],
        team_strengths={},
        scoring_categories=CATEGORIES,
        league_type="h2h_categories",
        has_keepers=has_keepers,
        player_ages=player_ages or {},
    )


# ---------------------------------------------------------------------------
# Tests for _pick_value
# ---------------------------------------------------------------------------


class TestPickValue:
    def test_first_round_pick(self):
        assert _pick_value("2025 1st round") == pytest.approx(3.0)

    def test_second_round_pick(self):
        assert _pick_value("2026 2nd round pick") == pytest.approx(1.5)

    def test_third_round_pick(self):
        assert _pick_value("3rd") == pytest.approx(0.5)

    def test_unknown_pick_defaults(self):
        assert _pick_value("competitive balance round") == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Tests for _adjusted_side_value
# ---------------------------------------------------------------------------


class TestAdjustedSideValue:
    def test_single_player_gets_full_star_bonus(self):
        # 1 player at 10 → star_share=1.0 → 10 + 0.4*1.0*10 = 14.0
        result = _adjusted_side_value([10.0])
        assert result == pytest.approx(10.0 + STAR_POWER_WEIGHT * 10.0)

    def test_multiple_equal_players_penalized_vs_single_elite(self):
        # 5 players at 2 → star_share=0.2 → 10 + 0.4*0.2*10 = 10.8
        single = _adjusted_side_value([10.0])
        five_equal = _adjusted_side_value([2.0, 2.0, 2.0, 2.0, 2.0])
        assert single > five_equal

    def test_empty_list_returns_zero(self):
        assert _adjusted_side_value([]) == pytest.approx(0.0)

    def test_all_zero_scores_handled(self):
        # total=0, should not raise; returns 0
        result = _adjusted_side_value([0.0, 0.0])
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Tests for _compute_category_impact
# ---------------------------------------------------------------------------


class TestComputeCategoryImpact:
    def test_positive_impact_when_receiving_is_better(self):
        giving = [_make_ranking(1, "A", ["OF"], "batting", 1.0, {"HR": 1.0, "SB": 0.0})]
        receiving = [_make_ranking(2, "B", ["OF"], "batting", 2.0, {"HR": 3.0, "SB": 0.0})]
        impact = _compute_category_impact(giving, receiving, ["HR", "SB"])
        assert impact["HR"] > 0

    def test_negative_impact_when_giving_is_better(self):
        giving = [_make_ranking(1, "A", ["OF"], "batting", 3.0, {"SB": 3.0})]
        receiving = [_make_ranking(2, "B", ["OF"], "batting", 1.0, {"SB": 0.5})]
        impact = _compute_category_impact(giving, receiving, ["SB"])
        assert impact["SB"] < 0


# ---------------------------------------------------------------------------
# Tests for _parse_pros_cons
# ---------------------------------------------------------------------------


class TestParseProrsCons:
    def test_parses_pros_and_cons_blocks(self):
        text = "[PROS]\n- Better ERA\n- More Ks\n[CONS]\n- Loses saves"
        pros, cons = _parse_pros_cons(text)
        assert "Better ERA" in pros
        assert "More Ks" in pros
        assert "Loses saves" in cons

    def test_missing_blocks_return_empty(self):
        pros, cons = _parse_pros_cons("No structure here at all.")
        assert pros == []
        assert cons == []

    def test_case_insensitive_markers(self):
        text = "[pros]\n- Pro one\n[cons]\n- Con one"
        pros, cons = _parse_pros_cons(text)
        assert len(pros) == 1
        assert len(cons) == 1


# ---------------------------------------------------------------------------
# Tests for evaluate_trade
# ---------------------------------------------------------------------------


class TestEvaluateTrade:
    def test_favor_receive_when_value_strongly_in_favor(self):
        # Receiving a single elite player (score=10) for two weak ones (1+1)
        giving = [
            _make_ranking(1, "Weak A", ["OF"], "batting", 1.0, {"HR": 0.5, "R": 0.5}),
            _make_ranking(2, "Weak B", ["OF"], "batting", 1.0, {"HR": 0.5, "R": 0.5}),
        ]
        receiving = [
            _make_ranking(3, "Star", ["OF"], "batting", 10.0, {"HR": 5.0, "R": 5.0}),
        ]
        ctx = _make_ctx(giving, receiving)
        result = evaluate_trade(ctx)
        assert result.verdict == "favor_receive"

    def test_favor_give_when_giving_away_an_elite_player(self):
        # Giving away a star for depth
        giving = [
            _make_ranking(1, "Star", ["OF"], "batting", 10.0, {"HR": 5.0, "R": 5.0}),
        ]
        receiving = [
            _make_ranking(2, "Filler A", ["1B"], "batting", 2.0, {"HR": 1.0, "R": 1.0}),
            _make_ranking(3, "Filler B", ["2B"], "batting", 2.0, {"HR": 1.0, "R": 1.0}),
            _make_ranking(4, "Filler C", ["SS"], "batting", 2.0, {"HR": 1.0, "R": 1.0}),
        ]
        ctx = _make_ctx(giving, receiving)
        result = evaluate_trade(ctx)
        # Despite equal raw totals (10 vs 6), density adjustment favors giving
        assert result.verdict == "favor_give"

    def test_fair_verdict_for_balanced_trade(self):
        giving = [_make_ranking(1, "A", ["OF"], "batting", 3.0, {"HR": 1.5, "R": 1.5})]
        receiving = [_make_ranking(2, "B", ["OF"], "batting", 3.0, {"HR": 1.5, "R": 1.5})]
        ctx = _make_ctx(giving, receiving)
        result = evaluate_trade(ctx)
        assert result.verdict == "fair"

    def test_draft_picks_add_value(self):
        giving = [_make_ranking(1, "A", ["OF"], "batting", 3.0, {})]
        receiving = [_make_ranking(2, "B", ["OF"], "batting", 1.0, {})]
        # Adding a 1st round pick to receiving side should tip it to favor_receive
        ctx = _make_ctx(giving, receiving, receiving_picks=["2025 1st round"])
        result = evaluate_trade(ctx)
        # receive_raw = 1.0 + 3.0 = 4.0, give_raw = 3.0; diff = 1.0 (may not pass threshold)
        # With density adj for give: 3.0 + 0.4*3.0 = 4.2; receive: adj(1.0) + 3.0 = 4.4
        # Should be close to fair or favor_receive
        assert result.verdict in ("fair", "favor_receive")

    def test_confidence_scales_with_differential(self):
        giving = [_make_ranking(1, "Low", ["OF"], "batting", 1.0, {})]
        receiving = [_make_ranking(2, "High", ["OF"], "batting", 8.0, {})]
        ctx = _make_ctx(giving, receiving)
        result = evaluate_trade(ctx)
        assert result.confidence > 0.5

    def test_returns_non_empty_category_impact(self):
        giving = [_make_ranking(1, "A", ["SP"], "pitching", 3.0, {"K": 2.0, "ERA": 1.0})]
        receiving = [_make_ranking(2, "B", ["OF"], "batting", 3.0, {"HR": 2.0, "R": 1.0})]
        ctx = _make_ctx(giving, receiving)
        result = evaluate_trade(ctx)
        assert isinstance(result.category_impact, dict)
        assert len(result.category_impact) > 0

    def test_keeper_league_boosts_young_player(self):
        # Young player (age 23) in keeper league should have boosted score
        giving = [_make_ranking(1, "Vet", ["OF"], "batting", 5.0, {"HR": 3.0, "R": 2.0})]
        receiving = [_make_ranking(2, "Prospect", ["OF"], "batting", 4.0, {"HR": 2.0, "R": 2.0})]
        ctx_no_keeper = _make_ctx(giving, receiving, has_keepers=False)
        ctx_keeper = _make_ctx(
            giving, receiving,
            has_keepers=True,
            player_ages={2: 23},  # Prospect is 23
        )
        result_no = evaluate_trade(ctx_no_keeper)
        result_yes = evaluate_trade(ctx_keeper)
        # Keeper adjustment should make the receive side more attractive
        assert result_yes.receive_value >= result_no.receive_value
