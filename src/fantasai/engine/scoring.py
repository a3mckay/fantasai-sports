"""Scoring engine: compute player rankings using z-score methodology.

For each scoring category, every player gets a z-score (standard deviations
above/below the mean). The composite score is the sum of z-scores across
all categories. This naturally rewards multi-category contributors.

For "lower is better" stats (ERA, WHIP), z-scores are inverted so that
lower raw values produce higher z-scores.

Positional scarcity is applied as a bonus: positions with fewer quality
options get a boost so that e.g. a good catcher ranks higher than a
comparably-performing first baseman.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from fantasai.adapters.base import NormalizedPlayerData, SportAdapter

logger = logging.getLogger(__name__)

# Stats where lower values are better — z-scores get inverted
LOWER_IS_BETTER = {"ERA", "WHIP", "BB/9", "HR/9", "FIP", "xFIP", "SIERA", "xERA"}

# Category -> which stat bucket it lives in (counting_stats, rate_stats, advanced_stats)
# and the actual column name in that bucket
CATEGORY_STAT_MAP = {
    # Hitting categories
    "R": ("counting_stats", "R"),
    "HR": ("counting_stats", "HR"),
    "RBI": ("counting_stats", "RBI"),
    "SB": ("counting_stats", "SB"),
    "AVG": ("rate_stats", "AVG"),
    "OPS": ("rate_stats", "OPS"),
    "OBP": ("rate_stats", "OBP"),
    "SLG": ("rate_stats", "SLG"),
    "H": ("counting_stats", "H"),
    "BB": ("counting_stats", "BB"),
    # Pitching categories — "K" maps to "SO" in pybaseball
    "IP": ("counting_stats", "IP"),
    "W": ("counting_stats", "W"),
    "SV": ("counting_stats", "SV"),
    "K": ("counting_stats", "SO"),
    "SO": ("counting_stats", "SO"),
    "ERA": ("rate_stats", "ERA"),
    "WHIP": ("rate_stats", "WHIP"),
    "HLD": ("counting_stats", "HLD"),
    "K/9": ("rate_stats", "K/9"),
    "BB/9": ("rate_stats", "BB/9"),
    "QS": ("counting_stats", "QS"),
}

# Which categories apply to hitters vs pitchers
HITTING_CATEGORIES = {"R", "HR", "RBI", "SB", "AVG", "OPS", "OBP", "SLG", "H", "BB"}
PITCHING_CATEGORIES = {"IP", "W", "SV", "K", "SO", "ERA", "WHIP", "HLD", "K/9", "BB/9", "QS"}

# Predictive stat weights — how much each underlying metric matters for
# predicting future performance. Higher weight = more important.
PREDICTIVE_HITTING_WEIGHTS = {
    ("advanced_stats", "xwOBA"): 3.0,
    ("advanced_stats", "xBA"): 2.0,
    ("advanced_stats", "xSLG"): 2.0,
    ("advanced_stats", "Barrel%"): 2.5,
    ("advanced_stats", "HardHit%"): 2.0,
    ("advanced_stats", "EV"): 1.5,
    ("advanced_stats", "Spd"): 1.5,
    ("advanced_stats", "SwStr%"): -1.0,  # higher whiff = worse for hitters
    ("advanced_stats", "CSW%"): -0.5,
    ("advanced_stats", "LD%"): 1.0,
    ("advanced_stats", "wRC+"): 2.5,
    ("rate_stats", "BB%"): 1.0,
    ("rate_stats", "K%"): -1.0,
}

PREDICTIVE_PITCHING_WEIGHTS = {
    ("advanced_stats", "xERA"): -3.0,  # lower xERA = better, but we invert
    ("advanced_stats", "xFIP"): -2.5,
    ("advanced_stats", "SIERA"): -2.5,
    ("advanced_stats", "Stuff+"): 3.0,
    ("advanced_stats", "CSW%"): 2.5,
    ("advanced_stats", "K-BB%"): 3.0,
    ("advanced_stats", "SwStr%"): 2.0,
    ("advanced_stats", "GB%"): 1.0,
    ("advanced_stats", "HardHit%"): -2.0,  # lower hard hit% = better for pitchers
    ("advanced_stats", "Barrel%"): -2.0,
    ("rate_stats", "K%"): 2.0,
    ("rate_stats", "BB%"): -1.5,
}

# Positional scarcity multipliers — positions with fewer quality options
# get a boost. Based on typical fantasy baseball positional depth.
POSITIONAL_SCARCITY = {
    "C": 1.15,
    "SS": 1.05,
    "2B": 1.03,
    "3B": 1.02,
    "OF": 1.00,
    "1B": 0.98,
    "DH": 0.95,
    "SP": 1.00,
    "RP": 1.08,
}


@dataclass
class PlayerRanking:
    """A single player's ranking result."""

    player_id: int
    name: str
    team: str
    positions: list[str]
    stat_type: str
    overall_rank: int = 0
    position_rank: int = 0
    score: float = 0.0
    raw_score: float = 0.0  # before positional scarcity
    category_contributions: dict[str, float] = field(default_factory=dict)


def _get_stat_value(
    player: NormalizedPlayerData, bucket: str, stat_name: str
) -> Optional[float]:
    """Extract a stat value from the appropriate bucket."""
    stats_dict = getattr(player, bucket, {})
    val = stats_dict.get(stat_name)
    if val is None:
        return None
    try:
        f = float(val)
        if np.isnan(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


class ScoringEngine:
    """Computes player rankings based on configurable stat categories."""

    def __init__(self, adapter: SportAdapter, categories: list[str]) -> None:
        self.adapter = adapter
        self.categories = categories
        self.hitting_cats = [c for c in categories if c in HITTING_CATEGORIES]
        self.pitching_cats = [c for c in categories if c in PITCHING_CATEGORIES]

    def compute_lookback_rankings(
        self,
        season: int,
        week: Optional[int] = None,
        players: Optional[list[NormalizedPlayerData]] = None,
    ) -> list[PlayerRanking]:
        """Rank players by season-to-date performance using z-scores.

        If players is provided, uses that data directly. Otherwise fetches
        from the adapter.
        """
        if players is None:
            players = self.adapter.fetch_player_data(season, week)

        batters = [p for p in players if p.stat_type == "batting"]
        pitchers = [p for p in players if p.stat_type == "pitching"]

        batter_rankings = self._score_lookback(batters, self.hitting_cats)
        pitcher_rankings = self._score_lookback(pitchers, self.pitching_cats)

        all_rankings = batter_rankings + pitcher_rankings
        all_rankings.sort(key=lambda r: r.score, reverse=True)
        for i, r in enumerate(all_rankings):
            r.overall_rank = i + 1

        _assign_position_ranks(all_rankings)
        return all_rankings

    def compute_predictive_rankings(
        self,
        season: int,
        week: Optional[int] = None,
        players: Optional[list[NormalizedPlayerData]] = None,
    ) -> list[PlayerRanking]:
        """Rank players by expected future performance using predictive stats.

        SP and RP are scored in separate pools to prevent reliever rate-stat
        dominance (relievers have inherently better K-BB%, SwStr% because they
        throw max effort for 1 inning). Z-scores are computed within each pool
        so an elite SP is compared to other SPs, not to closers.
        """
        if players is None:
            players = self.adapter.fetch_player_data(season, week)

        batters = [p for p in players if p.stat_type == "batting"]
        starters = [p for p in players if p.stat_type == "pitching" and "SP" in p.positions]
        relievers = [p for p in players if p.stat_type == "pitching" and "RP" in p.positions]
        # Pitchers with no position (shouldn't happen after fix, but defensive)
        unknown_pitchers = [
            p for p in players
            if p.stat_type == "pitching" and "SP" not in p.positions and "RP" not in p.positions
        ]

        batter_rankings = self._score_predictive(
            batters, PREDICTIVE_HITTING_WEIGHTS, "batting"
        )
        sp_rankings = self._score_predictive(
            starters, PREDICTIVE_PITCHING_WEIGHTS, "pitching"
        )
        rp_rankings = self._score_predictive(
            relievers, PREDICTIVE_PITCHING_WEIGHTS, "pitching"
        )
        unknown_rankings = self._score_predictive(
            unknown_pitchers, PREDICTIVE_PITCHING_WEIGHTS, "pitching"
        )

        all_rankings = batter_rankings + sp_rankings + rp_rankings + unknown_rankings
        all_rankings.sort(key=lambda r: r.score, reverse=True)
        for i, r in enumerate(all_rankings):
            r.overall_rank = i + 1

        _assign_position_ranks(all_rankings)
        return all_rankings

    def compute_window_rankings(
        self,
        rolling_stats: list[dict],
    ) -> list[PlayerRanking]:
        """Rank players within a rolling window using z-score methodology.

        Rolling window stats come from Baseball Reference (via the pipeline)
        and only include counting + rate stats — no advanced metrics. The
        ranking reflects genuine recent performance rather than projections.

        Args:
            rolling_stats: List of dicts with keys:
                player_id, name, team, positions, stat_type,
                counting_stats (dict), rate_stats (dict)

        Returns:
            PlayerRanking list sorted best → worst, with overall_rank set.
            category_contributions are z-scores within the window pool.
        """
        if not rolling_stats:
            return []

        # Separate batters and pitchers — score in their respective pools
        batters = [r for r in rolling_stats if r["stat_type"] == "batting"]
        pitchers = [r for r in rolling_stats if r["stat_type"] == "pitching"]

        batter_rankings = self._score_window_pool(batters, self.hitting_cats)
        pitcher_rankings = self._score_window_pool(pitchers, self.pitching_cats)

        all_rankings = batter_rankings + pitcher_rankings
        all_rankings.sort(key=lambda r: r.score, reverse=True)
        for i, r in enumerate(all_rankings):
            r.overall_rank = i + 1

        return all_rankings

    def _score_window_pool(
        self,
        pool: list[dict],
        categories: list[str],
    ) -> list[PlayerRanking]:
        """Score a pool of rolling-window records using z-scores.

        Works the same as _score_lookback but operates on pre-fetched dicts
        rather than NormalizedPlayerData (BRef stats have no advanced bucket).
        """
        if not pool or not categories:
            return []

        # Gather per-category raw values
        cat_values: dict[str, list[Optional[float]]] = {}
        for cat in categories:
            mapping = CATEGORY_STAT_MAP.get(cat)
            if mapping is None:
                continue
            bucket, stat_name = mapping
            vals = []
            for rec in pool:
                # bucket is "counting_stats" or "rate_stats" in BRef records
                if bucket == "counting_stats":
                    v = rec["counting_stats"].get(stat_name)
                elif bucket == "rate_stats":
                    v = rec["rate_stats"].get(stat_name)
                    # BRef K column stored as K (renamed from SO in adapter)
                    if v is None and stat_name == "SO":
                        v = rec["counting_stats"].get("K")
                else:
                    v = None
                vals.append(float(v) if v is not None else None)
            cat_values[cat] = vals

        # Compute per-category z-scores
        cat_zscores: dict[str, list[float]] = {}
        for cat, vals in cat_values.items():
            numeric = [v for v in vals if v is not None]
            if len(numeric) < 2:
                cat_zscores[cat] = [0.0] * len(pool)
                continue
            mean = float(np.mean(numeric))
            std = float(np.std(numeric))
            if std == 0:
                cat_zscores[cat] = [0.0] * len(pool)
                continue
            inverted = cat in LOWER_IS_BETTER
            z_raw = [(((v - mean) / std) if v is not None else 0.0) for v in vals]
            cat_zscores[cat] = [(-z if inverted else z) for z in z_raw]

        rankings = []
        for idx, rec in enumerate(pool):
            contributions = {cat: cat_zscores[cat][idx] for cat in cat_zscores}
            raw_score = sum(contributions.values())
            scarcity = max(
                (POSITIONAL_SCARCITY.get(pos, 1.0) for pos in rec.get("positions", [])),
                default=1.0,
            )
            rankings.append(
                PlayerRanking(
                    player_id=rec["player_id"],
                    name=rec["name"],
                    team=rec["team"],
                    positions=rec.get("positions", []),
                    stat_type=rec["stat_type"],
                    score=raw_score * scarcity,
                    raw_score=raw_score,
                    category_contributions=contributions,
                )
            )

        return rankings

    def _score_lookback(
        self, players: list[NormalizedPlayerData], categories: list[str]
    ) -> list[PlayerRanking]:
        """Compute z-score based rankings for a set of players and categories."""
        if not players or not categories:
            return []

        # Step 1: Extract raw values for each category
        cat_values: dict[str, list[Optional[float]]] = {}
        for cat in categories:
            mapping = CATEGORY_STAT_MAP.get(cat)
            if mapping is None:
                logger.warning(f"Unknown category: {cat}")
                continue
            bucket, stat_name = mapping
            cat_values[cat] = [_get_stat_value(p, bucket, stat_name) for p in players]

        # Step 2: Compute z-scores per category
        cat_zscores: dict[str, list[float]] = {}
        for cat, values in cat_values.items():
            clean_values = [v for v in values if v is not None]
            if len(clean_values) < 2:
                cat_zscores[cat] = [0.0] * len(players)
                continue

            mean = np.mean(clean_values)
            std = np.std(clean_values)
            if std == 0:
                cat_zscores[cat] = [0.0] * len(players)
                continue

            zscores: list[float] = []
            for v in values:
                if v is None:
                    zscores.append(0.0)
                else:
                    z = float((v - mean) / std)
                    # Invert for "lower is better" stats
                    if cat in LOWER_IS_BETTER:
                        z = -z
                    zscores.append(z)
            cat_zscores[cat] = zscores

        # Step 3: Combine z-scores into composite score + apply scarcity
        rankings = []
        for i, player in enumerate(players):
            contributions: dict[str, float] = {}
            raw_score = 0.0
            for cat in cat_zscores:
                z = cat_zscores[cat][i]
                contributions[cat] = float(round(z, 3))
                raw_score += z

            scarcity_mult = _get_scarcity_multiplier(player.positions)
            score = raw_score * scarcity_mult

            rankings.append(
                PlayerRanking(
                    player_id=player.player_id,
                    name=player.name,
                    team=player.team,
                    positions=player.positions,
                    stat_type=player.stat_type,
                    score=round(score, 3),
                    raw_score=round(raw_score, 3),
                    category_contributions=contributions,
                )
            )

        rankings.sort(key=lambda r: r.score, reverse=True)
        return rankings

    def _score_predictive(
        self,
        players: list[NormalizedPlayerData],
        weights: dict[tuple[str, str], float],
        stat_type: str,
    ) -> list[PlayerRanking]:
        """Compute weighted z-score rankings using predictive stats."""
        if not players:
            return []

        # Step 1: Extract raw values for each weighted stat
        stat_values: dict[tuple[str, str], list[Optional[float]]] = {}
        for (bucket, stat_name) in weights:
            stat_values[(bucket, stat_name)] = [
                _get_stat_value(p, bucket, stat_name) for p in players
            ]

        # Step 2: Z-score each stat
        stat_zscores: dict[tuple[str, str], list[float]] = {}
        for key, values in stat_values.items():
            clean_values = [v for v in values if v is not None]
            if len(clean_values) < 2:
                stat_zscores[key] = [0.0] * len(players)
                continue

            mean = np.mean(clean_values)
            std = np.std(clean_values)
            if std == 0:
                stat_zscores[key] = [0.0] * len(players)
                continue

            zscores: list[float] = []
            for v in values:
                if v is None:
                    zscores.append(0.0)
                else:
                    zscores.append(float((v - mean) / std))
            stat_zscores[key] = zscores

        # Step 3: Weighted combination
        rankings = []
        for i, player in enumerate(players):
            contributions: dict[str, float] = {}
            raw_score = 0.0
            for key, weight in weights.items():
                bucket, stat_name = key
                z = stat_zscores.get(key, [0.0] * len(players))[i]
                weighted_z = z * weight
                contributions[stat_name] = float(round(weighted_z, 3))
                raw_score += weighted_z

            scarcity_mult = _get_scarcity_multiplier(player.positions)
            score = raw_score * scarcity_mult

            rankings.append(
                PlayerRanking(
                    player_id=player.player_id,
                    name=player.name,
                    team=player.team,
                    positions=player.positions,
                    stat_type=stat_type,
                    score=round(score, 3),
                    raw_score=round(raw_score, 3),
                    category_contributions=contributions,
                )
            )

        rankings.sort(key=lambda r: r.score, reverse=True)
        return rankings


def _get_scarcity_multiplier(positions: list[str]) -> float:
    """Return the highest scarcity multiplier among a player's positions."""
    if not positions:
        return 1.0
    return max(POSITIONAL_SCARCITY.get(pos, 1.0) for pos in positions)


def _assign_position_ranks(rankings: list[PlayerRanking]) -> None:
    """Assign position-specific ranks within a ranked list."""
    position_counters: dict[str, int] = {}
    for r in rankings:
        for pos in r.positions:
            position_counters.setdefault(pos, 0)
            position_counters[pos] += 1
            if r.position_rank == 0:  # take first (highest) position rank
                r.position_rank = position_counters[pos]
