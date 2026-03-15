"""Waiver wire recommender: identifies best available pickups for a team.

Purely functional design — receives all data via WaiverContext, returns
plain dataclasses. No DB dependency. The API layer handles persistence.

Algorithm:
1. Partition rankings into "my roster" vs "available pool"
2. Compute team strength profile (sum z-scores per category)
3. Identify weak categories (roto: bottom third; H2H: bottom half of non-punted)
4. Apply build preferences (punt overrides, pitcher strategy, priority targets)
5. Score available players by need-weighted category impact (30% lookback + 70% predictive)
6. Filter by position eligibility, apply pitcher strategy position bonuses
7. Suggest drop candidates for each recommendation
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from fantasai.engine.scoring import (
    HITTING_CATEGORIES,
    PITCHING_CATEGORIES,
    PlayerRanking,
)

logger = logging.getLogger(__name__)

# Blend weights for waiver recommendations — favor predictive (future upside)
LOOKBACK_WEIGHT = 0.30
PREDICTIVE_WEIGHT = 0.70

# Multi-position flexibility bonus (multiplier on final score)
MULTI_POSITION_BONUS = 1.05

# Punt detection threshold for H2H leagues — if a team's total z-score
# in a category is below this, assume they're punting it
PUNT_THRESHOLD = -3.0

# Priority target multiplier — categories the user explicitly wants to target
# get this multiplier on top of their need weight. Stacks with weak-cat boost.
PRIORITY_TARGET_MULTIPLIER = 1.5

# Pitcher strategy adjustments — how category weights and position scoring
# change based on the user's pitcher build philosophy
PITCHER_STRATEGY_ADJUSTMENTS: dict[str, dict] = {
    "rp_heavy": {
        "category_boosts": {"SV": 1.5, "HLD": 1.5},
        "category_dampens": {"W": 0.5, "QS": 0.5},
        "position_bonus": {"RP": 1.10},
        "position_penalty": {"SP": 0.90},
    },
    "sp_heavy": {
        "category_boosts": {"W": 1.3, "K": 1.3, "QS": 1.3},
        "category_dampens": {"SV": 0.5, "HLD": 0.5},
        "position_bonus": {"SP": 1.10},
        "position_penalty": {"RP": 0.90},
    },
    "balanced": {},
}

# Position slot eligibility mapping
# Maps roster slot names to which player positions can fill them
SLOT_ELIGIBILITY: dict[str, set[str]] = {
    "C": {"C"},
    "1B": {"1B"},
    "2B": {"2B"},
    "3B": {"3B"},
    "SS": {"SS"},
    "LF": {"LF", "OF"},
    "CF": {"CF", "OF"},
    "RF": {"RF", "OF"},
    "OF": {"LF", "CF", "RF", "OF"},
    "Util": HITTING_CATEGORIES,  # placeholder — handled specially below
    "SP": {"SP"},
    "RP": {"RP"},
    "P": {"SP", "RP"},
    "BN": set(),  # bench accepts anyone — handled specially
    "IL": set(),  # IL accepts anyone with IL status — handled specially
    "IL+": set(),
    "NA": set(),
    "DH": {"DH"},
}

# All hitter positions (for Util slot eligibility)
HITTER_POSITIONS = {"C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "DH"}
PITCHER_POSITIONS = {"SP", "RP"}

# Minimum pitching thresholds for drop-candidate safety checks.
# MIN_ROSTER_PITCHERS: refuse to suggest a drop that leaves fewer than this many
#   pitchers on the active roster (always enforced regardless of IP data).
# MIN_WEEKLY_IP: warn when the team's rolling-window IP sum would fall below
#   this after the drop.  Only applied when team_pitcher_ip data is available.
MIN_ROSTER_PITCHERS: int = 3
MIN_WEEKLY_IP: float = 15.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BuildPreferences:
    """User-configurable preferences that shape waiver recommendations.

    pitcher_strategy: "rp_heavy" (closers + elite relievers), "sp_heavy",
        or "balanced" (default, no adjustment).
    punt_positions: Positions the user is intentionally leaving empty
        (e.g., ["C"] for zero-catcher strategy). Disables drop protection
        for those slots and filters out players who only fill punted positions.
    punt_categories: Categories the user is intentionally punting
        (e.g., ["SB"]). Overrides auto-detection — these always get weight 0.
    priority_targets: Categories to prioritize beyond what the algorithm
        would normally suggest (e.g., ["SV", "K"]). Gets a 1.5x weight boost.
    """

    pitcher_strategy: str = "balanced"
    punt_positions: list[str] = field(default_factory=list)
    punt_categories: list[str] = field(default_factory=list)
    priority_targets: list[str] = field(default_factory=list)


@dataclass
class WaiverContext:
    """All data needed to generate waiver recommendations for one team."""

    team_id: int
    roster_player_ids: list[int]
    league_type: str  # "h2h_categories", "roto", "points"
    scoring_categories: list[str]
    roster_positions: list[str]
    max_acquisitions_remaining: int
    all_rankings: list[PlayerRanking]  # lookback rankings, ALL players
    predictive_rankings: list[PlayerRanking]  # predictive rankings, ALL players
    all_rostered_ids: set[int]  # player_ids rostered by ANY team in the league
    build_preferences: Optional[BuildPreferences] = None
    # Optional: player_id → recent IP (from rolling window stats).
    # When provided, drop candidates that would reduce team IP below
    # MIN_WEEKLY_IP will receive an ip_warning rather than being silently skipped.
    team_pitcher_ip: dict[int, float] = field(default_factory=dict)


@dataclass
class DropCandidate:
    """A rostered player who could be dropped to make room for a pickup."""

    player_id: int
    player_name: str
    positions: list[str]
    current_score: float
    category_contributions: dict[str, float]
    net_impact: float  # team score change if we swap (add - drop)
    ip_warning: Optional[str] = None  # set when dropping risks pitching floor


@dataclass
class WaiverRecommendation:
    """A single waiver wire recommendation."""

    player_id: int
    player_name: str
    team: str
    positions: list[str]
    priority_score: float
    category_impact: dict[str, float]
    fills_positions: list[str]
    weak_categories_addressed: list[str]
    drop_candidates: list[DropCandidate]
    action: str
    rationale_blurb: Optional[str] = None


# ---------------------------------------------------------------------------
# Helper functions (module-level for testability)
# ---------------------------------------------------------------------------


def _compute_team_strengths(
    roster_rankings: list[PlayerRanking],
    categories: list[str],
) -> dict[str, float]:
    """Sum z-scores per category across the roster.

    Returns a dict mapping category name to total team z-score for that category.
    Higher = stronger in that category.
    """
    strengths: dict[str, float] = {cat: 0.0 for cat in categories}
    for ranking in roster_rankings:
        for cat in categories:
            strengths[cat] += ranking.category_contributions.get(cat, 0.0)
    return strengths


def _identify_weak_categories(
    strengths: dict[str, float],
    league_type: str,
    punt_threshold: float = PUNT_THRESHOLD,
) -> tuple[list[str], list[str]]:
    """Identify which categories the team is weak in.

    Returns (weak_categories, punted_categories).

    For roto: bottom third of categories are "weak".
    For H2H: auto-detect punted categories (very negative z-score),
    then weak = bottom half of non-punted categories.
    """
    if not strengths:
        return [], []

    sorted_cats = sorted(strengths.items(), key=lambda x: x[1])

    if league_type == "h2h_categories":
        punted = [cat for cat, score in sorted_cats if score < punt_threshold]
        non_punted = [(cat, score) for cat, score in sorted_cats if cat not in punted]
        if not non_punted:
            return [], list(strengths.keys())
        mid = max(1, len(non_punted) // 2)
        weak = [cat for cat, _ in non_punted[:mid]]
        return weak, punted
    else:
        # Roto or points: bottom third are weak
        n = max(1, len(sorted_cats) // 3)
        weak = [cat for cat, _ in sorted_cats[:n]]
        return weak, []


def _compute_need_weights(
    strengths: dict[str, float],
    weak_categories: list[str],
    punted_categories: list[str],
) -> dict[str, float]:
    """Compute per-category weights based on team needs.

    Weak categories get higher weight. Punted categories get zero weight.
    Strong categories get baseline weight of 1.0.
    """
    weights: dict[str, float] = {}
    for cat, strength in strengths.items():
        if cat in punted_categories:
            weights[cat] = 0.0
        elif cat in weak_categories:
            # Weaker categories get higher weight — inverse relationship
            # Floor at 1.5, cap at 3.0
            weights[cat] = min(3.0, max(1.5, 2.0 - strength))
        else:
            weights[cat] = 1.0
    return weights


def _apply_build_preferences(
    need_weights: dict[str, float],
    weak_cats: list[str],
    punted_cats: list[str],
    preferences: BuildPreferences,
) -> tuple[dict[str, float], list[str], list[str]]:
    """Apply user build preferences to need weights and category lists.

    Modifies need_weights in place and returns updated (weak_cats, punted_cats).
    This is the central point where all preference overrides happen.
    """
    # 1. Merge user punt_categories into punted set
    for cat in preferences.punt_categories:
        if cat not in punted_cats:
            punted_cats.append(cat)
        if cat in weak_cats:
            weak_cats.remove(cat)
        need_weights[cat] = 0.0

    # 2. Apply pitcher_strategy category adjustments
    adjustments = PITCHER_STRATEGY_ADJUSTMENTS.get(preferences.pitcher_strategy, {})
    for cat, multiplier in adjustments.get("category_boosts", {}).items():
        if cat in need_weights:
            need_weights[cat] *= multiplier
    for cat, multiplier in adjustments.get("category_dampens", {}).items():
        if cat in need_weights:
            need_weights[cat] *= multiplier

    # 3. Apply priority_targets multiplier
    for cat in preferences.priority_targets:
        if cat in need_weights:
            need_weights[cat] *= PRIORITY_TARGET_MULTIPLIER

    return need_weights, weak_cats, punted_cats


def _get_pitcher_strategy_position_multiplier(
    player_positions: list[str],
    preferences: Optional[BuildPreferences],
) -> float:
    """Get position-based score multiplier from pitcher strategy.

    Returns a multiplier (e.g., 1.10 for RP in rp_heavy strategy).
    Returns 1.0 if no adjustment applies.
    """
    if not preferences or preferences.pitcher_strategy == "balanced":
        return 1.0

    adjustments = PITCHER_STRATEGY_ADJUSTMENTS.get(preferences.pitcher_strategy, {})
    pos_set = set(player_positions)

    # Check bonuses first (player might be both SP and RP — bonus wins)
    for pos, mult in adjustments.get("position_bonus", {}).items():
        if pos in pos_set:
            return mult

    for pos, mult in adjustments.get("position_penalty", {}).items():
        if pos in pos_set:
            return mult

    return 1.0


def _player_eligible_for_slot(player_positions: list[str], slot: str) -> bool:
    """Check if a player with given positions can fill a specific roster slot."""
    if slot in ("BN", "IL", "IL+", "NA"):
        return True  # bench/IL/NA accept anyone

    if slot == "Util":
        # Util accepts any hitter
        return bool(set(player_positions) & HITTER_POSITIONS)

    eligible_positions = SLOT_ELIGIBILITY.get(slot, set())
    return bool(set(player_positions) & eligible_positions)


def _check_position_fit(
    player_positions: list[str],
    roster_positions: list[str],
) -> list[str]:
    """Return which roster slots a player could fill.

    Only returns unique slot types (not duplicates like ["BN", "BN"]).
    """
    seen: set[str] = set()
    fillable: list[str] = []
    for slot in roster_positions:
        if slot in seen:
            continue
        if _player_eligible_for_slot(player_positions, slot):
            fillable.append(slot)
            seen.add(slot)
    return fillable


def _score_available_player(
    lookback: Optional[PlayerRanking],
    predictive: Optional[PlayerRanking],
    need_weights: dict[str, float],
) -> tuple[float, dict[str, float]]:
    """Compute need-weighted blended score for an available player.

    Returns (blended_score, category_impact_dict).
    """
    lookback_score = 0.0
    predictive_score = 0.0
    category_impact: dict[str, float] = {}

    if lookback:
        for cat, weight in need_weights.items():
            z = lookback.category_contributions.get(cat, 0.0)
            lookback_score += z * weight

    if predictive:
        for cat, weight in need_weights.items():
            z = predictive.category_contributions.get(cat, 0.0)
            predictive_score += z * weight

    # Compute per-category impact (blended z-scores, unweighted by need)
    all_cats = set()
    if lookback:
        all_cats.update(lookback.category_contributions.keys())
    if predictive:
        all_cats.update(predictive.category_contributions.keys())

    for cat in all_cats:
        lb_z = lookback.category_contributions.get(cat, 0.0) if lookback else 0.0
        pr_z = predictive.category_contributions.get(cat, 0.0) if predictive else 0.0
        blended_z = LOOKBACK_WEIGHT * lb_z + PREDICTIVE_WEIGHT * pr_z
        category_impact[cat] = round(blended_z, 3)

    blended_score = LOOKBACK_WEIGHT * lookback_score + PREDICTIVE_WEIGHT * predictive_score
    return blended_score, category_impact


def _find_drop_candidates(
    roster_rankings: list[PlayerRanking],
    add_player_score: float,
    add_player_contributions: dict[str, float],
    need_weights: dict[str, float],
    roster_positions: list[str],
    add_player_positions: list[str] | None = None,
    punt_positions: list[str] | None = None,
    team_pitcher_ip: dict[int, float] | None = None,
    max_candidates: int = 3,
) -> list[DropCandidate]:
    """Find the weakest rostered players who could be dropped.

    Ensures we don't drop the only player eligible for a required position slot
    UNLESS the incoming player can also fill that slot OR the position is punted.

    Pitcher floor safety:
    - Hard floor: dropping a pitcher that would leave fewer than MIN_ROSTER_PITCHERS
      pitchers on the roster will skip that candidate entirely.
    - Soft floor: if team_pitcher_ip is provided and dropping a pitcher would leave
      the team below MIN_WEEKLY_IP innings, the candidate is still surfaced but
      receives an ip_warning string so the user can make an informed decision.

    Returns up to max_candidates, ordered by best drop (highest net_impact) first.
    """
    if not roster_rankings:
        return []

    add_positions = add_player_positions or []
    punted_pos = set(punt_positions or [])
    ip_map = team_pitcher_ip or {}

    # Count how many players fill each required (non-bench) position slot
    required_slots = [s for s in roster_positions if s not in ("BN", "IL", "IL+", "NA")]
    position_counts: dict[str, int] = {}
    for slot in set(required_slots):
        count = sum(
            1
            for r in roster_rankings
            if _player_eligible_for_slot(r.positions, slot)
        )
        position_counts[slot] = count

    # Pre-compute total pitcher count and total IP for floor checks
    roster_pitcher_ids = {
        r.player_id
        for r in roster_rankings
        if any(p in PITCHER_POSITIONS for p in r.positions)
    }
    total_roster_pitchers = len(roster_pitcher_ids)

    # Score each rostered player by their need-weighted contribution
    scored_roster: list[tuple[PlayerRanking, float]] = []
    for ranking in roster_rankings:
        player_contribution = sum(
            ranking.category_contributions.get(cat, 0.0) * weight
            for cat, weight in need_weights.items()
        )
        scored_roster.append((ranking, player_contribution))

    # Sort by contribution ascending (worst player first = best drop candidate)
    scored_roster.sort(key=lambda x: x[1])

    candidates: list[DropCandidate] = []
    for ranking, contribution in scored_roster:
        if len(candidates) >= max_candidates:
            break

        # Check if dropping this player would leave a required slot unfillable
        # A slot is safe if: (a) other roster players can fill it, or
        # (b) the incoming add player can fill it, or (c) the position is punted
        is_sole_eligible = False
        for slot in set(required_slots):
            if slot in punted_pos:
                continue  # don't protect punted position slots
            if (
                _player_eligible_for_slot(ranking.positions, slot)
                and position_counts.get(slot, 0) <= 1
                and not _player_eligible_for_slot(add_positions, slot)
            ):
                is_sole_eligible = True
                break

        if is_sole_eligible:
            continue

        # ------------------------------------------------------------------ #
        # Pitcher floor check
        # ------------------------------------------------------------------ #
        ip_warning: Optional[str] = None
        is_pitcher = any(p in PITCHER_POSITIONS for p in ranking.positions)

        if is_pitcher:
            # Hard floor: silently skip if the drop would leave too few pitchers
            # (the incoming add player doesn't help unless they're also a pitcher)
            add_is_pitcher = any(p in PITCHER_POSITIONS for p in add_positions)
            remaining_pitchers = total_roster_pitchers - 1 + (1 if add_is_pitcher else 0)
            if remaining_pitchers < MIN_ROSTER_PITCHERS:
                logger.debug(
                    "Skipping drop of %s — would leave only %d pitchers (floor: %d)",
                    ranking.name,
                    remaining_pitchers,
                    MIN_ROSTER_PITCHERS,
                )
                continue

            # Soft floor: warn if we'd go below the weekly IP minimum
            if ip_map:
                remaining_ip = sum(
                    ip for pid, ip in ip_map.items()
                    if pid != ranking.player_id
                )
                if remaining_ip < MIN_WEEKLY_IP:
                    ip_warning = (
                        f"Dropping would leave ~{remaining_ip:.1f} IP "
                        f"(floor: {MIN_WEEKLY_IP:.0f} IP)"
                    )
        # ------------------------------------------------------------------ #

        net_impact = add_player_score - contribution
        candidates.append(
            DropCandidate(
                player_id=ranking.player_id,
                player_name=ranking.name,
                positions=ranking.positions,
                current_score=ranking.score,
                category_contributions=ranking.category_contributions,
                net_impact=round(net_impact, 3),
                ip_warning=ip_warning,
            )
        )

    # Sort by net_impact descending (best swap first)
    candidates.sort(key=lambda c: c.net_impact, reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# Main recommender class
# ---------------------------------------------------------------------------


class Recommender:
    """Generates league-aware waiver wire recommendations.

    Purely functional: receives all data via WaiverContext, returns
    plain dataclasses. No DB session needed.
    """

    def __init__(
        self,
        categories: list[str],
        league_type: str = "h2h_categories",
    ) -> None:
        self.categories = categories
        self.hitting_cats = [c for c in categories if c in HITTING_CATEGORIES]
        self.pitching_cats = [c for c in categories if c in PITCHING_CATEGORIES]
        self.league_type = league_type

    def get_waiver_recommendations(
        self,
        context: WaiverContext,
        limit: int = 15,
    ) -> list[WaiverRecommendation]:
        """Return top waiver pickups for the team, ordered by priority_score.

        Args:
            context: All data needed for recommendations (roster, rankings, etc.)
            limit: Maximum number of recommendations to return.

        Returns:
            List of WaiverRecommendation, sorted by priority_score descending.
        """
        # Early exit: no acquisitions remaining
        if context.max_acquisitions_remaining <= 0:
            logger.info("Team %s has no acquisitions remaining", context.team_id)
            return []

        prefs = context.build_preferences

        # Step 1: Partition rankings
        roster_set = set(context.roster_player_ids)

        my_lookback = [
            r for r in context.all_rankings if r.player_id in roster_set
        ]

        available_lookback = {
            r.player_id: r
            for r in context.all_rankings
            if r.player_id not in context.all_rostered_ids
        }
        available_predictive = {
            r.player_id: r
            for r in context.predictive_rankings
            if r.player_id not in context.all_rostered_ids
        }

        # All available player IDs (union of lookback + predictive)
        available_ids = set(available_lookback.keys()) | set(available_predictive.keys())

        if not available_ids:
            logger.info("No available players for team %s", context.team_id)
            return []

        # Step 2: Team strength profile
        strengths = _compute_team_strengths(my_lookback, context.scoring_categories)
        logger.debug("Team %s strengths: %s", context.team_id, strengths)

        # Step 3: Identify weak categories (base algorithm)
        weak_cats, punted_cats = _identify_weak_categories(
            strengths, context.league_type
        )
        logger.debug(
            "Team %s weak: %s, punted: %s", context.team_id, weak_cats, punted_cats
        )

        # Step 4: Need weights (base)
        need_weights = _compute_need_weights(strengths, weak_cats, punted_cats)

        # Step 4b: Apply build preferences (punt overrides, pitcher strategy, priority targets)
        if prefs:
            need_weights, weak_cats, punted_cats = _apply_build_preferences(
                need_weights, weak_cats, punted_cats, prefs
            )
            logger.debug(
                "Team %s preferences applied: strategy=%s, punt_pos=%s, punt_cats=%s, priority=%s",
                context.team_id,
                prefs.pitcher_strategy,
                prefs.punt_positions,
                prefs.punt_categories,
                prefs.priority_targets,
            )

        # Punt positions set (for filtering + drop protection)
        punt_pos = prefs.punt_positions if prefs else []

        # Step 5: Score all available players
        scored_players: list[tuple[int, float, dict[str, float]]] = []
        for pid in available_ids:
            lb = available_lookback.get(pid)
            pr = available_predictive.get(pid)
            score, cat_impact = _score_available_player(lb, pr, need_weights)

            # Get player info from whichever ranking we have
            player_info = lb or pr
            if player_info is None:
                continue

            # Skip players whose ONLY positions are all punted
            if punt_pos and all(p in punt_pos for p in player_info.positions):
                continue

            # Multi-position flexibility bonus
            if len(player_info.positions) > 1:
                score *= MULTI_POSITION_BONUS

            # Pitcher strategy position bonus/penalty
            strategy_mult = _get_pitcher_strategy_position_multiplier(
                player_info.positions, prefs
            )
            score *= strategy_mult

            scored_players.append((pid, score, cat_impact))

        # Sort by score descending
        scored_players.sort(key=lambda x: x[1], reverse=True)

        # Step 6: Build recommendations with position fit + drop candidates
        roster_ranking_map: dict[int, PlayerRanking] = {}
        for r in my_lookback:
            roster_ranking_map[r.player_id] = r

        roster_for_drops = list(roster_ranking_map.values())

        recommendations: list[WaiverRecommendation] = []
        for pid, score, cat_impact in scored_players:
            if len(recommendations) >= limit:
                break

            # Get player info
            player_info = available_lookback.get(pid) or available_predictive.get(pid)
            if player_info is None:
                continue

            # Position eligibility check
            fills = _check_position_fit(
                player_info.positions, context.roster_positions
            )
            active_fills = [s for s in fills if s not in ("BN", "IL", "IL+", "NA")]
            if not active_fills and not fills:
                continue

            # Find drop candidates
            drops = _find_drop_candidates(
                roster_for_drops,
                add_player_score=score,
                add_player_contributions=cat_impact,
                need_weights=need_weights,
                roster_positions=context.roster_positions,
                add_player_positions=player_info.positions,
                punt_positions=punt_pos,
                team_pitcher_ip=context.team_pitcher_ip or None,
            )

            # Determine which weak categories this player addresses
            addressed = [
                cat
                for cat in weak_cats
                if cat_impact.get(cat, 0.0) > 0.0
            ]

            # Build action string
            action = f"Add {player_info.name} ({'/'.join(player_info.positions)})"
            if drops:
                action += f" — drop {drops[0].player_name}"

            recommendations.append(
                WaiverRecommendation(
                    player_id=pid,
                    player_name=player_info.name,
                    team=player_info.team,
                    positions=player_info.positions,
                    priority_score=round(score, 3),
                    category_impact=cat_impact,
                    fills_positions=fills,
                    weak_categories_addressed=addressed,
                    drop_candidates=drops,
                    action=action,
                    rationale_blurb=None,
                )
            )

        logger.info(
            "Generated %d waiver recommendations for team %s",
            len(recommendations),
            context.team_id,
        )
        return recommendations

    def get_trade_targets(self, context: WaiverContext) -> list[dict]:
        """Identify trade targets that address team needs.

        Not implemented in this phase.
        """
        raise NotImplementedError("Trade target analysis coming in a future phase")
