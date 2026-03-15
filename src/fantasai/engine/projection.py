"""Category-based stat projection for predictive rankings.

Converts advanced/process metrics into projected fantasy category stats for a
given horizon.  The projected stats are then scored using the same z-score
machinery as the lookback model, ensuring that:

  - Playing time volume is incorporated (SPs > RPs in counting cats)
  - Category contributions are directly comparable to lookback rankings
  - Horizon length controls both PA/IP volume and the talent-vs-recency blend

Blend logic:
  Short horizon (WEEK)  → lean on recent actual performance (65%) with talent (35%)
  Medium horizon (MONTH) → balanced lean toward talent (65%) over actuals (35%)
  Long horizon (SEASON) → mostly talent signal (85%), small recency anchor (15%)

Schedule-aware adjustments (opponent quality for short-term projections) are a
future enhancement; the HorizonConfig dataclass has a slot reserved for that.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from fantasai.adapters.base import NormalizedPlayerData


class ProjectionHorizon(str, Enum):
    WEEK = "week"
    MONTH = "month"
    SEASON = "season"


@dataclass
class HorizonConfig:
    label: str
    hitter_pa: int        # projected plate appearances in window
    sp_ip: float          # projected innings for a starting pitcher in window
    rp_ip: float          # projected innings for a reliever in window
    talent_weight: float  # weight applied to advanced-metric talent signal (0–1)
    actual_weight: float  # weight applied to YTD actual-stat signal (0–1)


HORIZON_CONFIGS: dict[ProjectionHorizon, HorizonConfig] = {
    ProjectionHorizon.WEEK: HorizonConfig(
        label="This Week",
        hitter_pa=26,
        sp_ip=6.0,
        rp_ip=3.5,
        talent_weight=0.35,
        actual_weight=0.65,
    ),
    ProjectionHorizon.MONTH: HorizonConfig(
        label="This Month",
        hitter_pa=100,
        sp_ip=28.0,
        rp_ip=13.0,
        talent_weight=0.65,
        actual_weight=0.35,
    ),
    ProjectionHorizon.SEASON: HorizonConfig(
        label="Full Season",
        hitter_pa=540,
        sp_ip=170.0,
        rp_ip=62.0,
        talent_weight=0.85,
        actual_weight=0.15,
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _blend(
    talent_val: Optional[float],
    actual_val: Optional[float],
    talent_w: float,
    actual_w: float,
    default: float,
) -> float:
    """Weighted blend of talent and actual signals.

    Falls back gracefully: both available → weighted average;
    only one available → use it directly; neither → league-average default.
    """
    if talent_val is not None and actual_val is not None:
        return talent_w * talent_val + actual_w * actual_val
    if talent_val is not None:
        return talent_val
    if actual_val is not None:
        return actual_val
    return default


def _safe(d: dict, key: str, default: float = 0.0) -> float:
    """Return float value from dict, or default if missing/None/NaN."""
    import math
    v = d.get(key)
    if v is None:
        return default
    try:
        f = float(v)
        return default if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Hitter projection
# ---------------------------------------------------------------------------

def project_hitter_stats(
    player: NormalizedPlayerData,
    config: HorizonConfig,
) -> dict[str, float]:
    """Project hitter fantasy category stats for a given horizon.

    Returns a flat dict keyed by fantasy category / stat name, all scaled to
    the horizon's plate-appearance volume.  Rate stats (AVG, OBP, SLG, OPS)
    are projected independently of volume; counting stats scale linearly with PA.
    """
    adv = player.advanced_stats
    rate = player.rate_stats
    cnt = player.counting_stats
    tw, aw = config.talent_weight, config.actual_weight
    pa = config.hitter_pa

    season_pa = max(_safe(cnt, "PA", 1.0), 1.0)

    # ── Rate stats ──────────────────────────────────────────────────────────

    # AVG: xBA is the expected batting average — best talent predictor
    proj_avg = _blend(
        _safe(adv, "xBA") or None,
        _safe(rate, "AVG") or None,
        tw, aw, default=0.250,
    )
    proj_avg = max(0.100, min(0.400, proj_avg))

    # OBP: derive from xwOBA (wOBA ≈ 1.21 * OBP + 0.04, so OBP ≈ (wOBA − 0.04) / 1.21)
    xwoba = adv.get("xwOBA")
    talent_obp: Optional[float] = ((float(xwoba) - 0.04) / 1.21) if xwoba is not None else None
    proj_obp = _blend(
        talent_obp,
        _safe(rate, "OBP") or None,
        tw, aw, default=0.315,
    )
    proj_obp = max(proj_avg, min(0.500, proj_obp))  # OBP must be ≥ AVG

    # SLG: xSLG is the direct talent signal
    proj_slg = _blend(
        adv.get("xSLG"),
        _safe(rate, "SLG") or None,
        tw, aw, default=0.400,
    )
    proj_slg = max(proj_avg, min(0.900, proj_slg))  # SLG must be ≥ AVG

    proj_ops = proj_obp + proj_slg

    # ── Per-PA rates for counting stats ─────────────────────────────────────

    # HR rate: Barrel% * 0.35 converts barrel% to HR/PA (roughly 35% of barrels = HR)
    barrel_pct = _safe(adv, "Barrel%") / 100.0  # stored as 0–100 percent
    talent_hr_rate = barrel_pct * 0.35
    actual_hr_rate = _safe(cnt, "HR") / season_pa
    proj_hr_rate = max(0.0, _blend(talent_hr_rate, actual_hr_rate, tw, aw, default=0.033))

    # SB rate: Spd score (0–10) predicts steals; 3.5 is roughly replacement-level speed
    spd = _safe(adv, "Spd", default=4.5)
    talent_sb_rate = max(0.0, (spd - 3.5) * 0.012)
    actual_sb_rate = _safe(cnt, "SB") / season_pa
    proj_sb_rate = max(0.0, _blend(talent_sb_rate, actual_sb_rate, tw, aw, default=0.010))

    # BB rate: BB% is stored as a decimal (0.10 = 10%), very stable year-to-year
    talent_bb_pct = _safe(rate, "BB%", default=0.08)
    actual_bb_pct = _safe(cnt, "BB") / season_pa
    proj_bb_pct = max(0.03, min(0.25, _blend(talent_bb_pct, actual_bb_pct, tw, aw, default=0.08)))

    # ── Scale counting stats to horizon PA ──────────────────────────────────

    # estimated ABs ≈ PA minus BB, HBP, SF (roughly BB% + ~1% for HBP/SF)
    est_ab = pa * max(0.5, 1.0 - proj_bb_pct - 0.01)

    proj_h   = proj_avg * est_ab
    proj_hr  = proj_hr_rate * pa
    proj_sb  = proj_sb_rate * pa
    proj_bb  = proj_bb_pct * pa

    # R estimate: linear approximation from OBP and SLG
    # Based on run-value research: R/PA ≈ 0.42*OBP + 0.09*SLG
    proj_r   = (0.42 * proj_obp + 0.09 * proj_slg) * pa

    # RBI estimate: power-driven; ISO = SLG − AVG proxies extra-base hit rate
    iso      = max(0.0, proj_slg - proj_avg)
    proj_rbi = (0.07 + 0.46 * iso) * pa

    return {
        "AVG": proj_avg,
        "OBP": proj_obp,
        "SLG": proj_slg,
        "OPS": proj_ops,
        "H":   max(0.0, proj_h),
        "HR":  max(0.0, proj_hr),
        "SB":  max(0.0, proj_sb),
        "BB":  max(0.0, proj_bb),
        "R":   max(0.0, proj_r),
        "RBI": max(0.0, proj_rbi),
    }


# ---------------------------------------------------------------------------
# Pitcher projection
# ---------------------------------------------------------------------------

def project_pitcher_stats(
    player: NormalizedPlayerData,
    config: HorizonConfig,
    is_sp: bool,
) -> dict[str, float]:
    """Project pitcher fantasy category stats for a given horizon.

    IP is set directly from the horizon config (sp_ip or rp_ip) — this is the
    key mechanism that naturally bounds reliever upside relative to starters.
    All per-inning rates are then scaled to that IP total.
    """
    adv = player.advanced_stats
    rate = player.rate_stats
    cnt = player.counting_stats
    tw, aw = config.talent_weight, config.actual_weight
    ip = config.sp_ip if is_sp else config.rp_ip

    season_ip = max(_safe(cnt, "IP", 0.1), 0.1)

    # ── ERA (ensemble of regressed ERA estimators) ───────────────────────────
    era_estimates = [
        float(adv[k]) for k in ("xERA", "SIERA", "xFIP")
        if adv.get(k) is not None
    ]
    talent_era: Optional[float] = (sum(era_estimates) / len(era_estimates)) if era_estimates else None
    proj_era = _blend(
        talent_era,
        _safe(rate, "ERA") or None,
        tw, aw, default=4.00,
    )
    proj_era = max(0.50, min(9.0, proj_era))

    # ── K/9 (SwStr% is the strongest per-pitch strikeout predictor) ──────────
    # Approximation derived from FanGraphs research: K% ≈ 2.3 × SwStr% + 0.04
    # K/9 = K% × 27  (27 outs = 9 innings, ignoring walks for this approximation)
    swstr = _safe(adv, "SwStr%", default=0.10)
    talent_k_pct = min(0.45, 2.3 * swstr + 0.04)
    talent_k9 = talent_k_pct * 27.0
    proj_k9 = _blend(
        talent_k9,
        _safe(rate, "K/9") or None,
        tw, aw, default=8.0,
    )
    proj_k9 = max(3.0, min(18.0, proj_k9))

    # ── BB/9 (command is among the stickiest pitcher skills) ─────────────────
    # Use actual BB/9 directly — it's already a reliable talent proxy
    proj_bb9 = max(0.5, min(8.0, _safe(rate, "BB/9", default=3.0)))

    # ── WHIP (ERA-correlated; blend with actual WHIP) ────────────────────────
    # Rough linear: WHIP ≈ 0.22 × ERA + 0.55
    talent_whip = 0.22 * proj_era + 0.55
    proj_whip = _blend(
        talent_whip,
        _safe(rate, "WHIP") or None,
        tw, aw, default=1.28,
    )
    proj_whip = max(0.60, min(3.0, proj_whip))

    # ── Counting stats scaled to horizon IP ──────────────────────────────────

    proj_k = proj_k9 / 9.0 * ip

    # W/SV/HLD: role- and team-context-dependent; scale from actual rates
    proj_w   = (_safe(cnt, "W")   / season_ip) * ip
    proj_sv  = (_safe(cnt, "SV")  / season_ip) * ip
    proj_hld = (_safe(cnt, "HLD") / season_ip) * ip

    # QS: SP only; tiered by projected ERA
    if is_sp:
        qs_rate = (
            0.55 if proj_era < 3.50 else
            0.45 if proj_era < 4.00 else
            0.30 if proj_era < 5.00 else
            0.15
        )
        # IP / 5.5 ≈ number of starts in window (league-average IP/start ≈ 5.5)
        proj_qs = qs_rate * (ip / 5.5)
    else:
        proj_qs = 0.0

    return {
        "ERA":  proj_era,
        "WHIP": proj_whip,
        "K/9":  proj_k9,
        "BB/9": proj_bb9,
        "K":    max(0.0, proj_k),
        "SO":   max(0.0, proj_k),  # alias — CATEGORY_STAT_MAP uses "SO" for K
        "IP":   ip,
        "W":    max(0.0, proj_w),
        "SV":   max(0.0, proj_sv),
        "HLD":  max(0.0, proj_hld),
        "QS":   max(0.0, proj_qs),
    }
