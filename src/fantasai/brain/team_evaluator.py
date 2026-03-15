"""Team evaluation and keeper planning engine.

Two responsibilities:
  1. evaluate_team() — holistic assessment of a roster: letter grade,
     position-by-position breakdown, category strengths/gaps, and
     algorithmic improvement suggestions.
  2. evaluate_keepers() / plan_keepers() — keeper/dynasty planning:
     evaluate an existing keeper core, or recommend which players to
     keep from a full roster and what to target in the upcoming draft.

Purely functional — no DB dependency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from fantasai.brain.recommender import (
    HITTER_POSITIONS,
    _compute_team_strengths,
    _identify_weak_categories,
)
from fantasai.brain.trade_evaluator import KEEPER_AGE_BONUS
from fantasai.engine.scoring import PlayerRanking

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Position grouping
# ---------------------------------------------------------------------------

# Canonical groups for the position breakdown display.
# Each roster player is bucketed by their primary position.
POSITION_GROUPS = ["C", "1B", "2B", "SS", "3B", "OF", "Util", "DH", "SP", "RP", "P"]

# Assessment tiers for a position group based on the group's z-score sum
# relative to the team's own distribution.
ASSESSMENT_THRESHOLDS = [
    (1.5, "elite"),
    (0.5, "solid"),
    (-0.5, "average"),
    (-1.5, "weak"),
]


def _assess_group(group_score: float, group_scores: list[float]) -> str:
    """Classify a position group as elite/solid/average/weak/empty.

    Uses the team's own distribution so every team's worst group reads
    "weak" and the best reads "elite" — relative to themselves.
    """
    if not group_scores:
        return "empty"
    sorted_scores = sorted(group_scores)
    n = len(sorted_scores)
    if n == 0:
        return "empty"

    # Simple quartile-relative classification
    pct = sorted_scores.index(min(sorted_scores, key=lambda x: abs(x - group_score))) / max(n - 1, 1)
    if pct >= 0.75:
        return "elite"
    if pct >= 0.5:
        return "solid"
    if pct >= 0.25:
        return "average"
    return "weak"


# ---------------------------------------------------------------------------
# Letter grade helpers
# ---------------------------------------------------------------------------

# Absolute z-score thresholds used when no league context is available
GRADE_THRESHOLDS_ABSOLUTE = [
    (1.5, "A"),
    (0.5, "B"),
    (-0.5, "C"),
    (-1.5, "D"),
]

GRADE_PERCENTILE_THRESHOLDS = [
    (80.0, "A"),
    (60.0, "B"),
    (40.0, "C"),
    (20.0, "D"),
]


def _compute_letter_grade(
    overall_score: float,
    league_team_scores: Optional[list[float]] = None,
) -> tuple[str, float]:
    """Return (letter_grade, percentile).

    Percentile is the team's rank vs other league teams (0–100).
    If no league context, percentile is estimated from absolute score.
    """
    if league_team_scores and len(league_team_scores) > 1:
        # Percentile rank among actual league teams
        rank = sum(1 for s in league_team_scores if s < overall_score)
        percentile = round(rank / len(league_team_scores) * 100, 1)
        for threshold, grade in GRADE_PERCENTILE_THRESHOLDS:
            if percentile >= threshold:
                return grade, percentile
        return "F", percentile
    else:
        # Absolute scale: estimate percentile from known distribution
        for threshold, grade in GRADE_THRESHOLDS_ABSOLUTE:
            if overall_score >= threshold:
                # Rough percentile estimate from z-score
                pct = min(99.0, max(1.0, 50.0 + overall_score * 20.0))
                return grade, round(pct, 1)
        return "F", max(1.0, round(50.0 + overall_score * 20.0, 1))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PositionGroupScore:
    """Summary for one position group on the roster."""

    position: str
    players: list[str]          # player names in this group
    group_score: float          # sum of z-scores for the group
    assessment: str             # "elite" | "solid" | "average" | "weak" | "empty"


@dataclass
class TeamEvaluation:
    """Full team evaluation result."""

    overall_score: float
    letter_grade: str
    grade_percentile: float
    category_strengths: dict[str, float]
    strong_categories: list[str]
    weak_categories: list[str]
    position_breakdown: list[PositionGroupScore]
    improvement_suggestions: list[str]
    pros: list[str]
    cons: list[str]
    analysis_blurb: str            # filled in by API layer


@dataclass
class DraftProfile:
    """A recommended player profile to target in an upcoming draft."""

    priority: int
    position: str
    category_targets: list[str]
    rationale: str
    example_players: list[str] = field(default_factory=list)


@dataclass
class KeeperEvaluation:
    """Result of a keeper/dynasty evaluation."""

    mode: str                          # "evaluate_keepers" | "plan_keepers"
    keepers: list[PlayerRanking]       # confirmed or recommended
    cuts: list[PlayerRanking]          # plan_keepers only; empty otherwise
    keeper_foundation_grade: str       # A–F
    category_strengths: dict[str, float]
    category_gaps: list[str]
    position_gaps: list[str]
    draft_profiles: list[DraftProfile]
    pros: list[str]
    cons: list[str]
    analysis_blurb: str                # filled in by API layer


# ---------------------------------------------------------------------------
# Team evaluation
# ---------------------------------------------------------------------------


def _build_position_breakdown(
    roster_rankings: list[PlayerRanking],
    categories: list[str],
) -> list[PositionGroupScore]:
    """Group players by primary position and score each group."""
    groups: dict[str, list[PlayerRanking]] = {}

    for ranking in roster_rankings:
        primary = ranking.positions[0] if ranking.positions else "UTIL"

        # Normalise OF sub-positions
        if primary in ("LF", "CF", "RF"):
            primary = "OF"

        # Map hitters without a specific slot to Util
        if primary not in POSITION_GROUPS:
            primary = "Util" if primary in HITTER_POSITIONS else "P"

        groups.setdefault(primary, []).append(ranking)

    # Compute group scores
    group_scores_raw: list[tuple[str, list[PlayerRanking], float]] = []
    for pos, players in groups.items():
        score = sum(
            sum(r.category_contributions.get(c, 0.0) for c in categories)
            for r in players
        )
        group_scores_raw.append((pos, players, round(score, 3)))

    # Assess each group relative to the team
    all_scores = [s for _, _, s in group_scores_raw]

    result: list[PositionGroupScore] = []
    for pos, players, score in group_scores_raw:
        assessment = _assess_group(score, all_scores)
        result.append(
            PositionGroupScore(
                position=pos,
                players=[r.name for r in players],
                group_score=score,
                assessment=assessment,
            )
        )

    # Sort by group score descending
    result.sort(key=lambda g: g.group_score, reverse=True)

    # Add explicit "empty" entries for required positions with no players
    populated = {g.position for g in result}
    for pos in ["C", "SP", "RP"]:
        if pos not in populated:
            result.append(PositionGroupScore(pos, [], 0.0, "empty"))

    return result


def _compute_improvement_suggestions(
    position_breakdown: list[PositionGroupScore],
    weak_categories: list[str],
    category_strengths: dict[str, float],
    context: Optional[str] = None,
) -> list[str]:
    """Generate algorithmic improvement suggestions."""
    suggestions: list[str] = []

    # Empty or weak position groups
    for group in position_breakdown:
        if group.assessment == "empty":
            suggestions.append(
                f"No {group.position} coverage — this is a significant roster gap."
            )
        elif group.assessment == "weak" and group.position not in ("Util", "DH"):
            suggestions.append(
                f"Weak {group.position} group ({', '.join(group.players) or 'no players'}) "
                f"— consider an upgrade."
            )

    # Category deficits → position recommendations
    cat_to_position = {
        "SB": "OF or SS with speed",
        "SV": "closer (RP)",
        "HLD": "setup man (RP)",
        "ERA": "SP with strong ERA track record",
        "WHIP": "SP or RP with command",
        "K": "power arm (SP or RP)",
        "W": "SP with run support",
        "HR": "power bat (1B, OF, or DH)",
        "R": "leadoff-type hitter (OF or SS)",
        "RBI": "run producer (1B, OF, or DH)",
        "AVG": "contact hitter",
        "OPS": "high-OBP bat",
    }
    for cat in weak_categories[:3]:  # top 3 most urgent
        suggestion = f"Weak in {cat}"
        hint = cat_to_position.get(cat)
        if hint:
            suggestion += f" — targeting a {hint} could help"
        suggestions.append(suggestion + ".")

    # Single-player dependence warning
    # (if one player accounts for >50% of a weak category's improvement potential)
    # Keep simple: just flag if any category is carried by one player
    if context and "injur" in context.lower():
        suggestions.append(
            "Given injury concerns: prioritise players with track records of > 140 games per season."
        )

    return suggestions[:6]  # cap at 6 to keep response clean


def evaluate_team(
    roster_rankings: list[PlayerRanking],
    categories: list[str],
    roster_positions: list[str],
    league_type: str,
    league_team_scores: Optional[list[float]] = None,
    context: Optional[str] = None,
) -> TeamEvaluation:
    """Evaluate a roster holistically.

    Args:
        roster_rankings: Pre-computed PlayerRanking objects for all rostered players.
        categories: League scoring categories.
        roster_positions: Roster slot configuration (e.g. ["SP", "SP", "RP", ...]).
        league_type: "h2h_categories" | "roto" | "points".
        league_team_scores: Optional list of all league teams' overall scores for
            percentile-relative grading.
        context: Free-text user context passed to LLM and checked for keywords.

    Returns:
        TeamEvaluation with grade, breakdown, and suggestions.
    """
    if not roster_rankings:
        return TeamEvaluation(
            overall_score=0.0,
            letter_grade="F",
            grade_percentile=0.0,
            category_strengths={},
            strong_categories=[],
            weak_categories=[],
            position_breakdown=[],
            improvement_suggestions=["Roster appears empty — no players found."],
            pros=[],
            cons=["Empty roster"],
            analysis_blurb="",
        )

    # Overall score: mean of individual player composite scores
    overall_score = round(sum(r.score for r in roster_rankings) / len(roster_rankings), 3)

    # Letter grade + percentile
    letter_grade, percentile = _compute_letter_grade(overall_score, league_team_scores)

    # Category strengths
    category_strengths = _compute_team_strengths(roster_rankings, categories)

    # Weak / punted categories
    weak_cats, punted_cats = _identify_weak_categories(category_strengths, league_type)

    # Apply context keyword overrides (e.g. "punting stolen bases" → treat SB as punted)
    if context:
        from fantasai.brain.comparator import _parse_context_keywords
        mentioned = _parse_context_keywords(context, categories)
        # If user mentions a category, treat as either a priority or a punt based on phrase
        punt_phrases = ["punt", "punting", "don't care", "ignoring", "skip"]
        is_punt_context = any(p in context.lower() for p in punt_phrases)
        if is_punt_context and mentioned:
            for cat in mentioned:
                if cat not in punted_cats:
                    punted_cats.append(cat)
                if cat in weak_cats:
                    weak_cats.remove(cat)

    # Strong categories: top third by absolute strength
    sorted_cats = sorted(category_strengths.items(), key=lambda x: x[1], reverse=True)
    n_strong = max(1, len(sorted_cats) // 3)
    strong_cats = [c for c, _ in sorted_cats[:n_strong] if c not in punted_cats]

    # Position breakdown
    position_breakdown = _build_position_breakdown(roster_rankings, categories)

    # Improvement suggestions
    suggestions = _compute_improvement_suggestions(
        position_breakdown, weak_cats, category_strengths, context
    )

    # Algorithmic pros and cons
    pros: list[str] = []
    cons: list[str] = []

    if strong_cats:
        pros.append(f"Strong in {', '.join(strong_cats[:3])}")
    if letter_grade in ("A", "B"):
        pros.append("Overall roster quality is above average")
    elite_groups = [g for g in position_breakdown if g.assessment == "elite"]
    if elite_groups:
        pros.append(f"Elite depth at {', '.join(g.position for g in elite_groups[:2])}")

    if weak_cats and all(c not in punted_cats for c in weak_cats):
        cons.append(f"Category weaknesses: {', '.join(weak_cats[:3])}")
    empty_groups = [g for g in position_breakdown if g.assessment == "empty"]
    if empty_groups:
        cons.append(f"Missing coverage at {', '.join(g.position for g in empty_groups)}")
    if letter_grade in ("D", "F"):
        cons.append("Overall roster quality is below average")

    logger.info(
        "Team evaluated: score=%.2f, grade=%s (%.0f%%ile), weak=%s",
        overall_score,
        letter_grade,
        percentile,
        weak_cats,
    )

    return TeamEvaluation(
        overall_score=overall_score,
        letter_grade=letter_grade,
        grade_percentile=percentile,
        category_strengths=category_strengths,
        strong_categories=strong_cats,
        weak_categories=weak_cats + punted_cats,
        position_breakdown=position_breakdown,
        improvement_suggestions=suggestions,
        pros=pros,
        cons=cons,
        analysis_blurb="",
    )


# ---------------------------------------------------------------------------
# Keeper evaluation
# ---------------------------------------------------------------------------


def _keeper_age_multiplier(player_id: int, player_ages: dict[int, int]) -> float:
    """Return a keeper-value age multiplier (same table as trade_evaluator)."""
    age = player_ages.get(player_id)
    if age is None:
        return 1.0
    for lo, hi, mult in KEEPER_AGE_BONUS:
        if lo <= age <= hi:
            return mult
    return 1.0


def _compute_position_gaps(
    keeper_rankings: list[PlayerRanking],
    roster_positions: list[str],
) -> list[str]:
    """Identify roster slot types that have no keeper coverage."""
    # Required non-bench slots
    required = set(
        s for s in roster_positions
        if s not in ("BN", "IL", "IL+", "NA")
    )
    from fantasai.brain.recommender import _player_eligible_for_slot
    gaps: list[str] = []
    for slot in sorted(required):
        covered = any(
            _player_eligible_for_slot(r.positions, slot)
            for r in keeper_rankings
        )
        if not covered:
            gaps.append(slot)
    return gaps


def _build_draft_profiles(
    category_gaps: list[str],
    position_gaps: list[str],
    available_pool: Optional[list[PlayerRanking]] = None,
) -> list[DraftProfile]:
    """Build ordered draft target profiles based on category and position gaps."""
    profiles: list[DraftProfile] = []
    priority = 1

    # Position gaps are highest priority
    _pos_to_cat: dict[str, list[str]] = {
        "C": ["HR", "RBI"],
        "SS": ["R", "SB", "OBP"],
        "2B": ["R", "OBP"],
        "3B": ["HR", "RBI"],
        "SP": ["K", "ERA", "WHIP", "W"],
        "RP": ["SV", "HLD", "ERA"],
        "OF": ["R", "HR", "SB"],
        "1B": ["HR", "RBI"],
    }

    for pos in position_gaps:
        cat_targets = _pos_to_cat.get(pos, [])
        example_players: list[str] = []
        if available_pool:
            from fantasai.brain.recommender import _player_eligible_for_slot
            eligible = [r for r in available_pool if _player_eligible_for_slot(r.positions, pos)]
            eligible.sort(key=lambda r: r.score, reverse=True)
            example_players = [r.name for r in eligible[:3]]

        profiles.append(
            DraftProfile(
                priority=priority,
                position=pos,
                category_targets=cat_targets,
                rationale=f"No {pos} coverage in keeper core — essential positional need.",
                example_players=example_players,
            )
        )
        priority += 1

    # Category gaps next
    _cat_to_pos: dict[str, str] = {
        "SB": "OF or SS",
        "SV": "RP (closer)",
        "HLD": "RP (setup)",
        "ERA": "SP",
        "WHIP": "SP or RP",
        "K": "SP",
        "W": "SP",
        "HR": "1B or OF",
        "R": "OF or SS",
        "RBI": "1B or OF",
        "AVG": "any position",
        "OPS": "any position",
    }

    for cat in category_gaps:
        pos_hint = _cat_to_pos.get(cat, "any position")
        example_players = []
        if available_pool:
            relevant = sorted(
                [r for r in available_pool if r.category_contributions.get(cat, 0.0) > 0.5],
                key=lambda r: r.category_contributions.get(cat, 0.0),
                reverse=True,
            )
            example_players = [r.name for r in relevant[:3]]

        profiles.append(
            DraftProfile(
                priority=priority,
                position=pos_hint,
                category_targets=[cat],
                rationale=f"Keeper core is weak in {cat} — target a {pos_hint} contributor.",
                example_players=example_players,
            )
        )
        priority += 1

    return profiles[:8]  # cap at 8 profiles


def evaluate_keepers(
    keeper_rankings: list[PlayerRanking],
    categories: list[str],
    roster_positions: list[str],
    league_type: str,
    available_pool: Optional[list[PlayerRanking]] = None,
    context: Optional[str] = None,
) -> KeeperEvaluation:
    """Evaluate a confirmed keeper core and suggest draft targets.

    Args:
        keeper_rankings: Rankings for the confirmed keeper players only.
        categories: League scoring categories.
        roster_positions: Full expected roster configuration.
        league_type: League format.
        available_pool: Optional rankings for non-rostered players (improves
            example player suggestions in draft profiles).
        context: Optional free-text user context.

    Returns:
        KeeperEvaluation with draft profiles and foundation grade.
    """
    if not keeper_rankings:
        return KeeperEvaluation(
            mode="evaluate_keepers",
            keepers=[],
            cuts=[],
            keeper_foundation_grade="F",
            category_strengths={},
            category_gaps=categories,
            position_gaps=roster_positions,
            draft_profiles=[],
            pros=[],
            cons=["No keepers provided"],
            analysis_blurb="",
        )

    team_eval = evaluate_team(
        keeper_rankings, categories, roster_positions, league_type, context=context
    )

    # Category gaps: weak categories that aren't punted
    cat_gaps = team_eval.weak_categories

    # Position gaps: required slots with no keeper coverage
    pos_gaps = _compute_position_gaps(keeper_rankings, roster_positions)

    draft_profiles = _build_draft_profiles(cat_gaps, pos_gaps, available_pool)

    pros: list[str] = []
    cons: list[str] = []

    if team_eval.strong_categories:
        pros.append(f"Keeper core is strong in {', '.join(team_eval.strong_categories[:3])}")
    if len(keeper_rankings) >= 3:
        top_keepers = sorted(keeper_rankings, key=lambda r: r.score, reverse=True)[:3]
        pros.append(f"Anchored by {', '.join(r.name for r in top_keepers)}")

    if cat_gaps:
        cons.append(f"Will need to draft for {', '.join(cat_gaps[:3])}")
    if pos_gaps:
        cons.append(f"Missing keeper coverage at {', '.join(pos_gaps)}")

    logger.info(
        "Keeper evaluation: %d keepers, %d cat gaps, %d pos gaps",
        len(keeper_rankings),
        len(cat_gaps),
        len(pos_gaps),
    )

    return KeeperEvaluation(
        mode="evaluate_keepers",
        keepers=keeper_rankings,
        cuts=[],
        keeper_foundation_grade=team_eval.letter_grade,
        category_strengths=team_eval.category_strengths,
        category_gaps=cat_gaps,
        position_gaps=pos_gaps,
        draft_profiles=draft_profiles,
        pros=pros,
        cons=cons,
        analysis_blurb="",
    )


def plan_keepers(
    full_roster_rankings: list[PlayerRanking],
    n_keepers: int,
    categories: list[str],
    roster_positions: list[str],
    league_type: str,
    available_pool: Optional[list[PlayerRanking]] = None,
    player_ages: Optional[dict[int, int]] = None,
    context: Optional[str] = None,
) -> KeeperEvaluation:
    """Recommend which players to keep and what to draft.

    Scores each player by keeper_value = composite_score × age_multiplier,
    recommends the top N, then evaluates the resulting keeper core.

    Args:
        full_roster_rankings: Rankings for the entire current roster.
        n_keepers: Number of players to recommend keeping.
        categories: League scoring categories.
        roster_positions: Full expected roster configuration.
        league_type: League format.
        available_pool: Optional rankings for non-rostered players.
        player_ages: Optional {player_id: age} for age-based adjustments.
        context: Optional free-text user context.

    Returns:
        KeeperEvaluation in "plan_keepers" mode with recommended keeps + cuts.
    """
    ages = player_ages or {}

    # Compute keeper value for each player
    scored: list[tuple[PlayerRanking, float]] = []
    for ranking in full_roster_rankings:
        keeper_value = ranking.score * _keeper_age_multiplier(ranking.player_id, ages)
        scored.append((ranking, round(keeper_value, 3)))

    # Sort by keeper value descending
    scored.sort(key=lambda x: x[1], reverse=True)

    n = min(n_keepers, len(scored))
    keep_rankings = [r for r, _ in scored[:n]]
    cut_rankings = [r for r, _ in scored[n:]]

    # Evaluate the recommended keeper core
    eval_result = evaluate_keepers(
        keep_rankings, categories, roster_positions, league_type, available_pool, context
    )

    # Augment with cut rationale
    if cut_rankings:
        eval_result.cons.append(
            f"Cutting: {', '.join(r.name for r in cut_rankings[:5])}"
            + (" ..." if len(cut_rankings) > 5 else "")
        )

    logger.info(
        "Keeper planning: keeping %d, cutting %d (total roster %d)",
        len(keep_rankings),
        len(cut_rankings),
        len(full_roster_rankings),
    )

    return KeeperEvaluation(
        mode="plan_keepers",
        keepers=keep_rankings,
        cuts=cut_rankings,
        keeper_foundation_grade=eval_result.keeper_foundation_grade,
        category_strengths=eval_result.category_strengths,
        category_gaps=eval_result.category_gaps,
        position_gaps=eval_result.position_gaps,
        draft_profiles=eval_result.draft_profiles,
        pros=eval_result.pros,
        cons=eval_result.cons,
        analysis_blurb="",
    )
