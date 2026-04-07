"""Tests for the roster analysis endpoint logic.

Covers:
- Trade difficulty classification (possible / hard / unrealistic)
- Runner-up inclusion when top trade target is unrealistic
- possible/hard sorted before unrealistic
- Per-player category extraction
- Empty-roster guard
- Full API endpoint smoke test via TestClient
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fantasai.engine.scoring import PlayerRanking


# ---------------------------------------------------------------------------
# Helpers
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


# ---------------------------------------------------------------------------
# Unit tests — trade difficulty logic (pure, no DB)
# ---------------------------------------------------------------------------

def _compute_difficulty(
    target: PlayerRanking,
    other_rankings: list[PlayerRanking],
) -> tuple[str, str]:
    """Re-implement the difficulty logic from the endpoint for isolated testing."""
    top_score = max((r.score for r in other_rankings), default=0.0)
    same_pos = [x for x in other_rankings if set(x.positions) & set(target.positions)]
    same_pos_top = max((x.score for x in same_pos), default=0.0)
    pos_label = target.positions[0] if target.positions else "that position"

    if len(same_pos) == 1:
        return "unrealistic", f"Only {pos_label} on their roster"
    elif top_score > 0 and target.score >= top_score * 0.92:
        return "unrealistic", "Their best player overall"
    elif same_pos_top > 0 and target.score >= same_pos_top * 0.92:
        return "hard", f"Best {pos_label} on their roster"
    else:
        n = len(same_pos)
        return "possible", f"Depth at {pos_label} — they have {n} at that spot"


class TestTradeDifficulty:
    def test_only_player_at_position_is_unrealistic(self):
        catcher = _make_ranking(1, "Star C", ["C"], "batting", 2.0)
        other_roster = [
            catcher,
            _make_ranking(2, "SP Ace", ["SP"], "pitching", 3.0),
        ]
        diff, reason = _compute_difficulty(catcher, other_roster)
        assert diff == "unrealistic"
        assert "Only" in reason

    def test_best_overall_player_is_unrealistic(self):
        star = _make_ranking(1, "League Star", ["1B", "OF"], "batting", 5.0)
        other_roster = [
            star,
            _make_ranking(2, "Backup 1B", ["1B"], "batting", 1.0),
            _make_ranking(3, "SP", ["SP"], "pitching", 2.0),
        ]
        diff, _ = _compute_difficulty(star, other_roster)
        assert diff == "unrealistic"

    def test_best_at_position_is_hard(self):
        top_sp = _make_ranking(1, "Top SP", ["SP"], "pitching", 3.0)
        other_roster = [
            top_sp,
            _make_ranking(2, "2nd SP", ["SP"], "pitching", 1.0),
            _make_ranking(3, "Overall Star", ["1B"], "batting", 4.0),
        ]
        diff, _ = _compute_difficulty(top_sp, other_roster)
        assert diff == "hard"

    def test_depth_piece_is_possible(self):
        depth = _make_ranking(2, "Depth SP", ["SP"], "pitching", 1.0)
        other_roster = [
            _make_ranking(1, "Ace SP", ["SP"], "pitching", 3.5),
            depth,
            _make_ranking(3, "3rd SP", ["SP"], "pitching", 0.8),
        ]
        diff, _ = _compute_difficulty(depth, other_roster)
        assert diff == "possible"

    def test_near_top_score_threshold(self):
        """Player scoring exactly 92% of top score gets unrealistic."""
        top = _make_ranking(1, "Star", ["OF"], "batting", 4.0)
        near_top = _make_ranking(2, "Near Top", ["OF"], "batting", 3.68)  # 92% of 4.0
        other_roster = [top, near_top]
        diff, _ = _compute_difficulty(near_top, other_roster)
        assert diff == "unrealistic"


# ---------------------------------------------------------------------------
# Unit tests — per-player top_categories
# ---------------------------------------------------------------------------

class TestTopCategories:
    def test_positive_categories_returned(self):
        r = _make_ranking(
            1, "Power Bat", ["1B"], "batting", 2.0,
            contributions={"HR": 1.5, "RBI": 0.8, "R": 0.3, "SB": -0.2, "AVG": 0.1},
        )
        # Simulate the _top_cats logic from the endpoint
        top = [
            cat for cat, val in sorted(
                r.category_contributions.items(), key=lambda x: x[1], reverse=True
            )
            if val > 0
        ][:3]
        assert top == ["HR", "RBI", "R"]

    def test_no_positive_categories_returns_empty(self):
        r = _make_ranking(
            1, "Bad Player", ["C"], "batting", -0.5,
            contributions={"HR": -0.3, "RBI": -0.1},
        )
        top = [
            cat for cat, val in sorted(
                r.category_contributions.items(), key=lambda x: x[1], reverse=True
            )
            if val > 0
        ][:3]
        assert top == []

    def test_capped_at_three(self):
        r = _make_ranking(
            1, "Multi Cat", ["OF"], "batting", 3.0,
            contributions={"R": 1.0, "HR": 0.9, "RBI": 0.8, "SB": 0.7, "AVG": 0.6},
        )
        top = [
            cat for cat, val in sorted(
                r.category_contributions.items(), key=lambda x: x[1], reverse=True
            )
            if val > 0
        ][:3]
        assert len(top) == 3
        assert top[0] == "R"


# ---------------------------------------------------------------------------
# Unit tests — runner-up logic (when top trade target is unrealistic)
# ---------------------------------------------------------------------------

class TestRunnerUpInclusion:
    """Simulate the per-team candidate collection loop from the endpoint."""

    def _collect_candidates(self, trade_pool_entries: list[dict]) -> list[dict]:
        """Mirror the endpoint's per-team runner-up logic exactly.

        Step 1: iterate by score descending (so each team's #1 is first).
        Step 2: re-sort for display (possible/hard before unrealistic).
        """
        _DIFF_ORDER = {"possible": 0, "hard": 1, "unrealistic": 2}

        # Step 1 — collect per-team, score-order first
        pool = sorted(trade_pool_entries, key=lambda x: -x["score"])
        pos_candidates: list[dict] = []
        seen_teams: dict[int, int] = {}

        for t in pool:
            tid = t["owner_team_id"]
            already = seen_teams.get(tid, 0)
            if already == 0:
                pos_candidates.append(t)
                seen_teams[tid] = 1
            elif already == 1:
                first = next(
                    (c for c in pos_candidates if c["owner_team_id"] == tid), None
                )
                if first and first["difficulty"] == "unrealistic":
                    pos_candidates.append(t)
                    seen_teams[tid] = 2

        # Step 2 — display sort: actionable first, then unrealistic
        pos_candidates.sort(
            key=lambda x: (_DIFF_ORDER.get(x["difficulty"], 9), -x["score"])
        )
        return pos_candidates

    def test_runner_up_included_when_top_is_unrealistic(self):
        """If Team A's best C is unrealistic, their #2 C should also appear."""
        entries = [
            {"player_id": 1, "player_name": "Star C", "positions": ["C"],
             "score": 3.0, "owner_team_id": 10, "owner_team_name": "Team A",
             "difficulty": "unrealistic", "difficulty_reason": "Only C"},
            {"player_id": 2, "player_name": "Backup C", "positions": ["C"],
             "score": 1.2, "owner_team_id": 10, "owner_team_name": "Team A",
             "difficulty": "possible", "difficulty_reason": "Depth"},
        ]
        result = self._collect_candidates(entries)
        ids = [r["player_id"] for r in result]
        assert 1 in ids, "Unrealistic top pick should be included"
        assert 2 in ids, "Runner-up should be included when top is unrealistic"

    def test_runner_up_not_included_when_top_is_hard(self):
        """If Team A's best player is 'hard' (not unrealistic), no runner-up."""
        entries = [
            {"player_id": 1, "player_name": "Good C", "positions": ["C"],
             "score": 2.0, "owner_team_id": 10, "owner_team_name": "Team A",
             "difficulty": "hard", "difficulty_reason": "Best C"},
            {"player_id": 2, "player_name": "Backup C", "positions": ["C"],
             "score": 1.0, "owner_team_id": 10, "owner_team_name": "Team A",
             "difficulty": "possible", "difficulty_reason": "Depth"},
        ]
        result = self._collect_candidates(entries)
        ids = [r["player_id"] for r in result]
        assert 1 in ids
        assert 2 not in ids, "Runner-up should NOT be shown when top is only 'hard'"

    def test_only_one_per_team_when_possible(self):
        """When the best player from a team is possible, show only them."""
        entries = [
            {"player_id": 1, "player_name": "C1", "positions": ["C"],
             "score": 2.0, "owner_team_id": 10, "owner_team_name": "Team A",
             "difficulty": "possible", "difficulty_reason": "Depth"},
            {"player_id": 2, "player_name": "C2", "positions": ["C"],
             "score": 1.5, "owner_team_id": 10, "owner_team_name": "Team A",
             "difficulty": "possible", "difficulty_reason": "Depth"},
        ]
        result = self._collect_candidates(entries)
        assert len([r for r in result if r["owner_team_id"] == 10]) == 1

    def test_actionable_targets_sorted_before_unrealistic(self):
        """possible and hard entries should come before unrealistic in output."""
        entries = [
            {"player_id": 1, "player_name": "Star", "positions": ["C"],
             "score": 4.0, "owner_team_id": 10, "owner_team_name": "A",
             "difficulty": "unrealistic", "difficulty_reason": "Only C"},
            {"player_id": 3, "player_name": "Decent", "positions": ["C"],
             "score": 1.5, "owner_team_id": 20, "owner_team_name": "B",
             "difficulty": "possible", "difficulty_reason": "Depth"},
            {"player_id": 4, "player_name": "Good", "positions": ["C"],
             "score": 2.0, "owner_team_id": 30, "owner_team_name": "C",
             "difficulty": "hard", "difficulty_reason": "Best C"},
        ]
        result = self._collect_candidates(entries)
        difficulties = [r["difficulty"] for r in result]
        # possible and hard must appear before unrealistic
        unrealistic_idx = difficulties.index("unrealistic")
        for i, d in enumerate(difficulties):
            if d in ("possible", "hard"):
                assert i < unrealistic_idx, f"{d} at index {i} should be before unrealistic at {unrealistic_idx}"


# ---------------------------------------------------------------------------
# API smoke test — requires DB fixture
# ---------------------------------------------------------------------------

class TestRosterAnalysisEndpoint:
    def test_empty_roster_returns_404(self, db_client):
        """Team with no rostered players should get a 404 with helpful message."""
        from fantasai.models.league import League, Team

        client, db = db_client

        league = League(
            league_id="test-league-ra",
            platform="yahoo",
            sport="mlb",
            scoring_categories=CATEGORIES,
            roster_positions=["C", "1B", "SP", "BN"],
            league_type="h2h_categories",
            settings={},
        )
        db.add(league)
        db.flush()

        team = Team(
            league_id=league.league_id,
            team_name="Empty Team",
            manager_name="Test Manager",
            roster=[],
        )
        db.add(team)
        db.commit()
        db.refresh(team)

        resp = client.get(f"/api/v1/recommendations/{team.team_id}/roster-analysis")
        assert resp.status_code == 404
        assert "sync" in resp.json()["detail"].lower()

    def test_unknown_team_returns_404(self, db_client):
        client, _ = db_client
        resp = client.get("/api/v1/recommendations/999999/roster-analysis")
        assert resp.status_code == 404
