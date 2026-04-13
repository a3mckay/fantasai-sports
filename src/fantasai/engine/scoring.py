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
from fantasai.engine.zscore_normalization import (
    compute_statcast_composites,
    compute_percentile_data,
    apply_replacement_level,
    BATTER_COMPOSITES,
    PITCHER_COMPOSITES,
    _BATTER_METRIC_BUCKET,
    _PITCHER_METRIC_BUCKET,
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

# ---------------------------------------------------------------------------
# Three-component Rest of Season formula constants
# All weights are named constants for easy tuning.
# Steamer weight is derived (1 − sum of others) so all weights always sum to 1.
# ---------------------------------------------------------------------------

# Maximum weights when fully ramped (at or above the PA/IP threshold)
STATCAST_WEIGHT_MAX = 0.35
ACCUM_WEIGHT_MAX    = 0.20
LATE_DECAY_MAX      = 0.13   # additional Steamer decay after 300 PA

# PA ramp thresholds for batters
STATCAST_PA_FULL    = 150
ACCUM_PA_FULL       = 300
LATE_DECAY_PA_START = 300
LATE_DECAY_PA_FULL  = 550    # late_decay fully phased in at 550 PA

# IP ramp thresholds for pitchers (mirror at ~1/5.5 of PA equivalents)
STATCAST_IP_FULL    = 30
ACCUM_IP_FULL       = 60
LATE_DECAY_IP_START = 60
LATE_DECAY_IP_FULL  = 110

# Replacement level ranks (1-indexed)
BATTER_REPLACEMENT_RANK  = 170
PITCHER_REPLACEMENT_RANK = 115

# SP and RP category weight multipliers — applied to per-category z-scores.
# SV is nearly irrelevant for SPs; W is reduced but not zeroed for RPs.
SP_CATEGORY_WEIGHTS: dict[str, float] = {
    "IP": 1.0, "W": 1.0, "K": 1.0, "SO": 1.0,
    "ERA": 1.0, "WHIP": 1.0, "SV": 0.05, "HLD": 0.10, "QS": 1.0,
}
RP_CATEGORY_WEIGHTS: dict[str, float] = {
    "IP": 1.0, "K": 1.0, "SO": 1.0, "SV": 1.0, "HLD": 1.0,
    "ERA": 1.0, "WHIP": 1.0, "W": 0.25, "QS": 0.10,
}

# Bayesian update priors for the Steamer component.
# The Steamer projection acts as a prior belief (strength = these PA/IP).
# As a player accumulates actual-season evidence, the posterior blends toward
# their real-world performance.  At 57 PA (Walker), actual_w = 57/357 = 16%.
# At 300 PA, actual_w = 50%.  The update is applied to rate stats and per-PA
# counting rates — projected PA/IP volume is unchanged.
_STEAMER_PRIOR_PA = 300.0   # batter: "Steamer is worth 300 PA of prior evidence"
_STEAMER_PRIOR_IP = 50.0    # pitcher: "Steamer is worth 50 IP of prior evidence"

# Meaningful xStat gap thresholds for outperformer flag
# Values below these are noise; above → flag as outperformer
_OUTPERFORMER_THRESHOLDS = {
    "AVG_vs_xBA":    0.040,  # .040 or more above xBA
    "ERA_vs_xERA":  -0.50,   # ERA 0.50+ below xERA (ERA better than expected)
    "ERA_vs_SIERA": -0.50,
}
_OUTPERFORMER_MIN_PA  = 150   # don't flag below this (Tier 3 is handled separately)
_OUTPERFORMER_MIN_IP  = 35

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
    # Prospect fields — set when a MiLB player is injected into rankings via PAV.
    is_prospect: bool = False
    pav_score: Optional[float] = None
    # Three-component formula outputs (populated by compute_rest_of_season_rankings)
    statcast_score: float = 0.0
    steamer_score: float = 0.0
    accum_score: float = 0.0
    # 1=Tier1 sustained outperformer, 2=Tier2 single-season, 3=Tier3 small-sample
    outperformer_flag: Optional[int] = None
    # {metric: {value, pct, label, avg}} — blurb prompts read this for percentile language
    percentile_data: dict = field(default_factory=dict)


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
        schedule_overrides: Optional[dict[int, "HorizonConfig"]] = None,
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

        # Rest of Season uses the three-component formula; delegate to the dedicated method.
        if horizon == ProjectionHorizon.SEASON:
            return self.compute_rest_of_season_rankings(
                season=season,
                players=players,
                steamer_lookup=steamer_lookup,
            )

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
            schedule_overrides=schedule_overrides,
        )
        # Score all pitchers in ONE combined pool so that volume differences
        # (SP 170 IP vs RP 62 IP) create real z-score variance in IP and K.
        # In separate pools every SP projects 170 IP → std(IP)=0 → IP z-score=0
        # for everyone, erasing the starter volume advantage entirely.
        all_pitchers = starters + relievers + unknown_pitchers
        pitcher_rankings = self._score_category_projection(
            all_pitchers, config, self.pitching_cats, is_sp=None,
            detect_pitcher_role=True, steamer_lookup=sl,
            schedule_overrides=schedule_overrides,
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

    def compute_rest_of_season_rankings(
        self,
        season: int,
        players: Optional[list[NormalizedPlayerData]] = None,
        steamer_lookup: Optional[dict[int, NormalizedPlayerData]] = None,
    ) -> list[PlayerRanking]:
        """Three-component Rest of Season ranking formula.

        For each player, the composite score is:
            score = steamer_z  × steamer_w
                  + statcast_z × statcast_w
                  + accum_z    × accum_w

        Where the weights are dynamically computed from the player's PA (or IP)
        using continuous linear ramps — no hard thresholds, no jump discontinuities.

        Final scores are anchored at replacement level so:
          - 170th batter  → score = 0
          - 115th pitcher → score = 0

        Position scarcity multipliers are applied post-anchoring.
        """
        if players is None:
            players = self.adapter.fetch_player_data(season)

        sl = steamer_lookup or {}

        batters = [p for p in players if p.stat_type == "batting"]
        pitchers = [p for p in players if p.stat_type == "pitching"]

        batter_rankings  = self._ros_pool(batters,  self.hitting_cats,  is_pitcher=False, sl=sl)
        pitcher_rankings = self._ros_pool(pitchers, self.pitching_cats, is_pitcher=True,  sl=sl)

        all_rankings = batter_rankings + pitcher_rankings
        all_rankings.sort(key=lambda r: r.score, reverse=True)
        for i, r in enumerate(all_rankings):
            r.overall_rank = i + 1

        _assign_position_ranks(all_rankings)
        return all_rankings

    def _ros_pool(
        self,
        players: list[NormalizedPlayerData],
        categories: list[str],
        is_pitcher: bool,
        sl: dict[int, NormalizedPlayerData],
    ) -> list[PlayerRanking]:
        """Score a batter or pitcher pool using the three-component formula."""
        if not players:
            return []

        n = len(players)

        # 1. Compute each component across the full pool
        statcast_scores = compute_statcast_composites(players, "pitching" if is_pitcher else "batting")
        steamer_scores, steamer_contributions = _score_steamer_component(players, categories, sl, is_pitcher)
        accum_scores    = _score_accumulated(players, categories, sl, is_pitcher)

        # 2. Per-player dynamic weight blend + role category adjustment
        raw_scores: list[float] = []
        per_player_statcast: list[float] = []
        per_player_steamer:  list[float] = []
        per_player_accum:    list[float] = []

        for i, p in enumerate(players):
            cnt = p.counting_stats or {}
            pa_or_ip = float(cnt.get("IP" if is_pitcher else "PA") or 0)

            statcast_w, accum_w, _late_decay_w, steamer_w = _compute_component_weights(
                pa_or_ip, is_pitcher
            )

            sc = statcast_scores[i] if i < len(statcast_scores) else 0.0
            st = steamer_scores[i]  if i < len(steamer_scores)  else 0.0
            ac = accum_scores[i]    if i < len(accum_scores)    else 0.0

            per_player_statcast.append(round(sc, 4))
            per_player_steamer.append(round(st, 4))
            per_player_accum.append(round(ac, 4))

            raw_scores.append(sc * statcast_w + st * steamer_w + ac * accum_w)

        # 3. Anchor at replacement level
        replacement_rank = PITCHER_REPLACEMENT_RANK if is_pitcher else BATTER_REPLACEMENT_RANK
        anchored = apply_replacement_level(raw_scores, replacement_rank)

        # 4. Compute percentile data for blurb prompts
        stat_type = "pitching" if is_pitcher else "batting"
        bucket_map = _PITCHER_METRIC_BUCKET if is_pitcher else _BATTER_METRIC_BUCKET
        composites = PITCHER_COMPOSITES if is_pitcher else BATTER_COMPOSITES
        all_metrics = [
            (m, bucket_map.get(m, "advanced_stats"), lower)
            for group in composites.values()
            for m, lower in group
        ]
        percentile_data_list = compute_percentile_data(players, all_metrics)

        # 5. Build PlayerRanking objects
        rankings: list[PlayerRanking] = []
        for i, player in enumerate(players):
            base_score   = anchored[i]
            scarcity_mult = _get_scarcity_multiplier(player.positions or [])
            outperformer  = _compute_outperformer_flag(player)

            # Apply role-specific category weights to the Steamer component contributions.
            # We approximate this on the final score via the scarcity path for now;
            # full per-category role weighting is applied inside _score_steamer_component
            # through the category z-score loop (SP_CATEGORY_WEIGHTS / RP_CATEGORY_WEIGHTS).

            rankings.append(PlayerRanking(
                player_id=player.player_id,
                name=player.name,
                team=player.team,
                positions=player.positions or [],
                stat_type=stat_type,
                score=round(base_score * scarcity_mult, 4),
                raw_score=round(base_score, 4),
                category_contributions=steamer_contributions[i] if i < len(steamer_contributions) else {},
                statcast_score=per_player_statcast[i],
                steamer_score=per_player_steamer[i],
                accum_score=per_player_accum[i],
                outperformer_flag=outperformer,
                percentile_data=percentile_data_list[i] if percentile_data_list else {},
                injury_status=getattr(player, "injury_status", "active"),
                risk_flag=getattr(player, "risk_flag", None),
                risk_note=getattr(player, "risk_note", None),
            ))

        rankings.sort(key=lambda r: r.score, reverse=True)
        return rankings

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
        schedule_overrides: Optional[dict[int, "HorizonConfig"]] = None,
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
        _so = schedule_overrides or {}
        projected: list[dict[str, float]] = []
        for p in players:
            steamer = sl.get(p.player_id)
            player_config = _so.get(p.player_id, config)
            if is_sp is None and not detect_pitcher_role:
                projected.append(project_hitter_stats(p, player_config, steamer_data=steamer))
            elif detect_pitcher_role:
                # Combined pitcher pool: each player uses their own role's IP volume.
                # For dual SP/RP players, use Steamer's projected IP to determine the
                # actual role. A player Steamer projects for < 100 IP is a swingman or
                # reliever, not a true starter — give them the RP ip budget to avoid
                # over-ranking players with SP eligibility but RP workloads.
                if "SP" in p.positions and "RP" in p.positions and steamer is not None:
                    steamer_season_ip = float(
                        steamer.counting_stats.get("IP") or 0
                    )
                    player_is_sp = steamer_season_ip >= 100.0
                else:
                    player_is_sp = "SP" in p.positions
                projected.append(project_pitcher_stats(p, player_config, is_sp=player_is_sp, steamer_data=steamer))
            else:
                projected.append(project_pitcher_stats(p, player_config, is_sp=is_sp, steamer_data=steamer))

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


def _compute_component_weights(
    pa_or_ip: float,
    is_pitcher: bool,
) -> tuple[float, float, float, float]:
    """Return (statcast_w, accum_w, late_decay_w, steamer_w) for a given PA or IP total.

    All four weights sum to exactly 1.0.  Steamer weight is derived so
    callers only need to tune the three named constants.
    """
    if is_pitcher:
        stat_full    = STATCAST_IP_FULL
        accum_full   = ACCUM_IP_FULL
        decay_start  = LATE_DECAY_IP_START
        decay_range  = LATE_DECAY_IP_FULL - LATE_DECAY_IP_START
    else:
        stat_full    = STATCAST_PA_FULL
        accum_full   = ACCUM_PA_FULL
        decay_start  = LATE_DECAY_PA_START
        decay_range  = LATE_DECAY_PA_FULL - LATE_DECAY_PA_START

    statcast_w   = STATCAST_WEIGHT_MAX * min(pa_or_ip / stat_full, 1.0)
    accum_w      = ACCUM_WEIGHT_MAX    * min(pa_or_ip / accum_full, 1.0)
    late_decay_w = LATE_DECAY_MAX      * min(max(pa_or_ip - decay_start, 0.0) / decay_range, 1.0)
    steamer_w    = max(0.0, 1.0 - statcast_w - accum_w - late_decay_w)
    return statcast_w, accum_w, late_decay_w, steamer_w


def _compute_outperformer_flag(
    player: NormalizedPlayerData,
) -> Optional[int]:
    """Return 1/2/3/None outperformer tier for a player.

    Tier 3 — small sample outperformer (< OUTPERFORMER_MIN_PA but hot start):
      actual AVG meaningfully above xBA, or actual ERA meaningfully below xERA.
    Tier 2 — single-season outperformer (≥ OUTPERFORMER_MIN_PA):
      actual stats significantly exceed xStats.
    Tier 1 (sustained multi-season): deferred — requires multi-year history.
      Currently returns Tier 2 for all qualifying players until history is tracked.
    """
    import math
    cnt  = player.counting_stats or {}
    rate = player.rate_stats or {}
    adv  = player.advanced_stats or {}

    pa  = float(cnt.get("PA")  or 0)
    ip  = float(cnt.get("IP")  or 0)
    is_pitcher = player.stat_type == "pitching"

    def _safe(d: dict, k: str) -> Optional[float]:
        v = d.get(k)
        if v is None:
            return None
        try:
            f = float(v)
            return None if math.isnan(f) else f
        except (TypeError, ValueError):
            return None

    if is_pitcher:
        actual_era = _safe(rate, "ERA")
        x_era      = _safe(adv, "xERA")
        siera      = _safe(adv, "SIERA")

        gap_era_xera  = (actual_era - x_era)  if actual_era is not None and x_era  is not None else None
        gap_era_siera = (actual_era - siera)   if actual_era is not None and siera  is not None else None

        meaningful = (
            (gap_era_xera  is not None and gap_era_xera  < _OUTPERFORMER_THRESHOLDS["ERA_vs_xERA"]) or
            (gap_era_siera is not None and gap_era_siera < _OUTPERFORMER_THRESHOLDS["ERA_vs_SIERA"])
        )
        if not meaningful:
            return None
        if ip < _OUTPERFORMER_MIN_IP:
            return 3
        return 2   # Tier 1 (sustained) deferred until multi-year history available
    else:
        actual_avg = _safe(rate, "AVG")
        x_ba       = _safe(adv, "xBA")

        if actual_avg is None or x_ba is None:
            return None
        gap = actual_avg - x_ba
        if gap < _OUTPERFORMER_THRESHOLDS["AVG_vs_xBA"]:
            return None
        if pa < _OUTPERFORMER_MIN_PA:
            return 3
        return 2


def _apply_role_category_weights(
    contributions: dict[str, float],
    positions: list[str],
) -> dict[str, float]:
    """Scale per-category z-scores by SP/RP role multipliers.

    Returns a new dict with weighted contributions.
    """
    if "SP" in positions:
        weights = SP_CATEGORY_WEIGHTS
    elif "RP" in positions:
        weights = RP_CATEGORY_WEIGHTS
    else:
        return contributions
    return {cat: z * weights.get(cat, 1.0) for cat, z in contributions.items()}


def _score_accumulated(
    players: list[NormalizedPlayerData],
    categories: list[str],
    steamer_lookup: dict[int, NormalizedPlayerData],
    is_pitcher: bool,
) -> list[float]:
    """Z-score actual YTD stats (annualized to full-season pace).

    Annualization prevents players who've simply played more games from
    dominating on counting stats — a batter on pace for 40 HR in 250 PA
    ranks the same as one who's already hit 40 HR in 600 PA.
    """
    if not players or not categories:
        return [0.0] * len(players)

    # Build annualized stat dicts
    annualized: list[dict[str, float]] = []
    for p in players:
        cnt  = p.counting_stats or {}
        rate = p.rate_stats or {}
        adv  = p.advanced_stats or {}

        if is_pitcher:
            actual_ip = max(float(cnt.get("IP") or 0), 0.1)
            # Use Steamer projected IP as season target, else use SP/RP default
            steamer = steamer_lookup.get(p.player_id)
            is_sp = "SP" in (p.positions or [])
            if steamer:
                target_ip = float(steamer.counting_stats.get("IP") or (170 if is_sp else 62))
            else:
                target_ip = 170.0 if is_sp else 62.0
            scale = min(target_ip / actual_ip, 4.0)   # cap at 4× to prevent extreme pace

            a: dict[str, float] = {}
            a["ERA"]  = float(rate.get("ERA")  or 4.50)
            a["WHIP"] = float(rate.get("WHIP") or 1.30)
            a["K/9"]  = float(rate.get("K/9")  or rate.get("K9") or 8.0)
            a["BB/9"] = float(rate.get("BB/9") or rate.get("BB9") or 3.0)
            a["IP"]   = actual_ip * scale
            for cat in ("W", "SV", "HLD", "K", "SO", "QS"):
                v = cnt.get(cat) or cnt.get(cat.lower())
                a[cat] = float(v or 0) * scale
        else:
            actual_pa = max(float(cnt.get("PA") or 0), 1.0)
            steamer = steamer_lookup.get(p.player_id)
            if steamer:
                target_pa = float(steamer.counting_stats.get("PA") or 550)
            else:
                target_pa = 550.0
            scale = min(target_pa / actual_pa, 4.0)

            a = {}
            a["AVG"] = float(rate.get("AVG") or 0.250)
            a["OBP"] = float(rate.get("OBP") or 0.315)
            a["SLG"] = float(rate.get("SLG") or 0.400)
            a["OPS"] = a["OBP"] + a["SLG"]
            for cat in ("R", "HR", "RBI", "SB", "H", "BB"):
                v = cnt.get(cat) or cnt.get(cat.lower())
                a[cat] = float(v or 0) * scale

        annualized.append(a)

    # Z-score each category
    from collections import defaultdict
    cat_values: dict[str, list[Optional[float]]] = defaultdict(list)
    for cat in categories:
        mapping = CATEGORY_STAT_MAP.get(cat)
        if mapping is None:
            continue
        _bucket, stat_name = mapping
        for a in annualized:
            v = a.get(stat_name) or a.get(cat)
            cat_values[cat].append(v)

    composite = np.zeros(len(players))
    for cat, vals in cat_values.items():
        clean = [v for v in vals if v is not None]
        if len(clean) < 2:
            continue
        mean = float(np.mean(clean))
        std  = float(np.std(clean))
        if std == 0:
            continue
        zs = np.array([
            float((v - mean) / std) if v is not None else 0.0
            for v in vals
        ])
        if cat in LOWER_IS_BETTER:
            zs = -zs
        zs = np.clip(zs, -Z_SCORE_CAP, Z_SCORE_CAP)

        # Apply role weights for pitchers
        role_weight = 1.0
        if is_pitcher:
            # Use average weight across the pool — individual weighting happens later
            role_weight = 1.0  # applied per-player in blend step
        composite += zs

    return composite.tolist()


def _bayesian_blend_steamer(
    steamer: NormalizedPlayerData,
    player: NormalizedPlayerData,
    is_pitcher: bool,
) -> NormalizedPlayerData:
    """Return a Steamer projection blended toward actual in-season performance.

    Treats the Steamer projection as a Bayesian prior with strength equal to
    _STEAMER_PRIOR_PA (batters) or _STEAMER_PRIOR_IP (pitchers).  The player's
    actual stats update that prior as evidence accumulates:

        posterior_rate = (PA × actual_rate + PRIOR_PA × steamer_rate)
                         / (PA + PRIOR_PA)

    Examples:
        Walker at 57 PA  → actual_w = 57/357 = 16%
                           posterior OPS ≈ 0.720 (vs raw Steamer 0.649)
        Soto   at 300 PA → actual_w = 300/600 = 50%
        Full   at 550 PA → actual_w = 550/850 = 65%  (actual dominates)

    Only *quality* signals are blended (rate stats, per-PA counting rates).
    The projected PA/IP *volume* is intentionally unchanged — playing-time
    discounts are handled separately by _availability_multiplier.
    """
    from dataclasses import replace as _dc_replace

    if is_pitcher:
        actual_ip = float((player.counting_stats or {}).get("IP") or 0)
        if actual_ip < 1.0:
            return steamer  # no evidence yet; use pure Steamer
        actual_w = actual_ip / (actual_ip + _STEAMER_PRIOR_IP)
        steamer_w = 1.0 - actual_w

        new_rate = dict(steamer.rate_stats or {})
        rate_p = player.rate_stats or {}
        for key in ("ERA", "WHIP", "K/9"):
            sv = new_rate.get(key)
            av = rate_p.get(key)
            if sv is not None and av is not None:
                new_rate[key] = steamer_w * sv + actual_w * av

        return _dc_replace(steamer, rate_stats=new_rate)

    else:
        actual_pa = float((player.counting_stats or {}).get("PA") or 0)
        if actual_pa < 1.0:
            return steamer  # no evidence yet; use pure Steamer
        actual_w = actual_pa / (actual_pa + _STEAMER_PRIOR_PA)
        steamer_w = 1.0 - actual_w

        new_rate = dict(steamer.rate_stats or {})
        rate_p = player.rate_stats or {}
        for key in ("AVG", "OBP", "SLG"):
            sv = new_rate.get(key)
            av = rate_p.get(key)
            if sv is not None and av is not None:
                new_rate[key] = steamer_w * sv + actual_w * av

        # Blend per-PA counting rates so HR/PA, SB/PA, BB/PA also reflect actual
        new_cnt = dict(steamer.counting_stats or {})
        steamer_pa = float(new_cnt.get("PA") or 1.0)
        cnt_p = player.counting_stats or {}
        for key in ("HR", "SB", "BB"):
            sv = new_cnt.get(key)
            if sv is not None and steamer_pa > 0:
                steamer_rate = sv / steamer_pa
                actual_rate = float(cnt_p.get(key) or 0) / actual_pa
                blended_rate = steamer_w * steamer_rate + actual_w * actual_rate
                new_cnt[key] = blended_rate * steamer_pa

        return _dc_replace(steamer, rate_stats=new_rate, counting_stats=new_cnt)


def _score_steamer_component(
    players: list[NormalizedPlayerData],
    categories: list[str],
    steamer_lookup: dict[int, NormalizedPlayerData],
    is_pitcher: bool,
) -> tuple[list[float], list[dict[str, float]]]:
    """Z-score Steamer full-season projections with Bayesian early-season blend.

    Uses project_hitter_stats / project_pitcher_stats with actual_weight=0
    (no within-projection blending), but applies _bayesian_blend_steamer first
    to adjust the Steamer *input data* toward actual in-season performance based
    on sample size.  This prevents early-season breakouts (Walker: 1.092 OPS
    at 57 PA vs Steamer .649) from being dragged below replacement level while
    maintaining Steamer dominance for players with very few PA.

    Returns (composite_scores, per_player_category_contributions).
    category_contributions is keyed by fantasy category name and used for
    display and context boosting in the blurb prompts.
    """
    from fantasai.engine.projection import (
        HORIZON_CONFIGS, ProjectionHorizon,
        project_hitter_stats, project_pitcher_stats,
    )
    from dataclasses import replace as _dc_replace

    # Use SEASON config but zero out actual_weight to get pure Steamer
    season_config = HORIZON_CONFIGS[ProjectionHorizon.SEASON]
    steamer_config = _dc_replace(season_config, actual_weight=0.0, talent_weight=1.0)

    projected: list[dict[str, float]] = []
    for p in players:
        steamer = steamer_lookup.get(p.player_id)
        # Bayesian update: blend Steamer projection toward actual in-season stats
        # proportional to sample size.  At 57 PA the prior still dominates (84%),
        # but the nudge is enough to surface breakouts like Walker above replacement.
        if steamer is not None:
            steamer = _bayesian_blend_steamer(steamer, p, is_pitcher)
        if is_pitcher:
            player_is_sp = "SP" in (p.positions or [])
            projected.append(project_pitcher_stats(p, steamer_config, is_sp=player_is_sp, steamer_data=steamer))
        else:
            projected.append(project_hitter_stats(p, steamer_config, steamer_data=steamer))

    # Z-score per category, tracking per-player contributions
    n = len(players)
    composite = np.zeros(n)
    cat_zscores: dict[str, list[float]] = {}

    for cat in categories:
        mapping = CATEGORY_STAT_MAP.get(cat)
        if mapping is None:
            continue
        _bucket, stat_name = mapping
        vals = [proj.get(stat_name) or proj.get("K" if stat_name == "SO" else "") for proj in projected]
        clean = [v for v in vals if v is not None]
        if len(clean) < 2:
            continue
        mean = float(np.mean(clean))
        std  = float(np.std(clean))
        if std == 0:
            continue
        zs = np.array([
            float((v - mean) / std) if v is not None else 0.0
            for v in vals
        ])
        if cat in LOWER_IS_BETTER:
            zs = -zs
        zs = np.clip(zs, -Z_SCORE_CAP, Z_SCORE_CAP)
        composite += zs
        cat_zscores[cat] = zs.tolist()

    # Build per-player category contributions dicts
    contributions: list[dict[str, float]] = [
        {cat: round(cat_zscores[cat][i], 3) for cat in cat_zscores}
        for i in range(n)
    ]

    return composite.tolist(), contributions


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
