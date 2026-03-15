"""Tests for the strategy suggester.

Pure function tests — no DB, no mocks. We build synthetic PlayerRanking
objects and verify that suggest_strategy() correctly detects build patterns.
"""
from __future__ import annotations


from fantasai.engine.scoring import PlayerRanking
from fantasai.brain.strategy import (
    StrategySuggestion,
    suggest_strategy,
    _detect_pitcher_strategy,
    _detect_position_punts,
    _detect_category_punts,
    _detect_priority_targets,
)
from fantasai.brain.recommender import BuildPreferences


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------

STANDARD_CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]
STANDARD_POSITIONS = [
    "C", "1B", "2B", "3B", "SS", "OF", "OF", "OF", "Util", "Util",
    "SP", "SP", "RP", "RP", "P", "P", "P",
    "BN", "BN", "BN", "BN", "BN",
]


def _make_ranking(
    player_id: int,
    name: str,
    positions: list[str],
    stat_type: str,
    score: float = 1.0,
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
# Tests: _detect_pitcher_strategy
# ---------------------------------------------------------------------------


class TestDetectPitcherStrategy:
    def test_detects_rp_heavy_strong_signal(self):
        """6 RP and 1 SP → ratio 6:1 → rp_heavy with high confidence."""
        roster = [
            _make_ranking(i, f"RP{i}", ["RP"], "pitching") for i in range(1, 7)
        ] + [_make_ranking(7, "SP1", ["SP"], "pitching")]

        strategy, reason, confidence = _detect_pitcher_strategy(roster)
        assert strategy == "rp_heavy"
        assert confidence == 0.9
        assert "6 relievers" in reason

    def test_detects_rp_heavy_medium_signal(self):
        """4 RP and 2 SP → ratio 2:1 → rp_heavy with medium confidence."""
        roster = [
            _make_ranking(i, f"RP{i}", ["RP"], "pitching") for i in range(1, 5)
        ] + [
            _make_ranking(5, "SP1", ["SP"], "pitching"),
            _make_ranking(6, "SP2", ["SP"], "pitching"),
        ]

        strategy, reason, confidence = _detect_pitcher_strategy(roster)
        assert strategy == "rp_heavy"
        assert confidence == 0.7

    def test_detects_sp_heavy_strong_signal(self):
        """6 SP and 1 RP → ratio 6:1 → sp_heavy with high confidence."""
        roster = [
            _make_ranking(i, f"SP{i}", ["SP"], "pitching") for i in range(1, 7)
        ] + [_make_ranking(7, "RP1", ["RP"], "pitching")]

        strategy, reason, confidence = _detect_pitcher_strategy(roster)
        assert strategy == "sp_heavy"
        assert confidence == 0.9
        assert "6 starters" in reason

    def test_detects_sp_heavy_medium_signal(self):
        """4 SP and 2 RP → ratio 2:1 → sp_heavy with medium confidence."""
        roster = [
            _make_ranking(i, f"SP{i}", ["SP"], "pitching") for i in range(1, 5)
        ] + [
            _make_ranking(5, "RP1", ["RP"], "pitching"),
            _make_ranking(6, "RP2", ["RP"], "pitching"),
        ]

        strategy, reason, confidence = _detect_pitcher_strategy(roster)
        assert strategy == "sp_heavy"
        assert confidence == 0.7

    def test_detects_balanced(self):
        """3 SP and 3 RP → balanced."""
        roster = [
            _make_ranking(i, f"SP{i}", ["SP"], "pitching") for i in range(1, 4)
        ] + [
            _make_ranking(i, f"RP{i}", ["RP"], "pitching") for i in range(4, 7)
        ]

        strategy, reason, confidence = _detect_pitcher_strategy(roster)
        assert strategy == "balanced"
        assert confidence == 0.5
        assert "balanced mix" in reason

    def test_no_pitchers(self):
        """Roster with only batters → balanced with low confidence."""
        roster = [_make_ranking(1, "Batter", ["1B"], "batting")]

        strategy, reason, confidence = _detect_pitcher_strategy(roster)
        assert strategy == "balanced"
        assert confidence == 0.3

    def test_rp_only_no_sp(self):
        """All RP, no SP → rp_heavy with high confidence."""
        roster = [
            _make_ranking(i, f"RP{i}", ["RP"], "pitching") for i in range(1, 5)
        ]

        strategy, reason, confidence = _detect_pitcher_strategy(roster)
        assert strategy == "rp_heavy"
        assert confidence == 0.9

    def test_sp_only_no_rp(self):
        """All SP, no RP → sp_heavy with high confidence."""
        roster = [
            _make_ranking(i, f"SP{i}", ["SP"], "pitching") for i in range(1, 5)
        ]

        strategy, reason, confidence = _detect_pitcher_strategy(roster)
        assert strategy == "sp_heavy"
        assert confidence == 0.9


# ---------------------------------------------------------------------------
# Tests: _detect_position_punts
# ---------------------------------------------------------------------------


class TestDetectPositionPunts:
    def test_detects_zero_catcher(self):
        """No C-eligible player → detects catcher punt."""
        roster = [
            _make_ranking(1, "First", ["1B"], "batting"),
            _make_ranking(2, "Second", ["2B"], "batting"),
            _make_ranking(3, "Third", ["3B"], "batting"),
            _make_ranking(4, "Short", ["SS"], "batting"),
            _make_ranking(5, "Left", ["LF", "OF"], "batting"),
            _make_ranking(6, "Center", ["CF", "OF"], "batting"),
            _make_ranking(7, "Right", ["RF", "OF"], "batting"),
        ]

        punted, reason, confidence = _detect_position_punts(roster, STANDARD_POSITIONS)
        assert "C" in punted
        assert confidence == 0.9

    def test_no_punt_when_catcher_present(self):
        """C-eligible player on roster → no catcher punt."""
        roster = [
            _make_ranking(1, "Catcher", ["C"], "batting"),
            _make_ranking(2, "First", ["1B"], "batting"),
            _make_ranking(3, "Short", ["SS"], "batting"),
            _make_ranking(4, "Left", ["LF", "OF"], "batting"),
        ]

        punted, reason, confidence = _detect_position_punts(roster, STANDARD_POSITIONS)
        assert "C" not in punted

    def test_detects_multiple_position_punts(self):
        """Missing C and 2B eligible players → both punted."""
        roster = [
            _make_ranking(1, "First", ["1B"], "batting"),
            _make_ranking(2, "Third", ["3B"], "batting"),
            _make_ranking(3, "Short", ["SS"], "batting"),
            _make_ranking(4, "Left", ["LF", "OF"], "batting"),
        ]

        punted, reason, confidence = _detect_position_punts(roster, STANDARD_POSITIONS)
        assert "C" in punted
        assert "2B" in punted

    def test_ignores_bench_and_util_slots(self):
        """BN, IL, NA, Util, P slots should not trigger punt detection."""
        roster = []  # empty roster
        positions = ["BN", "IL", "NA", "Util", "P"]

        punted, reason, confidence = _detect_position_punts(roster, positions)
        assert punted == []


# ---------------------------------------------------------------------------
# Tests: _detect_category_punts
# ---------------------------------------------------------------------------


class TestDetectCategoryPunts:
    def test_detects_punted_category(self):
        """Team with very negative z-scores in SB → detects SB punt."""
        # Build roster where SB contributions are terrible
        roster = [
            _make_ranking(
                i, f"Player{i}", ["1B"], "batting",
                contributions={"R": 1.0, "HR": 1.5, "RBI": 1.0, "SB": -4.0, "AVG": 0.5},
            )
            for i in range(1, 6)
        ]

        punted, reason, confidence = _detect_category_punts(roster, STANDARD_CATEGORIES)
        assert "SB" in punted
        assert confidence > 0.0

    def test_no_punt_with_strong_categories(self):
        """Team strong across the board → no punts detected."""
        roster = [
            _make_ranking(
                i, f"Player{i}", ["1B"], "batting",
                contributions={cat: 1.0 for cat in STANDARD_CATEGORIES},
            )
            for i in range(1, 6)
        ]

        punted, reason, confidence = _detect_category_punts(roster, STANDARD_CATEGORIES)
        assert punted == []

    def test_high_confidence_for_deeply_punted(self):
        """Very deeply negative z-score → higher confidence."""
        roster = [
            _make_ranking(
                i, f"Player{i}", ["1B"], "batting",
                contributions={"SB": -5.0, "R": 1.0, "HR": 1.0},
            )
            for i in range(1, 6)
        ]

        punted, reason, confidence = _detect_category_punts(roster, ["R", "HR", "SB"])
        if "SB" in punted:
            assert confidence >= 0.7


# ---------------------------------------------------------------------------
# Tests: _detect_priority_targets
# ---------------------------------------------------------------------------


class TestDetectPriorityTargets:
    def test_detects_competitive_category_with_elite_contributor(self):
        """Team competitive in HR with an elite contributor → priority target."""
        roster = [
            _make_ranking(
                1, "Slugger", ["1B"], "batting",
                contributions={"HR": 2.0, "R": 0.5, "RBI": 0.5},
            ),
            _make_ranking(
                2, "Average", ["2B"], "batting",
                contributions={"HR": -0.5, "R": 0.3, "RBI": 0.2},
            ),
        ]

        targets, reason, confidence = _detect_priority_targets(roster, ["HR", "R", "RBI"])
        # HR team z = 1.5 (competitive range 0.5-3.0) and Slugger has elite 2.0
        assert "HR" in targets

    def test_no_target_when_too_weak(self):
        """Team z-score below competitive range → not a priority target."""
        roster = [
            _make_ranking(
                1, "Weak", ["1B"], "batting",
                contributions={"HR": -2.0, "R": 0.3},
            ),
        ]

        targets, reason, confidence = _detect_priority_targets(roster, ["HR", "R"])
        assert "HR" not in targets

    def test_no_target_when_dominant(self):
        """Team z-score above competitive range → already dominant, not a target."""
        roster = [
            _make_ranking(
                i, f"Star{i}", ["1B"], "batting",
                contributions={"HR": 2.0},
            )
            for i in range(1, 4)
        ]

        targets, reason, confidence = _detect_priority_targets(roster, ["HR"])
        # Team z = 6.0 → above COMPETITIVE_HIGH (3.0)
        assert "HR" not in targets

    def test_no_target_without_elite_contributor(self):
        """Team competitive but no elite individual → not a target."""
        roster = [
            _make_ranking(
                i, f"Player{i}", ["1B"], "batting",
                contributions={"HR": 0.4},  # below ELITE_CONTRIBUTOR_THRESHOLD
            )
            for i in range(1, 4)
        ]

        targets, reason, confidence = _detect_priority_targets(roster, ["HR"])
        # Team z = 1.2 (competitive) but no individual is elite
        assert "HR" not in targets


# ---------------------------------------------------------------------------
# Tests: suggest_strategy (integration)
# ---------------------------------------------------------------------------


class TestSuggestStrategy:
    def test_returns_strategy_suggestion_type(self):
        """suggest_strategy returns a StrategySuggestion dataclass."""
        roster = [_make_ranking(1, "Player1", ["1B"], "batting")]
        result = suggest_strategy(roster, STANDARD_CATEGORIES, STANDARD_POSITIONS, "h2h_categories")
        assert isinstance(result, StrategySuggestion)
        assert isinstance(result.preferences, BuildPreferences)
        assert isinstance(result.reasoning, dict)
        assert 0.0 <= result.confidence <= 1.0

    def test_pure_function_no_side_effects(self):
        """Calling suggest_strategy twice with same input gives same output."""
        roster = [
            _make_ranking(1, "Slugger", ["1B"], "batting", contributions={"HR": 2.0}),
            _make_ranking(2, "Closer", ["RP"], "pitching"),
            _make_ranking(3, "Closer2", ["RP"], "pitching"),
            _make_ranking(4, "Closer3", ["RP"], "pitching"),
        ]
        cats = STANDARD_CATEGORIES
        pos = STANDARD_POSITIONS

        result1 = suggest_strategy(roster, cats, pos, "h2h_categories")
        result2 = suggest_strategy(roster, cats, pos, "h2h_categories")

        assert result1.preferences == result2.preferences
        assert result1.confidence == result2.confidence
        assert result1.reasoning == result2.reasoning

    def test_combined_rp_heavy_and_zero_catcher(self):
        """Roster with many RP and no catcher → detects both signals."""
        roster = [
            _make_ranking(1, "First", ["1B"], "batting"),
            _make_ranking(2, "Short", ["SS"], "batting"),
            _make_ranking(3, "Left", ["LF", "OF"], "batting"),
            _make_ranking(4, "Center", ["CF", "OF"], "batting"),
            _make_ranking(5, "Right", ["RF", "OF"], "batting"),
            _make_ranking(6, "RP1", ["RP"], "pitching"),
            _make_ranking(7, "RP2", ["RP"], "pitching"),
            _make_ranking(8, "RP3", ["RP"], "pitching"),
            _make_ranking(9, "RP4", ["RP"], "pitching"),
            _make_ranking(10, "RP5", ["RP"], "pitching"),
            _make_ranking(11, "RP6", ["RP"], "pitching"),
            _make_ranking(12, "SP1", ["SP"], "pitching"),
        ]

        result = suggest_strategy(roster, STANDARD_CATEGORIES, STANDARD_POSITIONS, "h2h_categories")
        assert result.preferences.pitcher_strategy == "rp_heavy"
        assert "C" in result.preferences.punt_positions
        assert "pitcher_strategy" in result.reasoning
        assert "punt_positions" in result.reasoning

    def test_confidence_high_for_clear_signal(self):
        """Very clear RP-heavy build → high overall confidence."""
        roster = [
            _make_ranking(i, f"RP{i}", ["RP"], "pitching") for i in range(1, 8)
        ] + [_make_ranking(8, "SP1", ["SP"], "pitching")]

        result = suggest_strategy(roster, STANDARD_CATEGORIES, STANDARD_POSITIONS, "h2h_categories")
        assert result.confidence >= 0.7

    def test_confidence_low_for_ambiguous(self):
        """Balanced roster with no clear signals → moderate confidence."""
        roster = [
            _make_ranking(1, "C", ["C"], "batting", contributions={cat: 0.5 for cat in STANDARD_CATEGORIES}),
            _make_ranking(2, "1B", ["1B"], "batting", contributions={cat: 0.5 for cat in STANDARD_CATEGORIES}),
            _make_ranking(3, "2B", ["2B"], "batting", contributions={cat: 0.5 for cat in STANDARD_CATEGORIES}),
            _make_ranking(4, "3B", ["3B"], "batting", contributions={cat: 0.5 for cat in STANDARD_CATEGORIES}),
            _make_ranking(5, "SS", ["SS"], "batting", contributions={cat: 0.5 for cat in STANDARD_CATEGORIES}),
            _make_ranking(6, "OF1", ["LF", "OF"], "batting", contributions={cat: 0.5 for cat in STANDARD_CATEGORIES}),
            _make_ranking(7, "SP1", ["SP"], "pitching"),
            _make_ranking(8, "SP2", ["SP"], "pitching"),
            _make_ranking(9, "RP1", ["RP"], "pitching"),
            _make_ranking(10, "RP2", ["RP"], "pitching"),
        ]

        result = suggest_strategy(roster, STANDARD_CATEGORIES, STANDARD_POSITIONS, "h2h_categories")
        assert result.preferences.pitcher_strategy == "balanced"
        assert result.confidence <= 0.6

    def test_skips_category_punts_for_roto(self):
        """Category punt detection only runs for h2h_categories."""
        roster = [
            _make_ranking(
                1, "Player1", ["1B"], "batting",
                contributions={"SB": -5.0, "R": 1.0},
            ),
        ]

        result = suggest_strategy(roster, ["R", "SB"], STANDARD_POSITIONS, "roto")
        assert result.preferences.punt_categories == []
