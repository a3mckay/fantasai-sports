"""Scoring engine: compute player rankings using z-score methodology.

For each scoring category, every player gets a z-score (standard deviations
above/below the mean). The composite score is the sum of z-scores across
all categories. This naturally rewards multi-category contributors.

For "lower is better" stats (ERA, WHIP), z-scores are inverted so that
lower raw values produce higher z-scores.

Positional scarcity is applied as a bonus: positions with fewer quality
options get a boost so that e.g. a good catcher ranks higher than a
comparably-performing first baseman.

Predictive rankings use category-projection rather than direct advanced-metric
z-scores.  See ``engine/projection.py`` for the projection formulas.  This
ensures that volume (IP/PA) bounds reliever upside and that category
contributions are on the same scale as lookback rankings.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from fantasai.adapters.base import NormalizedPlayerData, SportAdapter
from fantasai.engine.projection import (
    HorizonConfig,
    ProjectionHorizon,
    HORIZON_CONFIGS,
    project_hitter_stats,
    project_pitcher_stats,
)

logger = logging.getLogger(__name__)

# Stats where lower values are better — z-scores get inverted
LOWER_IS_BETTER = {"ERA", "WHIP", "BB/9", "HR/9", "FIP", "xFIP", "SIERA", "xERA"}

# Per-category z-score cap.  Without this, rare winner-take-all categories
# (SV, SB) produce outliers of +5–6σ that overwhelm all other contributions.
# A 40-save elite closer is genuinely valuable — this cap keeps them top-30
# without letting a single category override multi-category workhorses like
# elite SPs.  ±3.5 was chosen empirically: it keeps elite closers at ~#30,
# elite SPs at ~#15–20, and doesn't compress the rest of the pool.
Z_SCORE_CAP = 3.5

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
    # Injury fields — carried through from NormalizedPlayerData for display.
    injury_status: str = "active"
    risk_flag: Optional[str] = None
    risk_note: Optional[str] = None


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
        horizon: ProjectionHorizon = ProjectionHorizon.SEASON,
        steamer_lookup: Optional[dict[int, NormalizedPlayerData]] = None,
    ) -> list[PlayerRanking]:
        """Rank players by expected future performance using category projection.

        Instead of z-scoring advanced metrics directly, this method:
          1. Projects each player's expected fantasy category stats (HR, SB, K, ERA…)
             using Steamer projections as the talent signal (when available) blended
             with YTD actuals, falling back to xStats-derived estimates otherwise.
          2. Scales projected counting stats to the horizon's PA/IP volume — this
             naturally bounds reliever upside (62 IP season vs 170 IP for SPs).
          3. Z-scores the *projected category stats* through the same pipeline as
             the lookback model, making the two ranking modes directly comparable.

        ``steamer_lookup``: optional dict mapping player_id → NormalizedPlayerData
        loaded from the season+1 Steamer rows in PlayerStats.  When provided, the
        projection functions substitute Steamer's ERA/K9/AVG/OBP/etc. for the
        homegrown xStats derivations, giving better pre-season projections that
        already embed age curves, role context, and regression-to-mean.
        """
        if players is None:
            players = self.adapter.fetch_player_data(season, week)

        config = HORIZON_CONFIGS[horizon]
        sl = steamer_lookup or {}

        batters = [p for p in players if p.stat_type == "batting"]
        # SP takes priority: a player with both SP+RP in their positions (e.g. a
        # two-way arm like Bubba Chandler) is treated as a starter, not a closer.
        # This prevents double-counting a player in both pools, which would let
        # dedup select the inflated RP/saves score over the correct SP score.
        starters = [p for p in players if p.stat_type == "pitching" and "SP" in p.positions]
        relievers = [
            p for p in players
            if p.stat_type == "pitching" and "RP" in p.positions and "SP" not in p.positions
        ]
        unknown_pitchers = [
            p for p in players
            if p.stat_type == "pitching" and "SP" not in p.positions and "RP" not in p.positions
        ]

        batter_rankings = self._score_category_projection(
            batters, config, self.hitting_cats, is_sp=None, steamer_lookup=sl,
        )
        # Score all pitchers in ONE combined pool so that volume differences
        # (SP 170 IP vs RP 62 IP) create real z-score variance in IP and K.
        # In separate pools every SP projects 170 IP → std(IP)=0 → IP z-score=0
        # for everyone, erasing the starter volume advantage entirely.
        all_pitchers = starters + relievers + unknown_pitchers
        pitcher_rankings = self._score_category_projection(
            all_pitchers, config, self.pitching_cats, is_sp=None,
            detect_pitcher_role=True, steamer_lookup=sl,
        )

        all_rankings = batter_rankings + pitcher_rankings
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

    def _score_category_projection(
        self,
        players: list[NormalizedPlayerData],
        config: HorizonConfig,
        categories: list[str],
        is_sp: Optional[bool],
        detect_pitcher_role: bool = False,
        steamer_lookup: Optional[dict[int, NormalizedPlayerData]] = None,
    ) -> list[PlayerRanking]:
        """Score players by projecting category stats then z-scoring those projections.

        This is the engine behind ``compute_predictive_rankings``.  The flow is:
          1. Call projection module to get expected counting/rate stats per player,
             using Steamer projections as the talent signal when available.
          2. Run the same z-score loop as ``_score_lookback`` on the projected values.
          3. Return PlayerRanking objects with category_contributions keyed by
             fantasy category name — directly comparable to lookback contributions.

        ``is_sp``:
          None  → hitter projection
          True  → starting pitcher projection
          False → reliever projection
        """
        if not players or not categories:
            return []

        sl = steamer_lookup or {}

        # Step 1: Project stats for each player into flat dicts
        projected: list[dict[str, float]] = []
        for p in players:
            steamer = sl.get(p.player_id)
            if is_sp is None and not detect_pitcher_role:
                projected.append(project_hitter_stats(p, config, steamer_data=steamer))
            elif detect_pitcher_role:
                # Combined pitcher pool: each player uses their own role's IP volume
                player_is_sp = "SP" in p.positions
                projected.append(project_pitcher_stats(p, config, is_sp=player_is_sp, steamer_data=steamer))
            else:
                projected.append(project_pitcher_stats(p, config, is_sp=is_sp, steamer_data=steamer))

        # Step 2: Extract per-category raw values from projected dicts
        cat_values: dict[str, list[Optional[float]]] = {}
        for cat in categories:
            mapping = CATEGORY_STAT_MAP.get(cat)
            if mapping is None:
                logger.warning("Unknown category in projection: %s", cat)
                continue
            _bucket, stat_name = mapping
            vals: list[Optional[float]] = []
            for proj in projected:
                v = proj.get(stat_name)
                # Handle K → SO alias (CATEGORY_STAT_MAP uses "SO" as the stat name for "K")
                if v is None and stat_name == "SO":
                    v = proj.get("K")
                vals.append(v)
            cat_values[cat] = vals

        # Step 3: Z-score each category (same logic as _score_lookback)
        cat_zscores: dict[str, list[float]] = {}
        for cat, values in cat_values.items():
            clean = [v for v in values if v is not None]
            if len(clean) < 2:
                cat_zscores[cat] = [0.0] * len(players)
                continue
            mean = float(np.mean(clean))
            std = float(np.std(clean))
            if std == 0:
                cat_zscores[cat] = [0.0] * len(players)
                continue
            zscores: list[float] = []
            for v in values:
                if v is None:
                    zscores.append(0.0)
                else:
                    z = float((v - mean) / std)
                    if cat in LOWER_IS_BETTER:
                        z = -z
                    z = max(-Z_SCORE_CAP, min(Z_SCORE_CAP, z))
                    zscores.append(z)
            cat_zscores[cat] = zscores

        # Step 4: Build PlayerRanking objects
        rankings: list[PlayerRanking] = []
        for i, player in enumerate(players):
            contributions = {
                cat: float(round(cat_zscores[cat][i], 3))
                for cat in cat_zscores
            }
            raw_score = sum(contributions.values())
            scarcity_mult = _get_scarcity_multiplier(player.positions)
            rankings.append(
                PlayerRanking(
                    player_id=player.player_id,
                    name=player.name,
                    team=player.team,
                    positions=player.positions,
                    stat_type=player.stat_type,
                    score=round(raw_score * scarcity_mult, 3),
                    raw_score=round(raw_score, 3),
                    category_contributions=contributions,
                    injury_status=getattr(player, "injury_status", "active"),
                    risk_flag=getattr(player, "risk_flag", None),
                    risk_note=getattr(player, "risk_note", None),
                )
            )

        rankings.sort(key=lambda r: r.score, reverse=True)
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
                    # Cap to prevent winner-take-all categories (SV, SB) from
                    # dominating the composite score
                    z = max(-Z_SCORE_CAP, min(Z_SCORE_CAP, z))
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
                    injury_status=getattr(player, "injury_status", "active"),
                    risk_flag=getattr(player, "risk_flag", None),
                    risk_note=getattr(player, "risk_note", None),
                )
            )

        rankings.sort(key=lambda r: r.score, reverse=True)
        return rankings

    def _score_predictive(
        self,
        players: list[NormalizedPlayerData],
        weights: dict[tuple[str, str], float],
        stat_type: str,
        categories: list[str] | None = None,
    ) -> list[PlayerRanking]:
        """Compute weighted z-score rankings using predictive stats.

        The composite *score* is derived from advanced/predictive metrics
        (xERA, Stuff+, xwOBA, etc.) so ranking order reflects true talent.

        *category_contributions* is keyed by **fantasy category name** (R, HR,
        SV, ERA …) so the comparator and UI can display a meaningful per-cat
        breakdown and apply context boosts (e.g. "I need saves").  When
        `categories` is provided, these z-scores are computed from the same
        counting/rate stats as the lookback model; otherwise the dict is empty.
        """
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

        # Step 3: Weighted combination → composite score (advanced-stats based)
        adv_scores: list[float] = []
        for i, _player in enumerate(players):
            raw_score = 0.0
            for key, weight in weights.items():
                z = stat_zscores.get(key, [0.0] * len(players))[i]
                raw_score += z * weight
            adv_scores.append(raw_score)

        # Step 4: Per-fantasy-category z-scores for display / context boosting
        # (same logic as _score_lookback; uses counting/rate stats)
        cat_contributions: list[dict[str, float]] = [{} for _ in players]
        if categories:
            cat_values: dict[str, list[Optional[float]]] = {}
            for cat in categories:
                mapping = CATEGORY_STAT_MAP.get(cat)
                if mapping is None:
                    continue
                bucket, stat_name = mapping
                cat_values[cat] = [_get_stat_value(p, bucket, stat_name) for p in players]

            cat_zscores: dict[str, list[float]] = {}
            for cat, values in cat_values.items():
                clean = [v for v in values if v is not None]
                if len(clean) < 2:
                    cat_zscores[cat] = [0.0] * len(players)
                    continue
                mean = np.mean(clean)
                std = np.std(clean)
                if std == 0:
                    cat_zscores[cat] = [0.0] * len(players)
                    continue
                zs: list[float] = []
                for v in values:
                    if v is None:
                        zs.append(0.0)
                    else:
                        z = float((v - mean) / std)
                        if cat in LOWER_IS_BETTER:
                            z = -z
                        z = max(-Z_SCORE_CAP, min(Z_SCORE_CAP, z))
                        zs.append(z)
                cat_zscores[cat] = zs

            for i in range(len(players)):
                for cat in cat_zscores:
                    cat_contributions[i][cat] = float(round(cat_zscores[cat][i], 3))

        # Step 5: Build PlayerRanking objects
        rankings = []
        for i, player in enumerate(players):
            raw_score = adv_scores[i]
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
                    category_contributions=cat_contributions[i],
                    injury_status=getattr(player, "injury_status", "active"),
                    risk_flag=getattr(player, "risk_flag", None),
                    risk_note=getattr(player, "risk_note", None),
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
