"""Category-based stat projection for predictive rankings.

Converts advanced/process metrics into projected fantasy category stats for a
given horizon.  The projected stats are then scored using the same z-score
machinery as the lookback model, ensuring that:

  - Playing time volume is incorporated (SPs > RPs in counting cats)
  - Category contributions are directly comparable to lookback rankings
  - Horizon length controls both PA/IP volume and the talent-vs-recency blend

Blend logic:
  Short horizon (WEEK)  → pure projections (100% talent) — never use YTD stats
  Medium horizon (MONTH) → mostly projections (80%), small breakout signal (20%)
  Long horizon (SEASON) → 50/50 blend for Rest of Season

Schedule-aware adjustments (opponent quality for short-term projections) are a
future enhancement; the HorizonConfig dataclass has a slot reserved for that.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Optional

from fantasai.adapters.base import NormalizedPlayerData


# ---------------------------------------------------------------------------
# Steamer helpers
# ---------------------------------------------------------------------------

def _steamer_rate(steamer: Optional[NormalizedPlayerData], key: str) -> Optional[float]:
    """Return a Steamer rate stat (e.g. AVG, ERA, K/9) if available."""
    if steamer is None:
        return None
    return steamer.rate_stats.get(key)


def _steamer_count_per(
    steamer: Optional[NormalizedPlayerData],
    count_key: str,
    denom_key: str,
    denom_default: float = 550.0,
) -> Optional[float]:
    """Return Steamer count_key / denom_key as a per-unit rate.

    E.g. steamer HR / steamer PA  →  projected HR rate per PA.
    Returns None if Steamer data is absent or denom is zero.
    """
    if steamer is None:
        return None
    denom = steamer.counting_stats.get(denom_key, 0.0) or denom_default
    val = steamer.counting_stats.get(count_key)
    if val is None:
        return None
    return val / max(denom, 1.0)


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
        talent_weight=1.00,
        actual_weight=0.00,
    ),
    ProjectionHorizon.MONTH: HorizonConfig(
        label="This Month",
        hitter_pa=100,
        sp_ip=28.0,
        rp_ip=13.0,
        talent_weight=0.80,
        actual_weight=0.20,
    ),
    ProjectionHorizon.SEASON: HorizonConfig(
        label="Rest of Season",
        hitter_pa=540,
        sp_ip=170.0,
        rp_ip=62.0,
        talent_weight=0.50,
        actual_weight=0.50,
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
# Injury / availability discount
# ---------------------------------------------------------------------------

# Risk-flag multipliers applied to projected PA / IP.
# These represent realistic full-season expectations vs. Steamer's optimistic
# assumption of full health.  Calibrated so that the rank drop for a flagged
# player lands roughly where a well-informed analyst would expect:
#
#   "fragile"        → chronic injury history; typically misses 15-25% of the
#                      season across a career (Glasnow: ~134 IP in 2024,
#                      ~100 IP in 2023).  0.85× on a 160 IP Steamer line
#                      gives ~136 IP — a meaningful haircut without punishing
#                      a still-elite upside pitcher too harshly.
#
#   "recent_surgery" → recovering from a major procedure but structurally
#                      intact; expect a cautious ramp-up.  0.90× haircut
#                      (Wheeler: ~153 of 170 projected IP) acknowledges the
#                      risk without overcorrecting for a likely-healthy pitcher.
_RISK_FLAG_MULTIPLIER: dict[str, float] = {
    "fragile": 0.85,
    "recent_surgery": 0.90,
}

# 2026 season window used for availability calculation.
# April 1 → October 1 = 183 days.  Adjust here if the projection year changes.
_SEASON_START = date(2026, 4, 1)
_SEASON_END   = date(2026, 10, 1)
_SEASON_DAYS  = (_SEASON_END - _SEASON_START).days  # 183


def _availability_multiplier(player: NormalizedPlayerData, config: HorizonConfig) -> float:
    """Return a 0.0–1.0 playing-time multiplier reflecting injury/risk discounts.

    Two independent factors are multiplied together:

    1. **Risk-flag discount** (chronic or structural risk):
       Applied even when the player is not currently on the IL.  Reduces the
       effective PA/IP cap by _RISK_FLAG_MULTIPLIER[risk_flag].

    2. **Current IL availability** (on the IL right now):
       For SEASON horizon: computes the fraction of the season a player can
       contribute based on their expected return date vs. the season window.
       For WEEK/MONTH: zeros out any player who won't be back within that window.
       When return_date is unknown, falls back to a conservative estimate.
    """
    risk_mult = _RISK_FLAG_MULTIPLIER.get(player.risk_flag or "", 1.0)

    status = player.injury_status
    if status == "active":
        il_mult = 1.0
    elif status == "out_for_season":
        il_mult = 0.0
    else:
        return_date = player.injury_return_date
        if return_date is None:
            # Conservative estimate when no return date is known
            il_mult = 0.05 if status == "il_60" else 0.25
        else:
            today = date.today()
            if config == HORIZON_CONFIGS[ProjectionHorizon.WEEK]:
                horizon_end = today + timedelta(days=7)
                if return_date >= horizon_end:
                    il_mult = 0.0
                else:
                    available = max(0, (horizon_end - max(return_date, today)).days)
                    il_mult = available / 7.0
            elif config == HORIZON_CONFIGS[ProjectionHorizon.MONTH]:
                horizon_end = today + timedelta(days=30)
                if return_date >= horizon_end:
                    il_mult = 0.0
                else:
                    available = max(0, (horizon_end - max(return_date, today)).days)
                    il_mult = available / 30.0
            else:  # SEASON
                if return_date >= _SEASON_END:
                    il_mult = 0.0
                else:
                    effective_start = max(return_date, _SEASON_START)
                    available_days = max(0, (_SEASON_END - effective_start).days)
                    il_mult = available_days / _SEASON_DAYS

    return risk_mult * il_mult


# ---------------------------------------------------------------------------
# Hitter projection
# ---------------------------------------------------------------------------

def project_hitter_stats(
    player: NormalizedPlayerData,
    config: HorizonConfig,
    steamer_data: Optional[NormalizedPlayerData] = None,
) -> dict[str, float]:
    """Project hitter fantasy category stats for a given horizon.

    When *steamer_data* is supplied, Steamer's projected values replace the
    homegrown xStats-derived talent signal.  This gives better pre-season
    projections (Steamer accounts for age curves, role changes, health) while
    preserving the blend-with-actuals logic for mid-season use.

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

    # Effective PA for counting stat scaling.
    # The consensus projection already represents a full-season estimate at
    # whatever playing time the projection system expects (e.g. 120 PA for a
    # prospect, 634 PA for Bobby Witt Jr.).  If we naively divide by consensus
    # PA and multiply by 540 we get absurd numbers — a player projected for 46
    # SBs in 120 PA would "project" to 207 SBs, massively inflating the pool
    # mean and making real base-stealers look below average.
    # Fix: cap the projection PA at the consensus projected PA so we never
    # extrapolate beyond the full-season estimate.
    if steamer_data is not None:
        _consensus_pa = steamer_data.counting_stats.get("PA") or pa
    else:
        _consensus_pa = pa
    effective_pa = min(pa, float(_consensus_pa))

    # Apply injury / availability discount to effective playing time.
    # This handles both current IL (Hunter Greene) and chronic fragility
    # risk flags (Glasnow 0.70×, Wheeler 0.80×).  Rate stats (AVG, OBP,
    # SLG, OPS) are unaffected — we're only discounting volume, not skill.
    effective_pa *= _availability_multiplier(player, config)

    # ── Rate stats ──────────────────────────────────────────────────────────

    # AVG: Steamer projected AVG > xBA (already regressed + age-adjusted)
    talent_avg: Optional[float] = (
        _steamer_rate(steamer_data, "AVG")
        or (_safe(adv, "xBA") or None)
    )
    proj_avg = _blend(talent_avg, _safe(rate, "AVG") or None, tw, aw, default=0.250)
    proj_avg = max(0.100, min(0.400, proj_avg))

    # OBP: Steamer projected OBP, else derive from xwOBA
    talent_obp: Optional[float] = _steamer_rate(steamer_data, "OBP")
    if talent_obp is None:
        xwoba = adv.get("xwOBA")
        talent_obp = ((float(xwoba) - 0.04) / 1.21) if xwoba is not None else None
    proj_obp = _blend(talent_obp, _safe(rate, "OBP") or None, tw, aw, default=0.315)
    proj_obp = max(proj_avg, min(0.500, proj_obp))

    # SLG: Steamer projected SLG > xSLG
    talent_slg: Optional[float] = (
        _steamer_rate(steamer_data, "SLG")
        or adv.get("xSLG")
    )
    proj_slg = _blend(talent_slg, _safe(rate, "SLG") or None, tw, aw, default=0.400)
    proj_slg = max(proj_avg, min(0.900, proj_slg))

    proj_ops = proj_obp + proj_slg

    # ── Per-PA rates for counting stats ─────────────────────────────────────

    # HR rate: Steamer HR/PA > Barrel%-derived estimate.
    # Explicit None check prevents Steamer's legitimate 0-HR projection from
    # being silently replaced by the Barrel% fallback (0.0 is falsy in Python).
    _steamer_hr = _steamer_count_per(steamer_data, "HR", "PA")
    talent_hr_rate: float = (
        _steamer_hr if _steamer_hr is not None
        else (_safe(adv, "Barrel%") / 100.0 * 0.35)
    )
    actual_hr_rate = _safe(cnt, "HR") / season_pa
    proj_hr_rate = max(0.0, _blend(talent_hr_rate, actual_hr_rate, tw, aw, default=0.033))

    # SB rate: Steamer SB/PA > Spd-score estimate.
    # Two-level fallback:
    #   1. If Steamer data exists but has no SB key → FanGraphs omits SB for
    #      non-stealers instead of returning 0; treat as 0 (not a base-stealer).
    #      Using the Spd fallback here inflates the pool mean with ~6.5 phantom
    #      SBs per player and makes real base-stealers look below average.
    #   2. If there is NO Steamer data at all (pure prospect / fringe player
    #      not covered by any projection system) → use Spd score as the only
    #      available speed signal.
    _steamer_sb = _steamer_count_per(steamer_data, "SB", "PA")
    if _steamer_sb is not None:
        talent_sb_rate: float = _steamer_sb
    elif steamer_data is not None:
        # Has a consensus projection row but SB wasn't projected → not a stealer
        talent_sb_rate = 0.0
    else:
        # No projection data at all → use Spd as speed proxy
        talent_sb_rate = max(0.0, (_safe(adv, "Spd", default=4.5) - 3.5) * 0.012)
    actual_sb_rate = _safe(cnt, "SB") / season_pa
    proj_sb_rate = max(0.0, _blend(talent_sb_rate, actual_sb_rate, tw, aw, default=0.010))

    # BB rate: Steamer BB/PA > actual BB%
    talent_bb_pct: float = (
        _steamer_count_per(steamer_data, "BB", "PA")
        or _safe(rate, "BB%", default=0.08)
    )
    actual_bb_pct = _safe(cnt, "BB") / season_pa
    proj_bb_pct = max(0.03, min(0.25, _blend(talent_bb_pct, actual_bb_pct, tw, aw, default=0.08)))

    # ── Scale counting stats to horizon PA ──────────────────────────────────

    # estimated ABs ≈ PA minus BB, HBP, SF (roughly BB% + ~1% for HBP/SF)
    est_ab = effective_pa * max(0.5, 1.0 - proj_bb_pct - 0.01)

    proj_h   = proj_avg * est_ab
    proj_hr  = proj_hr_rate * effective_pa
    proj_sb  = proj_sb_rate * effective_pa
    proj_bb  = proj_bb_pct * effective_pa

    # R estimate: linear approximation from OBP and SLG
    # Based on run-value research: R/PA ≈ 0.42*OBP + 0.09*SLG
    proj_r   = (0.42 * proj_obp + 0.09 * proj_slg) * effective_pa

    # RBI estimate: power-driven; ISO = SLG − AVG proxies extra-base hit rate
    iso      = max(0.0, proj_slg - proj_avg)
    proj_rbi = (0.07 + 0.46 * iso) * effective_pa

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
    steamer_data: Optional[NormalizedPlayerData] = None,
) -> dict[str, float]:
    """Project pitcher fantasy category stats for a given horizon.

    When *steamer_data* is supplied, Steamer's projected ERA/K9/WHIP replace
    the homegrown xERA/SwStr% talent derivations.

    IP is set directly from the horizon config (sp_ip or rp_ip) — this is the
    key mechanism that naturally bounds reliever upside relative to starters.
    All per-inning rates are then scaled to that IP total.
    """
    adv = player.advanced_stats
    rate = player.rate_stats
    cnt = player.counting_stats
    tw, aw = config.talent_weight, config.actual_weight

    # Apply injury/availability discount to IP volume before any rate scaling.
    # Risk flags (Glasnow "fragile" → 0.70×) and current IL (return_date →
    # fraction of season available) both reduce the IP ceiling here, keeping
    # rate stats (ERA, WHIP, K/9) unaffected while docking counting stats.
    ip = (config.sp_ip if is_sp else config.rp_ip) * _availability_multiplier(player, config)

    season_ip = max(_safe(cnt, "IP", 0.1), 0.1)

    # ── ERA ───────────────────────────────────────────────────────────────────
    # Steamer ERA > ensemble of xERA/SIERA/xFIP
    talent_era: Optional[float] = _steamer_rate(steamer_data, "ERA")
    if talent_era is None:
        era_estimates = [
            float(adv[k]) for k in ("xERA", "SIERA", "xFIP")
            if adv.get(k) is not None
        ]
        talent_era = (sum(era_estimates) / len(era_estimates)) if era_estimates else None
    proj_era = _blend(talent_era, _safe(rate, "ERA") or None, tw, aw, default=4.00)
    proj_era = max(0.50, min(9.0, proj_era))

    # ── K/9 ───────────────────────────────────────────────────────────────────
    # Steamer K/9 > SwStr%-derived estimate
    talent_k9: Optional[float] = _steamer_rate(steamer_data, "K/9")
    if talent_k9 is None:
        swstr = _safe(adv, "SwStr%", default=0.10)
        talent_k_pct = min(0.45, 2.3 * swstr + 0.04)
        talent_k9 = talent_k_pct * 27.0
    proj_k9 = _blend(talent_k9, _safe(rate, "K/9") or None, tw, aw, default=8.0)
    proj_k9 = max(3.0, min(18.0, proj_k9))

    # ── BB/9 ──────────────────────────────────────────────────────────────────
    talent_bb9: Optional[float] = _steamer_rate(steamer_data, "BB/9")
    proj_bb9 = max(0.5, min(8.0, talent_bb9 or _safe(rate, "BB/9", default=3.0)))

    # ── WHIP ──────────────────────────────────────────────────────────────────
    talent_whip: Optional[float] = _steamer_rate(steamer_data, "WHIP")
    if talent_whip is None:
        talent_whip = 0.22 * proj_era + 0.55
    proj_whip = _blend(talent_whip, _safe(rate, "WHIP") or None, tw, aw, default=1.28)
    proj_whip = max(0.60, min(3.0, proj_whip))

    # ── Counting stats scaled to horizon IP ──────────────────────────────────

    proj_k = proj_k9 / 9.0 * ip

    # W/SV/HLD: Steamer already accounts for role, team context, and opener risk.
    # Use Steamer per-IP rates when available; fall back to scaling YTD actuals.
    # SPs never accumulate saves or holds.
    if steamer_data:
        steamer_ip = max(_safe(steamer_data.counting_stats, "IP", 0.1), 0.1)
        proj_w   = (_safe(steamer_data.counting_stats, "W",   0.0) / steamer_ip) * ip
        proj_sv  = 0.0 if is_sp else (_safe(steamer_data.counting_stats, "SV",  0.0) / steamer_ip) * ip
        proj_hld = 0.0 if is_sp else (_safe(steamer_data.counting_stats, "HLD", 0.0) / steamer_ip) * ip
    else:
        proj_w   = (_safe(cnt, "W")   / season_ip) * ip
        proj_sv  = 0.0 if is_sp else (_safe(cnt, "SV")  / season_ip) * ip
        proj_hld = 0.0 if is_sp else (_safe(cnt, "HLD") / season_ip) * ip

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
