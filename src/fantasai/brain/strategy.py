"""Strategy suggester: analyzes a roster and recommends a build strategy.

Pure function — takes roster data, returns a StrategySuggestion with
BuildPreferences and reasoning for each recommendation. No DB dependency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from fantasai.brain.recommender import (
    BuildPreferences,
    _compute_team_strengths,
    _identify_weak_categories,
    _player_eligible_for_slot,
)
from fantasai.engine.scoring import PlayerRanking

logger = logging.getLogger(__name__)

# Thresholds for pitcher strategy detection
SP_HEAVY_RATIO = 2.0  # SP count >= ratio * RP count → sp_heavy
RP_HEAVY_RATIO = 2.0  # RP count >= ratio * SP count → rp_heavy

# Confidence thresholds
HIGH_CONFIDENCE_RATIO = 3.0  # very clear signal
MEDIUM_CONFIDENCE_RATIO = 2.0
HIGH_PUNT_THRESHOLD = -4.0  # very clearly punted

# Priority target detection: team is competitive in a category
# if z-score is in this range
COMPETITIVE_LOW = 0.5
COMPETITIVE_HIGH = 3.0
# A player is "elite" in a category if their individual z-score is above this
ELITE_CONTRIBUTOR_THRESHOLD = 1.5


@dataclass
class StrategySuggestion:
    """A suggested BuildPreferences with reasoning for each field."""

    preferences: BuildPreferences
    reasoning: dict[str, str]  # maps field name → rationale string
    confidence: float  # 0.0–1.0, overall confidence in the suggestion


def suggest_strategy(
    roster_rankings: list[PlayerRanking],
    scoring_categories: list[str],
    roster_positions: list[str],
    league_type: str,
) -> StrategySuggestion:
    """Analyze a roster and suggest an optimal build strategy.

    Examines roster composition and category strengths to infer
    what build the manager appears to be running, then suggests
    preferences that align with and optimize that build.

    Args:
        roster_rankings: PlayerRanking objects for all rostered players.
        scoring_categories: League scoring categories.
        roster_positions: League roster slot configuration.
        league_type: "h2h_categories", "roto", or "points".

    Returns:
        StrategySuggestion with recommended BuildPreferences and reasoning.
    """
    reasoning: dict[str, str] = {}
    confidence_signals: list[float] = []

    # --- Pitcher strategy detection ---
    pitcher_strategy, pitcher_reason, pitcher_conf = _detect_pitcher_strategy(
        roster_rankings
    )
    reasoning["pitcher_strategy"] = pitcher_reason
    confidence_signals.append(pitcher_conf)

    # --- Position punt detection ---
    punt_positions, pos_reason, pos_conf = _detect_position_punts(
        roster_rankings, roster_positions
    )
    if punt_positions:
        reasoning["punt_positions"] = pos_reason
        confidence_signals.append(pos_conf)

    # --- Category punt detection (H2H only) ---
    punt_categories: list[str] = []
    if league_type == "h2h_categories" and scoring_categories:
        punt_categories, cat_reason, cat_conf = _detect_category_punts(
            roster_rankings, scoring_categories
        )
        if punt_categories:
            reasoning["punt_categories"] = cat_reason
            confidence_signals.append(cat_conf)

    # --- Priority target detection ---
    priority_targets: list[str] = []
    if scoring_categories:
        priority_targets, pri_reason, pri_conf = _detect_priority_targets(
            roster_rankings, scoring_categories
        )
        if priority_targets:
            reasoning["priority_targets"] = pri_reason
            confidence_signals.append(pri_conf)

    # Compute overall confidence (average of detected signals, or 0.5 default)
    overall_confidence = (
        sum(confidence_signals) / len(confidence_signals)
        if confidence_signals
        else 0.5
    )

    preferences = BuildPreferences(
        pitcher_strategy=pitcher_strategy,
        punt_positions=punt_positions,
        punt_categories=punt_categories,
        priority_targets=priority_targets,
    )

    return StrategySuggestion(
        preferences=preferences,
        reasoning=reasoning,
        confidence=round(overall_confidence, 2),
    )


def _detect_pitcher_strategy(
    roster_rankings: list[PlayerRanking],
) -> tuple[str, str, float]:
    """Detect pitcher build strategy from roster composition.

    Returns (strategy, reasoning, confidence).
    """
    sp_count = sum(1 for r in roster_rankings if "SP" in r.positions)
    rp_count = sum(1 for r in roster_rankings if "RP" in r.positions)

    if sp_count == 0 and rp_count == 0:
        return "balanced", "No pitchers on roster yet.", 0.3

    if rp_count > 0 and (sp_count == 0 or rp_count / max(sp_count, 1) >= HIGH_CONFIDENCE_RATIO):
        return (
            "rp_heavy",
            f"Roster has {rp_count} relievers and {sp_count} starters — "
            f"strong RP-heavy signal.",
            0.9,
        )
    if rp_count > 0 and rp_count / max(sp_count, 1) >= MEDIUM_CONFIDENCE_RATIO:
        return (
            "rp_heavy",
            f"Roster has {rp_count} relievers and {sp_count} starters — "
            f"appears RP-heavy.",
            0.7,
        )

    if sp_count > 0 and (rp_count == 0 or sp_count / max(rp_count, 1) >= HIGH_CONFIDENCE_RATIO):
        return (
            "sp_heavy",
            f"Roster has {sp_count} starters and {rp_count} relievers — "
            f"strong SP-heavy signal.",
            0.9,
        )
    if sp_count > 0 and sp_count / max(rp_count, 1) >= MEDIUM_CONFIDENCE_RATIO:
        return (
            "sp_heavy",
            f"Roster has {sp_count} starters and {rp_count} relievers — "
            f"appears SP-heavy.",
            0.7,
        )

    return (
        "balanced",
        f"Roster has {sp_count} starters and {rp_count} relievers — balanced mix.",
        0.5,
    )


def _detect_position_punts(
    roster_rankings: list[PlayerRanking],
    roster_positions: list[str],
) -> tuple[list[str], str, float]:
    """Detect position punts by finding required slots with zero eligible players.

    Returns (punt_positions, reasoning, confidence).
    """
    required_slots = set(
        s for s in roster_positions if s not in ("BN", "IL", "IL+", "NA", "Util", "P")
    )

    punted: list[str] = []
    for slot in sorted(required_slots):
        eligible_count = sum(
            1
            for r in roster_rankings
            if _player_eligible_for_slot(r.positions, slot)
        )
        if eligible_count == 0:
            punted.append(slot)

    if punted:
        reason = f"No players eligible for {', '.join(punted)} slot(s) — position punt detected."
        return punted, reason, 0.9
    return [], "", 0.0


def _detect_category_punts(
    roster_rankings: list[PlayerRanking],
    scoring_categories: list[str],
) -> tuple[list[str], str, float]:
    """Detect category punts from team z-score analysis.

    Returns (punt_categories, reasoning, confidence).
    """
    strengths = _compute_team_strengths(roster_rankings, scoring_categories)
    _, auto_punted = _identify_weak_categories(strengths, "h2h_categories")

    if not auto_punted:
        return [], "", 0.0

    # Build reasoning with z-scores
    parts = []
    confidences = []
    for cat in auto_punted:
        z = strengths.get(cat, 0.0)
        parts.append(f"{cat} (z={z:.1f})")
        confidences.append(0.9 if z < HIGH_PUNT_THRESHOLD else 0.7)

    reason = f"Team is effectively punting {', '.join(parts)}."
    avg_conf = sum(confidences) / len(confidences) if confidences else 0.5
    return auto_punted, reason, round(avg_conf, 2)


def _detect_priority_targets(
    roster_rankings: list[PlayerRanking],
    scoring_categories: list[str],
) -> tuple[list[str], str, float]:
    """Detect categories worth prioritizing — competitive with elite contributors.

    A category is a priority target if:
    1. Team z-score is in the competitive range (not dominant, not weak)
    2. At least one rostered player has an elite individual contribution

    Returns (priority_targets, reasoning, confidence).
    """
    strengths = _compute_team_strengths(roster_rankings, scoring_categories)

    targets: list[str] = []
    parts: list[str] = []

    for cat in scoring_categories:
        team_z = strengths.get(cat, 0.0)
        if not (COMPETITIVE_LOW <= team_z <= COMPETITIVE_HIGH):
            continue

        # Check for elite individual contributor
        has_elite = any(
            r.category_contributions.get(cat, 0.0) >= ELITE_CONTRIBUTOR_THRESHOLD
            for r in roster_rankings
        )
        if has_elite:
            targets.append(cat)
            parts.append(f"{cat} (z={team_z:.1f}, has elite contributor)")

    if targets:
        reason = f"Competitive with upside in {', '.join(parts)} — worth targeting."
        return targets, reason, 0.6
    return [], "", 0.0
