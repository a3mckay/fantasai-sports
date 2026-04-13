"""League-wide analysis: team comparisons and power rankings.

Provides two capabilities:
  1. compare_teams()   — head-to-head comparison of 2+ teams with trade
     opportunity surfacing (works for any set of team rosters, not just
     teams in the same league).
  2. compute_league_power() — full league power rankings: power scores,
     tier groupings (contender / middle / rebuilding), and the most
     complementary trade pairs across the league.

Purely functional — no DB dependency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fantasai.brain.recommender import _compute_team_strengths, _identify_weak_categories
from fantasai.engine.scoring import PlayerRanking

logger = logging.getLogger(__name__)

# Surplus / deficit thresholds for trade opportunity detection
TRADE_SURPLUS_THRESHOLD = 1.0   # category z-sum above this = surplus
TRADE_DEFICIT_THRESHOLD = -0.5  # category z-sum below this = deficit

# Cap on trade opportunities returned in a league power report
MAX_TRADE_OPPS_LEAGUE = 10


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TeamSnapshot:
    """Computed summary for a single team."""

    team_id: int
    team_name: str
    power_score: float                  # sum of all roster composite z-scores
    average_score: float                # mean composite z-score per player (talent density)
    category_strengths: dict[str, float]
    strong_cats: list[str]              # top third of categories by strength
    weak_cats: list[str]                # bottom third / auto-detected weak
    top_players: list[str]              # top 3 player names by composite score


@dataclass
class TradeOpportunity:
    """A complementary trade pair between two teams."""

    team_a_id: int
    team_b_id: int
    team_a_gives_cats: list[str]        # A's surplus (= B's need)
    team_b_gives_cats: list[str]        # B's surplus (= A's need)
    suggested_give: Optional[str]       # best player A can offer
    suggested_receive: Optional[str]    # best player B can offer
    complementarity_score: float        # size of the mutual need overlap
    rationale: str


@dataclass
class TeamsComparison:
    """Result of comparing 2+ specific teams."""

    snapshots: list[TeamSnapshot]            # sorted by power_score desc
    winner: int                              # team_id of the strongest team
    trade_opportunities: list[TradeOpportunity]
    analysis_blurb: str                      # filled by API layer


@dataclass
class LeaguePowerReport:
    """Full league power rankings report."""

    power_rankings: list[TeamSnapshot]       # sorted best → worst
    tiers: dict[str, list[int]]              # {tier: [team_ids]}
    trade_opportunities: list[TradeOpportunity]  # top N most complementary
    analysis_blurb: str                      # filled by API layer


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _build_team_snapshot(
    team_id: int,
    team_name: str,
    roster_rankings: list[PlayerRanking],
    categories: list[str],
    league_type: str,
) -> TeamSnapshot:
    """Compute a TeamSnapshot from a roster's PlayerRanking objects."""
    if not roster_rankings:
        return TeamSnapshot(
            team_id=team_id,
            team_name=team_name,
            power_score=0.0,
            average_score=0.0,
            category_strengths={c: 0.0 for c in categories},
            strong_cats=[],
            weak_cats=list(categories),
            top_players=[],
        )

    power_score = round(sum(r.score for r in roster_rankings), 3)
    average_score = round(sum(r.score for r in roster_rankings) / len(roster_rankings), 3)
    category_strengths = _compute_team_strengths(roster_rankings, categories)
    weak_cats, punted_cats = _identify_weak_categories(category_strengths, league_type)

    sorted_cats = sorted(category_strengths.items(), key=lambda x: x[1], reverse=True)
    n_strong = max(1, len(sorted_cats) // 3)
    strong_cats = [c for c, _ in sorted_cats[:n_strong]]

    top_players = [r.name for r in sorted(roster_rankings, key=lambda r: r.score, reverse=True)[:3]]

    return TeamSnapshot(
        team_id=team_id,
        team_name=team_name,
        power_score=power_score,
        average_score=average_score,
        category_strengths=category_strengths,
        strong_cats=strong_cats,
        weak_cats=weak_cats + punted_cats,
        top_players=top_players,
    )


def _find_best_trade_player(
    roster_rankings: list[PlayerRanking],
    target_cats: list[str],
) -> Optional[str]:
    """Find the best player on a roster who contributes to the target categories.

    Returns the player's name or None.
    """
    if not target_cats or not roster_rankings:
        return None

    best_player: Optional[PlayerRanking] = None
    best_contribution = -float("inf")

    for ranking in roster_rankings:
        contribution = sum(
            ranking.category_contributions.get(cat, 0.0)
            for cat in target_cats
        )
        if contribution > best_contribution:
            best_contribution = contribution
            best_player = ranking

    return best_player.name if best_player and best_contribution > 0 else None


def _detect_trade_opportunity(
    snap_a: TeamSnapshot,
    snap_b: TeamSnapshot,
    rankings_a: list[PlayerRanking],
    rankings_b: list[PlayerRanking],
) -> Optional[TradeOpportunity]:
    """Detect a complementary trade opportunity between two teams.

    A trade is worth surfacing when:
    - Team A has a surplus in categories Team B needs, AND
    - Team B has a surplus in categories Team A needs.

    Returns None if no complementary overlap exists.
    """
    cats_a = set(snap_a.category_strengths.keys())
    cats_b = set(snap_b.category_strengths.keys())
    all_cats = cats_a | cats_b

    a_surplus = {c for c in all_cats if snap_a.category_strengths.get(c, 0) > TRADE_SURPLUS_THRESHOLD}
    a_deficit = {c for c in all_cats if snap_a.category_strengths.get(c, 0) < TRADE_DEFICIT_THRESHOLD}
    b_surplus = {c for c in all_cats if snap_b.category_strengths.get(c, 0) > TRADE_SURPLUS_THRESHOLD}
    b_deficit = {c for c in all_cats if snap_b.category_strengths.get(c, 0) < TRADE_DEFICIT_THRESHOLD}

    # Mutual complementarity: A gives from surplus to fill B's deficit, vice-versa
    a_gives = sorted(a_surplus & b_deficit)  # A has what B needs
    b_gives = sorted(b_surplus & a_deficit)  # B has what A needs

    if not a_gives or not b_gives:
        return None

    complementarity_score = len(a_gives) + len(b_gives)

    suggested_give = _find_best_trade_player(rankings_a, a_gives)
    suggested_receive = _find_best_trade_player(rankings_b, b_gives)

    rationale = (
        f"{snap_a.team_name} has surplus {', '.join(a_gives)} that {snap_b.team_name} needs; "
        f"{snap_b.team_name} has surplus {', '.join(b_gives)} that {snap_a.team_name} needs."
    )

    return TradeOpportunity(
        team_a_id=snap_a.team_id,
        team_b_id=snap_b.team_id,
        team_a_gives_cats=a_gives,
        team_b_gives_cats=b_gives,
        suggested_give=suggested_give,
        suggested_receive=suggested_receive,
        complementarity_score=float(complementarity_score),
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Main functions
# ---------------------------------------------------------------------------


def compare_teams(
    team_data: list[tuple[int, str, list[PlayerRanking]]],
    categories: list[str],
    league_type: str,
    include_trades: bool = True,
) -> TeamsComparison:
    """Compare 2+ teams head-to-head.

    Args:
        team_data: List of (team_id, team_name, roster_rankings) tuples.
        categories: League scoring categories.
        league_type: League format.
        include_trades: Whether to run trade opportunity detection.

    Returns:
        TeamsComparison sorted by power score descending.
    """
    if not team_data:
        return TeamsComparison(snapshots=[], winner=-1, trade_opportunities=[], analysis_blurb="")

    # Build snapshots
    snapshots = [
        _build_team_snapshot(tid, name, rankings, categories, league_type)
        for tid, name, rankings in team_data
    ]
    snapshots.sort(key=lambda s: s.power_score, reverse=True)
    winner_id = snapshots[0].team_id if snapshots else -1

    # Trade opportunities
    trade_opps: list[TradeOpportunity] = []
    if include_trades and len(team_data) >= 2:
        rankings_by_id = {tid: rankings for tid, _, rankings in team_data}
        snaps_by_id = {s.team_id: s for s in snapshots}

        # Check all pairs
        team_ids = [s.team_id for s in snapshots]
        for i in range(len(team_ids)):
            for j in range(i + 1, len(team_ids)):
                a_id, b_id = team_ids[i], team_ids[j]
                opp = _detect_trade_opportunity(
                    snaps_by_id[a_id],
                    snaps_by_id[b_id],
                    rankings_by_id.get(a_id, []),
                    rankings_by_id.get(b_id, []),
                )
                if opp:
                    trade_opps.append(opp)

        # Sort by complementarity score descending
        trade_opps.sort(key=lambda o: o.complementarity_score, reverse=True)

    logger.info(
        "Compared %d teams, winner=%d, trade_opps=%d",
        len(snapshots),
        winner_id,
        len(trade_opps),
    )

    return TeamsComparison(
        snapshots=snapshots,
        winner=winner_id,
        trade_opportunities=trade_opps,
        analysis_blurb="",
    )


def compute_league_power(
    team_data: list[tuple[int, str, list[PlayerRanking]]],
    categories: list[str],
    league_type: str,
) -> LeaguePowerReport:
    """Compute full league power rankings with tier groupings.

    Args:
        team_data: List of (team_id, team_name, roster_rankings) tuples
            for ALL teams in the league.
        categories: League scoring categories.
        league_type: League format.

    Returns:
        LeaguePowerReport with power rankings, tiers, and top trade pairs.
    """
    if not team_data:
        return LeaguePowerReport(power_rankings=[], tiers={}, trade_opportunities=[], analysis_blurb="")

    comparison = compare_teams(team_data, categories, league_type, include_trades=True)
    ranked = comparison.snapshots  # already sorted by power_score desc

    # Tier groupings
    n = len(ranked)
    top_third = max(1, n // 3)
    bottom_third = max(1, n // 3)

    tiers: dict[str, list[int]] = {
        "contender": [s.team_id for s in ranked[:top_third]],
        "middle": [s.team_id for s in ranked[top_third: n - bottom_third]],
        "rebuilding": [s.team_id for s in ranked[n - bottom_third:]],
    }

    # Cap trade opportunities to the most complementary pairs
    top_opps = comparison.trade_opportunities[:MAX_TRADE_OPPS_LEAGUE]

    logger.info(
        "League power report: %d teams, tiers=%s, trade_opps=%d",
        n,
        {k: len(v) for k, v in tiers.items()},
        len(top_opps),
    )

    return LeaguePowerReport(
        power_rankings=ranked,
        tiers=tiers,
        trade_opportunities=top_opps,
        analysis_blurb="",
    )
