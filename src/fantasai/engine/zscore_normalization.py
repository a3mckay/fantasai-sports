"""Z-score normalization for the three-component Rest of Season ranking formula.

Provides:
- Statcast composite z-scores: groups correlated metrics so no single cluster of
  metrics can dominate the composite score.
- Percentile classification: Elite/Above average/Average/Below average/Poor labels
  with actual population data, passed into blurb prompts so the model never invents
  benchmarks from training data.
- Replacement level anchor: shifts the final blended score so the Nth player = 0,
  making above/below-replacement value explicit.

All functions are stateless — the full eligible player pool is passed in each time
so normalisation is always relative to the current population.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from fantasai.adapters.base import NormalizedPlayerData


# ---------------------------------------------------------------------------
# Composite metric groupings
# ---------------------------------------------------------------------------
# Each group = list of (metric_name, lower_is_better).
# Within each group, per-player z-scores are averaged (after direction correction).
# The three group composites are then averaged to produce the Statcast component.
#
# Batter metrics live in advanced_stats unless noted.
# Pitcher metrics are "against" stats, so direction is reversed vs batter versions.

BATTER_COMPOSITES: dict[str, list[tuple[str, bool]]] = {
    "contact_quality": [
        ("xwOBA",      False),   # higher = better
        ("Barrel%",    False),
        ("Sweet-Spot%",False),   # 8–32° launch angle
        ("HardHit%",   False),
    ],
    "plate_discipline": [
        ("K%",    True),    # lower K% = better for batters (rate_stats)
        ("BB%",   False),   # higher BB% = better              (rate_stats)
        ("SwStr%",True),    # lower whiff = better
    ],
    "speed_power": [
        ("Sprint Speed", False),  # populated via separate Statcast merge
        ("PulledFB%",    False),  # derived: Pull% × FB% / 100
    ],
}

PITCHER_COMPOSITES: dict[str, list[tuple[str, bool]]] = {
    "stuff": [
        ("vFA",      False),   # fastball velocity — canonical name after adapter normalization
        ("SpinRate", False),   # fastball spin rate
        ("Ext",      False),   # release extension
        ("Stuff+",   False),   # FanGraphs Stuff+ (>100 = above average)
    ],
    "contact_mgmt": [
        ("xBA",     True),    # xBA against: lower = better for pitcher
        ("Barrel%", True),    # Barrel% against
        ("HardHit%",True),    # HardHit% against
    ],
    "command_outcomes": [
        ("SIERA",     True),   # lower = better
        ("xERA",      True),
        ("xFIP",      True),
        ("K-BB%",    False),   # higher = better
        ("CSW%",     False),
        ("O-Swing%", False),   # chase rate — higher = better for pitcher
        ("SwStr%",   False),   # whiff rate — higher = better for pitcher
    ],
}

# Where each batter metric lives in NormalizedPlayerData
_BATTER_METRIC_BUCKET: dict[str, str] = {
    "xwOBA":       "advanced_stats",
    "Barrel%":     "advanced_stats",
    "Sweet-Spot%": "advanced_stats",
    "HardHit%":    "advanced_stats",
    "K%":          "rate_stats",
    "BB%":         "rate_stats",
    "SwStr%":      "advanced_stats",
    "Sprint Speed":"advanced_stats",
    "PulledFB%":   "advanced_stats",
}

_PITCHER_METRIC_BUCKET: dict[str, str] = {
    "vFA":       "advanced_stats",
    "SpinRate":  "advanced_stats",
    "Ext":       "advanced_stats",
    "Stuff+":    "advanced_stats",
    "xBA":       "advanced_stats",
    "Barrel%":   "advanced_stats",
    "HardHit%":  "advanced_stats",
    "SIERA":     "advanced_stats",
    "xERA":      "advanced_stats",
    "xFIP":      "advanced_stats",
    "K-BB%":     "advanced_stats",
    "CSW%":      "advanced_stats",
    "O-Swing%":  "advanced_stats",
    "SwStr%":    "advanced_stats",
}

# ---------------------------------------------------------------------------
# Percentile classification
# ---------------------------------------------------------------------------

PERCENTILE_LABELS = [
    (90, "Elite"),
    (70, "Above average"),
    (30, "Average"),
    (10, "Below average"),
    (0,  "Poor"),
]


def classify_percentile(pct: float) -> str:
    """Return a human-readable tier label for a 0–100 percentile rank."""
    for threshold, label in PERCENTILE_LABELS:
        if pct >= threshold:
            return label
    return "Poor"


def compute_percentile_data(
    players: list[NormalizedPlayerData],
    metrics: list[tuple[str, str, bool]],
) -> list[dict[str, dict]]:
    """Compute per-player percentile data for a set of metrics.

    Args:
        players: Full eligible pool.
        metrics: List of (metric_name, bucket, lower_is_better) tuples.

    Returns:
        One dict per player: {metric_name: {value, pct, label, avg}} where
        pct is the percentile rank (0–100) in the eligible pool.
        lower_is_better flips the rank (low value → high percentile).
    """
    # Collect raw values per metric
    raw: dict[str, list[Optional[float]]] = {}
    for (name, bucket, _) in metrics:
        raw[name] = [
            _get_val(getattr(p, bucket, {}), name)
            for p in players
        ]

    results: list[dict[str, dict]] = [{} for _ in players]

    for (name, _, lower_is_better) in metrics:
        values = raw[name]
        numeric = [(i, v) for i, v in enumerate(values) if v is not None]
        if len(numeric) < 2:
            continue

        pop_values = [v for _, v in numeric]
        avg = float(np.mean(pop_values))
        # Sort indices by value to assign percentile ranks
        sorted_indices = sorted(range(len(pop_values)), key=lambda x: pop_values[x])
        n = len(sorted_indices)
        rank_map: dict[int, float] = {}
        for rank_pos, orig_pos in enumerate(sorted_indices):
            # percentile = rank from 0 (lowest value) to 100 (highest value)
            pct = (rank_pos / (n - 1)) * 100.0 if n > 1 else 50.0
            if lower_is_better:
                pct = 100.0 - pct   # invert: low value → high percentile
            rank_map[numeric[orig_pos][0]] = pct

        for player_idx, v in numeric:
            pct = rank_map[player_idx]
            results[player_idx][name] = {
                "value": round(v, 4),
                "pct":   round(pct, 1),
                "label": classify_percentile(pct),
                "avg":   round(avg, 4),
            }

    return results


def _get_val(stats_dict: dict, key: str) -> Optional[float]:
    v = stats_dict.get(key)
    if v is None:
        return None
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Statcast composite z-scores
# ---------------------------------------------------------------------------

def compute_statcast_composites(
    players: list[NormalizedPlayerData],
    stat_type: str,
) -> list[float]:
    """Compute per-player Statcast composite z-scores for the full pool.

    Flow:
    1. For each composite group, z-score each member metric across the pool.
    2. Apply direction correction (invert for lower_is_better metrics).
    3. Average available metric z-scores within each group.
    4. Average the group composites → player's Statcast component score.

    Missing metrics get z-score = 0 (population mean), so gaps degrade
    gracefully without inflating or penalising any player.

    Returns a list of floats (one per player), same order as input.
    """
    composites = BATTER_COMPOSITES if stat_type == "batting" else PITCHER_COMPOSITES
    bucket_map = _BATTER_METRIC_BUCKET if stat_type == "batting" else _PITCHER_METRIC_BUCKET

    n = len(players)
    if n == 0:
        return []

    # group_scores[group_name] = array of composite z-scores, shape (n,)
    group_scores: dict[str, np.ndarray] = {}

    for group_name, metric_list in composites.items():
        group_matrix: list[np.ndarray] = []

        for metric_name, lower_is_better in metric_list:
            bucket = bucket_map.get(metric_name, "advanced_stats")
            values = np.array([
                _get_val(getattr(p, bucket, {}), metric_name) or np.nan
                for p in players
            ], dtype=float)

            valid_mask = ~np.isnan(values)
            if valid_mask.sum() < 2:
                # Not enough data — skip this metric
                continue

            pop_mean = float(np.nanmean(values))
            pop_std  = float(np.nanstd(values))
            if pop_std == 0:
                continue

            z = np.where(valid_mask, (values - pop_mean) / pop_std, 0.0)
            if lower_is_better:
                z = -z

            group_matrix.append(z)

        if not group_matrix:
            group_scores[group_name] = np.zeros(n)
        else:
            # Average z-scores across available metrics in the group
            group_scores[group_name] = np.mean(np.vstack(group_matrix), axis=0)

    if not group_scores:
        return [0.0] * n

    # Average the group composites
    composite = np.mean(np.vstack(list(group_scores.values())), axis=0)
    return composite.tolist()


# ---------------------------------------------------------------------------
# Replacement level anchor
# ---------------------------------------------------------------------------

def apply_replacement_level(
    scores: list[float],
    replacement_rank: int,
) -> list[float]:
    """Shift scores so the player at replacement_rank = 0.

    Players above replacement level get positive scores; below get negative.
    Scores are assumed to already be sorted or will be sorted internally.

    Args:
        scores: Composite scores for all players (unsorted).
        replacement_rank: 1-indexed rank of the replacement-level player
            (170 for batters, 115 for pitchers).

    Returns:
        Shifted scores, same order as input.
    """
    if not scores:
        return []

    sorted_scores = sorted(scores, reverse=True)
    # replacement_rank is 1-indexed; clamp to valid range
    idx = min(max(replacement_rank - 1, 0), len(sorted_scores) - 1)
    anchor = sorted_scores[idx]
    return [s - anchor for s in scores]
