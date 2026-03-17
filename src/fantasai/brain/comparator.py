"""Player comparison engine.

Given a list of player rankings, compare them head-to-head with optional
user context (e.g. "I need stolen bases") that adjusts relative weighting.

Purely functional — no DB dependency. The API layer handles LLM blurb
generation and serialization.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from fantasai.engine.scoring import PlayerRanking

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category keyword patterns
# ---------------------------------------------------------------------------

# Maps scoring category names to natural-language phrases a user might type.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "R": ["runs", "run scoring"],
    "HR": ["home run", "homers", "home runs", "hr", "power", "long ball"],
    "RBI": ["rbi", "rbis", "runs batted in", "run production"],
    "SB": ["stolen bases", "stolen base", "steals", "speed", "sb"],
    "AVG": ["batting average", "average", "avg", "batting avg"],
    "OPS": ["ops", "on-base plus slugging"],
    "OBP": ["on base", "obp", "on-base percentage"],
    "SLG": ["slugging", "slg", "slugging percentage"],
    "H": ["hits", "hit total"],
    "BB": ["walks", "walk rate", "plate discipline"],
    "W": ["wins", "win", "starter wins", "pitcher wins"],
    "SV": ["saves", "save", "closer", "closers"],
    "K": ["strikeouts", "strikeout", "ks", "punch outs", "punchouts", "whiffs", "k's"],
    "ERA": ["era", "earned run average", "runs allowed"],
    "WHIP": ["whip", "walks and hits per inning"],
    "HLD": ["holds", "hold", "setup", "setup men", "hld"],
    "QS": ["quality starts", "quality start", "qs"],
    "IP": ["innings", "innings pitched", "volume"],
    "K/9": ["k per nine", "strikeouts per nine", "k/9"],
    "BB/9": ["walks per nine", "bb/9", "control"],
}

# Multiplier applied to a category's z-score when the user mentions it in context
CONTEXT_BOOST = 2.0


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ComparePlayerResult:
    """Comparison result for a single player."""

    player_id: int
    player_name: str
    team: str
    positions: list[str]
    rank: int
    composite_score: float
    category_scores: dict[str, float]  # adjusted z-scores per category
    stat_type: str
    overall_rank: int = 0    # rank among all ranked players (for percentile display)
    total_players: int = 0   # denominator for percentile


@dataclass
class CompareContext:
    """All inputs needed to compare a set of players."""

    player_rankings: list[PlayerRanking]   # pre-computed from ScoringEngine
    scoring_categories: list[str]
    context: Optional[str] = None          # free-text user hint
    ranking_type: str = "predictive"       # "predictive" | "current"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _parse_context_keywords(text: str, categories: list[str]) -> set[str]:
    """Extract scoring category names mentioned in a free-text context string.

    E.g. "I need stolen bases and saves" → {"SB", "SV"}.
    """
    lowered = text.lower()
    matched: set[str] = set()
    for cat, phrases in CATEGORY_KEYWORDS.items():
        if cat not in categories:
            continue
        for phrase in phrases:
            if re.search(r"\b" + re.escape(phrase) + r"\b", lowered):
                matched.add(cat)
                break

    if matched:
        logger.debug("Context '%s' matched categories: %s", text[:60], matched)
    return matched


def _compute_adjusted_score(
    ranking: PlayerRanking,
    categories: list[str],
    boosted_cats: set[str],
) -> tuple[float, dict[str, float]]:
    """Compute an adjusted composite score for one player.

    For each scoring category, uses the category z-score from the ranking.
    Boosted categories get their z-score multiplied by CONTEXT_BOOST.

    Returns (adjusted_composite_score, {cat: adjusted_z_score}).
    """
    adjusted_cats: dict[str, float] = {}
    total = 0.0

    for cat in categories:
        z = ranking.category_contributions.get(cat, 0.0)
        if cat in boosted_cats:
            z *= CONTEXT_BOOST
        adjusted_cats[cat] = round(z, 3)
        total += z

    return round(total, 3), adjusted_cats


# ---------------------------------------------------------------------------
# Main comparison function
# ---------------------------------------------------------------------------


def compare_players(ctx: CompareContext) -> list[ComparePlayerResult]:
    """Rank a list of players head-to-head, optionally adjusted by context.

    Args:
        ctx: CompareContext with pre-computed rankings and optional user context.

    Returns:
        List of ComparePlayerResult, sorted by adjusted composite score (best first).
    """
    if not ctx.player_rankings:
        return []

    # Parse context for category boosts
    boosted: set[str] = set()
    if ctx.context:
        boosted = _parse_context_keywords(ctx.context, ctx.scoring_categories)

    # Score each player with optional context adjustment
    scored: list[tuple[PlayerRanking, float, dict[str, float]]] = []
    for ranking in ctx.player_rankings:
        adj_score, adj_cats = _compute_adjusted_score(
            ranking, ctx.scoring_categories, boosted
        )
        scored.append((ranking, adj_score, adj_cats))

    # Sort descending by adjusted composite score
    scored.sort(key=lambda x: x[1], reverse=True)

    total = len(ctx.player_rankings)

    results: list[ComparePlayerResult] = []
    for rank, (ranking, score, cats) in enumerate(scored, start=1):
        results.append(
            ComparePlayerResult(
                player_id=ranking.player_id,
                player_name=ranking.name,
                team=ranking.team,
                positions=ranking.positions,
                rank=rank,
                composite_score=score,
                category_scores=cats,
                stat_type=ranking.stat_type,
                overall_rank=ranking.overall_rank,
                total_players=total,
            )
        )

    logger.info(
        "Compared %d players (context=%r, boosted=%s)",
        len(results),
        ctx.context,
        boosted or "none",
    )
    return results
