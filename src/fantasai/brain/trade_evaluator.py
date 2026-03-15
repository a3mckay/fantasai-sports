"""Trade evaluator: assess the fairness of a proposed trade.

Takes players + draft picks on each side and computes a talent-density-
adjusted verdict. Unlike a naive total-value comparison, this module
rewards star power concentration — trading one elite player for five
average players is penalized even if raw totals are equal, because talent
density is a key predictor of fantasy success.

Keeper league support: when has_keepers=True and player_ages is provided,
player scores are adjusted upward for younger players and downward for
declining veterans before the density calculation.

Purely functional — no DB dependency. LLM blurb generation happens in
the API layer.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from fantasai.engine.scoring import PlayerRanking

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How much emphasis to place on star power vs total depth.
# A single elite player outscores multiple average players even when raw
# totals match because star_share=1.0 gives a full 40% density bonus.
STAR_POWER_WEIGHT = 0.4

# Draft pick value heuristics (composite score equivalents).
# Pick values are added directly to side totals but excluded from the
# density calc because draft picks represent uncertain future value.
DRAFT_PICK_VALUES: dict[str, float] = {
    "1st": 3.0,
    "2nd": 1.5,
    "3rd": 0.5,
}
DRAFT_PICK_DEFAULT = 0.25

# Value differential thresholds for verdict
VERDICT_THRESHOLD = 1.5  # |adj_diff| > this → clear winner

# Age → future value multiplier table for keeper leagues.
# (min_age, max_age, multiplier)
KEEPER_AGE_BONUS: list[tuple[int, int, float]] = [
    (0, 24, 1.3),
    (25, 27, 1.1),
    (28, 30, 1.0),
    (31, 99, 0.85),
]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TradeContext:
    """All data needed to evaluate a trade."""

    giving_rankings: list[PlayerRanking]     # players being given away
    receiving_rankings: list[PlayerRanking]  # players being received
    giving_picks: list[str]                  # e.g. ["2025 1st round"]
    receiving_picks: list[str]               # e.g. ["2026 2nd round"]
    team_strengths: dict[str, float]         # pre-computed category strengths
    scoring_categories: list[str]
    league_type: str = "h2h_categories"
    has_keepers: bool = False
    context: Optional[str] = None
    # player_id → age; only relevant when has_keepers=True.
    # Future enhancement: populated from Player.birth_date when available.
    player_ages: dict[int, int] = field(default_factory=dict)


@dataclass
class TradeEvaluation:
    """Result of a trade evaluation."""

    verdict: str                       # "favor_receive" | "favor_give" | "fair"
    confidence: float                  # 0.0–1.0
    value_differential: float          # density-adjusted (positive = favor receive)
    raw_value_differential: float      # raw score totals only (for transparency)
    talent_density_note: str           # human-readable concentration analysis
    category_impact: dict[str, float]  # per-cat delta (positive = improves on receive)
    give_value: float                  # density-adjusted giving-side total
    receive_value: float               # density-adjusted receiving-side total
    pros: list[str]
    cons: list[str]
    analysis_blurb: str                # filled in by API layer via LLM


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _pick_value(pick_str: str) -> float:
    """Convert a draft pick description string to a numeric value equivalent.

    E.g. "2025 1st round" → 3.0, "2026 3rd round" → 0.5.
    """
    lower = pick_str.lower()
    for key, val in DRAFT_PICK_VALUES.items():
        if key in lower:
            return val
    return DRAFT_PICK_DEFAULT


def _keeper_age_multiplier(player_id: int, player_ages: dict[int, int]) -> float:
    """Return a future-value multiplier based on player age (keeper leagues).

    Defaults to 1.0 if age data is not available for the player.
    """
    age = player_ages.get(player_id)
    if age is None:
        return 1.0
    for lo, hi, mult in KEEPER_AGE_BONUS:
        if lo <= age <= hi:
            return mult
    return 1.0


def _adjusted_side_value(scores: list[float]) -> float:
    """Compute talent-density-adjusted total for one side of the trade.

    A single elite player scores meaningfully higher than many average
    players even if raw totals match. This reflects the real fantasy
    advantage of having concentrated elite talent.

    Formula:
        adjusted = total + (star_share × STAR_POWER_WEIGHT × total)
    where star_share = max_score / total_score.

    Examples:
        1 player at 10 → star_share=1.0 → adjusted = 10 + 0.4×10 = 14.0
        5 players at 2 → star_share=0.2 → adjusted = 10 + 0.4×0.2×10 = 10.8
        → Single elite player is ~30% more valuable than equivalent depth
    """
    if not scores:
        return 0.0
    total = sum(scores)
    if total <= 0:
        return max(total, 0.0)
    top = max(scores)
    star_share = top / total
    density_bonus = star_share * STAR_POWER_WEIGHT * total
    return total + density_bonus


def _compute_category_impact(
    giving: list[PlayerRanking],
    receiving: list[PlayerRanking],
    categories: list[str],
) -> dict[str, float]:
    """Per-category delta: positive means receiving side improves team.

    Normalizes by player count so 1-for-3 trades compare fairly on a
    per-player basis before calculating the difference.
    """
    give_total: dict[str, float] = {c: 0.0 for c in categories}
    recv_total: dict[str, float] = {c: 0.0 for c in categories}
    for r in giving:
        for cat in categories:
            give_total[cat] += r.category_contributions.get(cat, 0.0)
    for r in receiving:
        for cat in categories:
            recv_total[cat] += r.category_contributions.get(cat, 0.0)

    give_n = max(1, len(giving))
    recv_n = max(1, len(receiving))
    return {
        cat: round(recv_total[cat] / recv_n - give_total[cat] / give_n, 3)
        for cat in categories
    }


def _talent_density_note(
    give_scores: list[float],
    recv_scores: list[float],
    give_adj: float,
    recv_adj: float,
) -> str:
    """Generate a human-readable explanation of talent concentration imbalance."""

    def _star_share_str(scores: list[float]) -> str:
        if not scores or sum(scores) <= 0:
            return "N/A"
        return f"{max(scores) / sum(scores):.0%}"

    give_share = _star_share_str(give_scores)
    recv_share = _star_share_str(recv_scores)
    give_n = len(give_scores)
    recv_n = len(recv_scores)

    if give_n == 1 and recv_n > 1:
        return (
            f"Giving away 1 player (star concentration {give_share}) for "
            f"{recv_n} players (star concentration {recv_share}). "
            "Talent density strongly favors the giving side — depth rarely "
            "compensates for losing a concentrated elite contributor."
        )
    if recv_n == 1 and give_n > 1:
        return (
            f"Giving {give_n} players (star concentration {give_share}) to "
            f"receive 1 player (star concentration {recv_share}). "
            "Receiving a single elite player typically improves talent density."
        )

    adj_diff = recv_adj - give_adj
    if abs(adj_diff) < 0.5:
        return (
            f"Both sides have similar talent density "
            f"(give: {give_share}, receive: {recv_share})."
        )
    if adj_diff > 0:
        return (
            f"Receiving side has better talent density "
            f"({recv_share} vs {give_share} star concentration)."
        )
    return (
        f"Giving side has better talent density "
        f"({give_share} vs {recv_share} star concentration)."
    )


def _parse_pros_cons(text: str) -> tuple[list[str], list[str]]:
    """Parse [PROS] and [CONS] block markers from LLM response text.

    Expected format::

        [PROS]
        - Pro item one
        - Pro item two

        [CONS]
        - Con item one

    Falls back to empty lists if markers are not found.
    """
    pros: list[str] = []
    cons: list[str] = []

    pros_match = re.search(
        r"\[PROS\](.*?)(?:\[CONS\]|\Z)", text, re.DOTALL | re.IGNORECASE
    )
    cons_match = re.search(
        r"\[CONS\](.*?)(?:\[PROS\]|\Z)", text, re.DOTALL | re.IGNORECASE
    )

    def _extract(block: str) -> list[str]:
        items = []
        for line in block.strip().splitlines():
            line = re.sub(r"^[\-\*•]\s*", "", line.strip())
            if line:
                items.append(line)
        return items

    if pros_match:
        pros = _extract(pros_match.group(1))
    if cons_match:
        cons = _extract(cons_match.group(1))
    return pros, cons


# ---------------------------------------------------------------------------
# Main evaluation function
# ---------------------------------------------------------------------------


def evaluate_trade(ctx: TradeContext) -> TradeEvaluation:
    """Evaluate a trade proposal and return a structured verdict.

    Applies talent-density adjustment so that a single elite player is
    not considered equivalent to many average players with the same raw
    total. Keeper leagues apply age-based future-value multipliers when
    player age data is available.

    Note: analysis_blurb in the returned evaluation is empty — the API
    layer fills it in via the LLM.

    Args:
        ctx: TradeContext with pre-computed rankings for both sides.

    Returns:
        TradeEvaluation with verdict, confidence, category impact, and
        algorithmic pros/cons.
    """
    # -----------------------------------------------------------------
    # 1. Score individual players (with optional keeper age adjustment)
    # -----------------------------------------------------------------
    def _player_score(r: PlayerRanking) -> float:
        base = r.score
        if ctx.has_keepers and ctx.player_ages:
            base *= _keeper_age_multiplier(r.player_id, ctx.player_ages)
        return base

    give_player_scores = [_player_score(r) for r in ctx.giving_rankings]
    recv_player_scores = [_player_score(r) for r in ctx.receiving_rankings]

    # -----------------------------------------------------------------
    # 2. Draft pick values (excluded from density calc)
    # -----------------------------------------------------------------
    give_pick_scores = [_pick_value(p) for p in ctx.giving_picks]
    recv_pick_scores = [_pick_value(p) for p in ctx.receiving_picks]

    # -----------------------------------------------------------------
    # 3. Raw value totals
    # -----------------------------------------------------------------
    give_raw = sum(give_player_scores) + sum(give_pick_scores)
    recv_raw = sum(recv_player_scores) + sum(recv_pick_scores)
    raw_diff = round(recv_raw - give_raw, 3)

    # -----------------------------------------------------------------
    # 4. Density-adjusted values
    #    Density calc applies only to player scores (picks stay as-is)
    # -----------------------------------------------------------------
    give_adj = _adjusted_side_value(give_player_scores) + sum(give_pick_scores)
    recv_adj = _adjusted_side_value(recv_player_scores) + sum(recv_pick_scores)
    adj_diff = round(recv_adj - give_adj, 3)

    # -----------------------------------------------------------------
    # 5. Verdict + confidence
    # -----------------------------------------------------------------
    if adj_diff > VERDICT_THRESHOLD:
        verdict = "favor_receive"
    elif adj_diff < -VERDICT_THRESHOLD:
        verdict = "favor_give"
    else:
        verdict = "fair"

    confidence = round(min(abs(adj_diff) / 5.0, 1.0), 3)

    # -----------------------------------------------------------------
    # 6. Category impact (per-player average delta)
    # -----------------------------------------------------------------
    cat_impact = _compute_category_impact(
        ctx.giving_rankings, ctx.receiving_rankings, ctx.scoring_categories
    )

    # -----------------------------------------------------------------
    # 7. Talent density note
    # -----------------------------------------------------------------
    density_note = _talent_density_note(
        give_player_scores, recv_player_scores, give_adj, recv_adj
    )

    # -----------------------------------------------------------------
    # 8. Algorithmic pros / cons (API layer appends LLM-generated ones)
    # -----------------------------------------------------------------
    algo_pros: list[str] = []
    algo_cons: list[str] = []

    improving_cats = [c for c, v in cat_impact.items() if v > 0.3]
    declining_cats = [c for c, v in cat_impact.items() if v < -0.3]

    if improving_cats:
        algo_pros.append(f"Improves {', '.join(improving_cats)}")
    if declining_cats:
        algo_cons.append(f"Weakens {', '.join(declining_cats)}")

    if recv_adj > give_adj + 0.3:
        algo_pros.append(
            f"Net density-adjusted gain of {abs(adj_diff):.1f} points"
        )
    elif give_adj > recv_adj + 0.3:
        algo_cons.append(
            f"Net density-adjusted loss of {abs(adj_diff):.1f} points"
        )

    if ctx.has_keepers and ctx.player_ages:
        young_recv = [
            r for r in ctx.receiving_rankings
            if ctx.player_ages.get(r.player_id, 99) <= 25
        ]
        if young_recv:
            age_str = str(ctx.player_ages.get(young_recv[0].player_id, "?"))
            algo_pros.append(
                f"Keeper upside: {', '.join(r.name for r in young_recv)}"
                f" (age {age_str})"
            )

    logger.info(
        "Trade evaluated: give_adj=%.2f, recv_adj=%.2f, verdict=%s (conf=%.2f)",
        give_adj,
        recv_adj,
        verdict,
        confidence,
    )

    return TradeEvaluation(
        verdict=verdict,
        confidence=confidence,
        value_differential=adj_diff,
        raw_value_differential=raw_diff,
        talent_density_note=density_note,
        category_impact=cat_impact,
        give_value=round(give_adj, 3),
        receive_value=round(recv_adj, 3),
        pros=algo_pros,
        cons=algo_cons,
        analysis_blurb="",  # filled in by API layer
    )
