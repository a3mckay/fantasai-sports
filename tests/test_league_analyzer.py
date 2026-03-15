"""Tests for the league analyzer (team comparisons + power rankings).

All tests are pure — no DB, no mocks.
"""
from __future__ import annotations

import pytest

from fantasai.engine.scoring import PlayerRanking
from fantasai.brain.league_analyzer import (
    TeamSnapshot,
    _build_team_snapshot,
    _detect_trade_opportunity,
    compare_teams,
    compute_league_power,
)


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]


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


def _make_team(
    team_id: int,
    team_name: str,
    player_configs: list[tuple[int, str, list[str], str, float, dict]],
) -> tuple[int, str, list[PlayerRanking]]:
    rankings = [
        _make_ranking(pid, name, pos, st, score, contribs)
        for pid, name, pos, st, score, contribs in player_configs
    ]
    return team_id, team_name, rankings


# ---------------------------------------------------------------------------
# Tests for _build_team_snapshot
# ---------------------------------------------------------------------------


class TestBuildTeamSnapshot:
    def test_power_score_is_sum_of_player_scores(self):
        roster = [
            _make_ranking(1, "A", ["OF"], "batting", 2.0),
            _make_ranking(2, "B", ["SP"], "pitching", 3.0),
        ]
        snap = _build_team_snapshot(1, "Team A", roster, CATEGORIES, "h2h_categories")
        assert snap.power_score == pytest.approx(5.0)

    def test_top_players_are_top_3(self):
        roster = [
            _make_ranking(1, "Best", ["OF"], "batting", 5.0),
            _make_ranking(2, "Second", ["1B"], "batting", 3.0),
            _make_ranking(3, "Third", ["SP"], "pitching", 2.0),
            _make_ranking(4, "Worst", ["RP"], "pitching", 0.5),
        ]
        snap = _build_team_snapshot(1, "T", roster, CATEGORIES, "h2h_categories")
        assert snap.top_players[0] == "Best"
        assert len(snap.top_players) == 3

    def test_empty_roster_returns_zero_power(self):
        snap = _build_team_snapshot(1, "Empty", [], CATEGORIES, "h2h_categories")
        assert snap.power_score == 0.0
        assert snap.top_players == []


# ---------------------------------------------------------------------------
# Tests for _detect_trade_opportunity
# ---------------------------------------------------------------------------


class TestDetectTradeOpportunity:
    def test_detects_complementary_trade(self):
        # Team A is strong in SV, weak in SB. Team B is strong in SB, weak in SV.
        snap_a = TeamSnapshot(
            team_id=1, team_name="A", power_score=10.0,
            category_strengths={"SV": 2.0, "SB": -1.5, "HR": 0.5},
            strong_cats=["SV"], weak_cats=["SB"], top_players=[],
        )
        snap_b = TeamSnapshot(
            team_id=2, team_name="B", power_score=10.0,
            category_strengths={"SV": -1.5, "SB": 2.0, "HR": 0.5},
            strong_cats=["SB"], weak_cats=["SV"], top_players=[],
        )
        rankings_a = [_make_ranking(1, "Closer", ["RP"], "pitching", 3.0, {"SV": 3.0})]
        rankings_b = [_make_ranking(2, "Speedster", ["OF"], "batting", 3.0, {"SB": 3.0})]

        opp = _detect_trade_opportunity(snap_a, snap_b, rankings_a, rankings_b)
        assert opp is not None
        assert "SV" in opp.team_a_gives_cats
        assert "SB" in opp.team_b_gives_cats

    def test_no_trade_when_no_overlap(self):
        # Both teams strong in the same categories
        snap_a = TeamSnapshot(
            1, "A", 10.0, {"HR": 2.0, "SB": 2.0}, ["HR", "SB"], [], [],
        )
        snap_b = TeamSnapshot(
            2, "B", 10.0, {"HR": 2.0, "SB": 2.0}, ["HR", "SB"], [], [],
        )
        opp = _detect_trade_opportunity(snap_a, snap_b, [], [])
        assert opp is None

    def test_suggested_players_populated(self):
        snap_a = TeamSnapshot(
            1, "A", 10.0, {"SV": 2.5, "ERA": 2.0, "SB": -1.0}, ["SV", "ERA"], ["SB"], [],
        )
        snap_b = TeamSnapshot(
            2, "B", 10.0, {"SB": 2.5, "R": 2.0, "SV": -1.0}, ["SB", "R"], ["SV"], [],
        )
        ra = [_make_ranking(1, "Closer A", ["RP"], "pitching", 4.0, {"SV": 4.0})]
        rb = [_make_ranking(2, "Speedster B", ["OF"], "batting", 3.0, {"SB": 3.0})]

        opp = _detect_trade_opportunity(snap_a, snap_b, ra, rb)
        assert opp is not None
        assert opp.suggested_give == "Closer A"
        assert opp.suggested_receive == "Speedster B"


# ---------------------------------------------------------------------------
# Tests for compare_teams
# ---------------------------------------------------------------------------


class TestCompareTeams:
    def test_winner_has_highest_power_score(self):
        team_a = _make_team(1, "Strong", [
            (1, "Star", ["OF"], "batting", 8.0, {"HR": 5.0, "R": 3.0}),
        ])
        team_b = _make_team(2, "Weak", [
            (2, "Average", ["1B"], "batting", 2.0, {"HR": 1.0, "R": 1.0}),
        ])
        result = compare_teams([team_a, team_b], CATEGORIES, "h2h_categories")
        assert result.winner == 1

    def test_snapshots_sorted_by_power_desc(self):
        team_a = _make_team(1, "A", [(1, "X", ["OF"], "batting", 8.0, {})])
        team_b = _make_team(2, "B", [(2, "Y", ["1B"], "batting", 3.0, {})])
        team_c = _make_team(3, "C", [(3, "Z", ["SP"], "pitching", 5.0, {})])
        result = compare_teams([team_a, team_b, team_c], CATEGORIES, "h2h_categories")
        scores = [s.power_score for s in result.snapshots]
        assert scores == sorted(scores, reverse=True)

    def test_returns_empty_for_no_teams(self):
        result = compare_teams([], CATEGORIES, "h2h_categories")
        assert result.snapshots == []
        assert result.winner == -1

    def test_include_trades_false_skips_detection(self):
        team_a = _make_team(1, "A", [
            (1, "Closer", ["RP"], "pitching", 3.0, {"SV": 3.0, "SB": -1.5}),
        ])
        team_b = _make_team(2, "B", [
            (2, "Speed", ["OF"], "batting", 3.0, {"SB": 3.0, "SV": -1.5}),
        ])
        result = compare_teams([team_a, team_b], CATEGORIES, "h2h_categories", include_trades=False)
        assert result.trade_opportunities == []


# ---------------------------------------------------------------------------
# Tests for compute_league_power
# ---------------------------------------------------------------------------


class TestComputeLeaguePower:
    def _make_league(self) -> list[tuple[int, str, list[PlayerRanking]]]:
        return [
            _make_team(i, f"Team {i}", [(i * 10 + 1, f"Player {i}", ["OF"], "batting", float(i), {})])
            for i in range(1, 7)  # 6 teams
        ]

    def test_power_rankings_ordered_best_first(self):
        league = self._make_league()
        report = compute_league_power(league, CATEGORIES, "h2h_categories")
        scores = [s.power_score for s in report.power_rankings]
        assert scores == sorted(scores, reverse=True)

    def test_tiers_cover_all_teams(self):
        league = self._make_league()
        report = compute_league_power(league, CATEGORIES, "h2h_categories")
        all_ids = set()
        for ids in report.tiers.values():
            all_ids.update(ids)
        league_ids = {tid for tid, _, _ in league}
        assert all_ids == league_ids

    def test_three_tiers_returned(self):
        league = self._make_league()
        report = compute_league_power(league, CATEGORIES, "h2h_categories")
        assert set(report.tiers.keys()) == {"contender", "middle", "rebuilding"}

    def test_contender_tier_has_highest_power(self):
        league = self._make_league()
        report = compute_league_power(league, CATEGORIES, "h2h_categories")
        contender_ids = set(report.tiers["contender"])
        contender_snaps = [s for s in report.power_rankings if s.team_id in contender_ids]
        other_snaps = [s for s in report.power_rankings if s.team_id not in contender_ids]
        if contender_snaps and other_snaps:
            min_contender = min(s.power_score for s in contender_snaps)
            max_other = max(s.power_score for s in other_snaps)
            assert min_contender >= max_other

    def test_empty_league_returns_empty_report(self):
        report = compute_league_power([], CATEGORIES, "h2h_categories")
        assert report.power_rankings == []
        assert report.tiers == {}
