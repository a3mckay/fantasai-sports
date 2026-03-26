"""Prospect Adjusted Value (PAV) scoring module.

Pure Python — no I/O, no database, no external dependencies beyond stdlib.

Formula:
    PAV = ProspectGrade × ((AgeAdjPerformance + VerticalVelocity + ETAProximity) / 300)
    PAV_final = PAV × PositionalScarcityMultiplier

The bracket term is a readiness multiplier (0.1–1.0).  A prospect who is elite
on all three readiness dimensions scores close to 1.0 and PAV approaches their
raw grade.  A prospect who is years away gets heavily discounted.

All component scores are 0–100.  PAV_final is typically 0–100.

Convert PAV to a proxy MLB overall rank with pav_to_proxy_rank().  Calibrated
so that Konnor Griffin (PAV ≈ 93) lands around rank 40–50, placing him inside
the top-80 keeper pool for a 12-team × 6-keeper league.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Positional scarcity multipliers (hitters)
POSITION_MULTIPLIERS_HIT: dict[str, float] = {
    "SS": 1.10,
    "CF": 1.10,
    "C": 1.10,
    "2B": 1.05,
    "3B": 1.05,
}

# Positional scarcity multipliers (pitchers)
POSITION_MULTIPLIERS_PITCH: dict[str, float] = {
    "SP": 1.05,
    "CL": 1.10,  # closer
}

# Ordered from lowest to highest for level comparison
LEVEL_ORDER: list[str] = [
    "Rookie",
    "Complex",
    "Low-A",
    "High-A",
    "Double-A",
    "Triple-A",
    "MLB",
]

# Approximate average player age by MiLB level (used for age-adjustment)
LEVEL_AVG_AGES: dict[str, float] = {
    "Rookie": 19.5,
    "Complex": 19.5,
    "Low-A": 20.5,
    "High-A": 21.5,
    "Double-A": 23.0,
    "Triple-A": 25.0,
    "MLB": 27.0,
}

# Expected (typical) age at each level for age-relative-to-level scoring.
# These reflect the MLB development pathway: elite prospects reach AA by 22-23,
# but a 19-year-old in AA is historically exceptional (Griffin-tier).
# Slightly tighter than LEVEL_AVG_AGES which includes journeymen; these
# represent the "on-track elite prospect" baseline.
_EXPECTED_AGE_BY_LEVEL: dict[str, float] = {
    "Rookie":   19.0,
    "Complex":  19.0,
    "Low-A":    20.5,
    "High-A":   21.5,
    "Double-A": 22.5,
    "Triple-A": 24.0,
    "MLB":      26.0,
}

# MLB Stats API sportId → canonical level name
SPORT_ID_TO_LEVEL: dict[int, str] = {
    11: "Triple-A",
    12: "Double-A",
    13: "High-A",
    14: "Low-A",
    15: "Low-A",   # Short-Season (legacy)
    16: "Rookie",
    17: "Complex",
    1:  "MLB",
}

# PAV → proxy rank calibration constants.
# max(PAV_PROXY_MIN, round(PAV_PROXY_OFFSET + (100 - pav) * PAV_PROXY_SCALE))
# Griffin PAV ≈ 93 → 25 + (7 × 3.0) = 46  ✓
PAV_PROXY_MIN: int = 10
PAV_PROXY_OFFSET: float = 25.0
PAV_PROXY_SCALE: float = 3.0


# ---------------------------------------------------------------------------
# Prospect grade (0–100)
# ---------------------------------------------------------------------------

def calculate_prospect_grade(
    pipeline_grade: Optional[float] = None,
    ba_grade: Optional[float] = None,
    fg_grade: Optional[float] = None,
    consensus_rank: Optional[int] = None,
) -> float:
    """Convert scouting grades or a list rank to a 0–100 scale and return the
    average of all available inputs.

    Scouting grades come on the 20–80 scale; this converts them:
        80 → 100, 70 → 85, 60 → 70, 55 → 62, 50 → 55, 45 → 47, 40 → 38

    If only a consensus list rank is available the following fallback applies:
        Top 10 → 90–100, 11–25 → 78–89, 26–50 → 65–77,
        51–100 → 50–64, 101–200 → 35–49, 200+ → 20–34
    """
    _20_80_TO_100 = {80: 100, 70: 85, 60: 70, 55: 62, 50: 55, 45: 47, 40: 38}

    def _convert_scouting(grade: float) -> float:
        """Linearly interpolate between the known breakpoints."""
        breakpoints = sorted(_20_80_TO_100.items())  # [(40,38),(45,47),…,(80,100)]
        if grade >= 80:
            return 100.0
        if grade <= 20:
            return max(0.0, (grade - 20) / 20 * 20)
        for i in range(len(breakpoints) - 1):
            lo_g, lo_v = breakpoints[i]
            hi_g, hi_v = breakpoints[i + 1]
            if lo_g <= grade <= hi_g:
                t = (grade - lo_g) / (hi_g - lo_g)
                return lo_v + t * (hi_v - lo_v)
        return 55.0  # fallback

    def _rank_to_grade(rank: int) -> float:
        if rank <= 10:
            return 90.0 + (10 - rank)          # 100 down to 91
        if rank <= 25:
            return 78.0 + (25 - rank) * (11 / 15)
        if rank <= 50:
            return 65.0 + (50 - rank) * (13 / 25)
        if rank <= 100:
            return 50.0 + (100 - rank) * (14 / 50)
        if rank <= 200:
            return 35.0 + (200 - rank) * (14 / 100)
        return max(20.0, 35.0 - (rank - 200) * 0.05)

    scores: list[float] = []
    for g in (pipeline_grade, ba_grade, fg_grade):
        if g is not None:
            scores.append(_convert_scouting(float(g)))
    if consensus_rank is not None and not scores:
        # Only use list rank when no scouting grade is present (to avoid
        # double-counting; the rank-derived grade is noisier than an OFP).
        scores.append(_rank_to_grade(int(consensus_rank)))

    return round(sum(scores) / len(scores), 2) if scores else 55.0


# ---------------------------------------------------------------------------
# Age-adjusted performance (hitters, 0–100)
# ---------------------------------------------------------------------------

def _ops_base_score(ops: float) -> float:
    """Map OPS to a raw performance score (before level/age adjustments)."""
    if ops >= 1.000:
        return 95.0
    if ops >= 0.950:
        return 88.0
    if ops >= 0.900:
        return 80.0
    if ops >= 0.850:
        return 72.0
    if ops >= 0.800:
        return 63.0
    if ops >= 0.750:
        return 52.0
    if ops >= 0.700:
        return 40.0
    return 25.0


def _level_multiplier(level: str) -> float:
    """Adjust raw performance score for the competition level."""
    return {
        "Rookie":   0.70,
        "Complex":  0.70,
        "Low-A":    0.85,
        "High-A":   1.00,
        "Double-A": 1.20,
        "Triple-A": 1.35,
        "MLB":      1.50,
    }.get(level, 1.00)


def _age_bonus(level: str, player_age: float) -> float:
    """Multiplicative age-relative-to-level bonus/penalty.

    Compares the player's age to the expected age for elite prospects at that
    level (_EXPECTED_AGE_BY_LEVEL).  A 19-year-old in Double-A is historically
    exceptional; a 25-year-old in High-A is a red flag.

    Formula: max(0.70, min(1.40, 1.0 + (expected - actual) * 0.12))
    Examples:
        2 years younger than expected → 1.0 + 2*0.12 = 1.24 (capped at 1.40)
        1 year younger               → 1.12
        at expected age              → 1.00 (neutral)
        1 year older                 → 0.88 → rounds to 0.92 via clamp... no:
            1.0 + (-1)*0.12 = 0.88 (within [0.70,1.40], so 0.88)
        2 years older                → 0.76
        3+ years older               → approaches 0.70 floor

    The 0.12-per-year slope means:
        −2 years younger: ×1.24 (roughly +24% boost — Griffin-tier)
        +2 years older:   ×0.76 (roughly −24% penalty)
    Bounds [0.70, 1.40] prevent extreme outliers from dominating.

    NOTE: replaces the old additive _age_adjustment() which applied +5/−4 pts.
    The multiplicative form scales proportionally with the underlying performance
    score rather than adding a fixed offset, making it fairer across high/low
    base scores.
    """
    expected = _EXPECTED_AGE_BY_LEVEL.get(level, 22.5)
    age_vs_expected = expected - player_age  # positive = younger than expected
    bonus = 1.0 + age_vs_expected * 0.12
    return max(0.70, min(1.40, bonus))


def calculate_age_adj_performance(stints: list[dict]) -> float:
    """Weighted average age-adjusted performance score for hitters.

    Each stint dict must contain:
        level      (str)   — e.g. "High-A", "Double-A"
        games      (int)   — games played (used as weight)
        ops        (float) — on-base plus slugging
        player_age (float) — player's age during that stint

    Optional (populated by fetch_hitting_stints when available):
        sb         (int)   — stolen bases
        bb         (int)   — walks (base on balls)
        k          (int)   — strikeouts

    Returns a score in [0, 100].

    Why weighted average by games: a 5-game sample at Triple-A matters far less
    than a 100-game sample at Double-A; games played is a natural weight.

    --- Improvement 1: SB as additive secondary component ---
    Stolen bases are a real fantasy-relevant skill orthogonal to OPS.  We
    normalise to a per-game rate and then scale to 0-100 so it can be blended
    with ops_value (which is also on a ~25-95 scale).
        sb_per_game = sb / games
        sb_raw      = min(sb_per_game * 5.0, 1.5)   (spec formula: 0.2/G→1.0, cap 1.5)
        sb_value    = sb_raw / 1.5 * 100             (rescale 0-1.5 → 0-100)
    So: 0.20 SB/G → 66.7; 0.30+/G (exceptional) → 100.
    If SB data is absent (stint has no 'sb' key), the OPS weight absorbs the
    full share (backward-compatible).

    --- Improvement 2: Age-relative-to-level multiplier ---
    Replaced old additive _age_adjustment (fixed +5/−4 pts) with multiplicative
    _age_bonus, which scales proportionally with the base score.  A 19-year-old
    in Double-A (3.5 years younger than expected 22.5) gets ~1.40× vs a
    25-year-old there who gets ~0.70×.

    --- Improvement 3: BB/K ratio as contact quality signal ---
    Walk-to-strikeout ratio predicts plate discipline and contact quality better
    than OPS alone in small MiLB samples.
        bb_k_ratio      = bb / max(k, 1)
        contact_quality = min(bb_k_ratio / 0.35, 1.5)
    0.35 BB/K → neutral 1.0; 0.50+ → 1.43 (excellent); 0.20 → 0.57 (poor).
    Rescaled to 0-100: contact_quality_100 = contact_quality / 1.5 * 100
    If bb/k data is absent, the contact_quality weight falls back to OPS.

    Blending weights (all signals on 0-100 scale):
        hitting_value = ops_value * 0.80 + sb_value * 0.12 + contact_quality * 0.08
    With only OPS + SB (no bb/k):
        hitting_value = ops_value * 0.85 + sb_value * 0.15
    With only OPS (no sb, no bb/k):
        hitting_value = ops_value * 1.00  (backward-compatible)
    """
    if not stints:
        return 50.0

    total_weight = 0.0
    weighted_sum = 0.0
    for stint in stints:
        level = stint.get("level", "High-A")
        games = float(stint.get("games", 0))
        ops = float(stint.get("ops", 0.750))
        age = float(stint.get("player_age", 22.0))

        if games <= 0:
            continue

        # --- Improvement 1: SB value (rescaled to 0-100) ---
        # sb key present → compute per-game rate, normalise, rescale to 0-100.
        # 0.20 SB/G → sb_raw=1.0 → sb_value=66.7; 0.30+/G (exceptional) → 100.
        # Absent → ops weight absorbs the sb share (backward-compatible).
        has_sb = "sb" in stint
        if has_sb:
            sb_per_game = int(stint.get("sb", 0)) / games
            sb_raw = min(sb_per_game * 5.0, 1.5)   # 0-1.5 range (spec formula)
            sb_value = sb_raw / 1.5 * 100.0         # rescale to 0-100
        else:
            sb_value = 0.0

        # --- Improvement 3: BB/K contact quality (rescaled to 0-100) ---
        # bb/k keys present → compute ratio, normalise, rescale to 0-100.
        # 0.35 BB/K = neutral → 66.7; 0.50+ = excellent → ≥95; 0.20 = poor → 38.
        # Absent → ops weight absorbs the bb/k share (backward-compatible).
        has_bbk = "bb" in stint and "k" in stint
        if has_bbk:
            bb = int(stint.get("bb", 0))
            k = int(stint.get("k", 0))
            bb_k_ratio = bb / max(k, 1)
            # 0.35 BB/K = neutral (1.0 on raw scale); 0.50+ = excellent (1.43+)
            contact_raw = min(bb_k_ratio / 0.35, 1.5)   # 0-1.5 range
            contact_quality = contact_raw / 1.5 * 100.0  # rescale to 0-100
        else:
            contact_quality = 0.0  # absent; weight will fall back to OPS

        # Blend weights depend on which optional signals are available.
        # All inputs are on 0-100 scale so weights are true fractional shares.
        ops_value = _ops_base_score(ops)
        if has_sb and has_bbk:
            # All three signals present: OPS 80%, SB 12%, BB/K 8%
            hitting_value = ops_value * 0.80 + sb_value * 0.12 + contact_quality * 0.08
        elif has_sb:
            # OPS + SB only: OPS 85%, SB 15%
            hitting_value = ops_value * 0.85 + sb_value * 0.15
        else:
            # OPS only: fully backward-compatible
            hitting_value = ops_value

        # --- Improvement 2: multiplicative age-relative-to-level bonus ---
        # Applied to per-stint value BEFORE level weight so it scales with
        # competition context (a big bonus at Low-A doesn't overshadow AA perf).
        bonus = _age_bonus(level, age)
        level_mult = _level_multiplier(level)
        stint_score = min(100.0, hitting_value * bonus * level_mult)

        weighted_sum += stint_score * games
        total_weight += games

    if total_weight == 0:
        return 50.0
    return round(weighted_sum / total_weight, 2)


# ---------------------------------------------------------------------------
# Age-adjusted performance (pitchers, 0–100)
# ---------------------------------------------------------------------------

def _era_base_score(era: float) -> float:
    """Map ERA to a raw performance score for pitching prospects."""
    if era <= 2.00:
        return 95.0
    if era <= 2.99:
        return 85.0
    if era <= 3.49:
        return 75.0
    if era <= 3.99:
        return 63.0
    if era <= 4.99:
        return 48.0
    if era <= 5.99:
        return 33.0
    return 20.0


def _k9_modifier(k9: float) -> float:
    """K/9 add-on to ERA base score."""
    if k9 >= 12.0:
        return 8.0
    if k9 >= 10.0:
        return 4.0
    if k9 >= 8.0:
        return 0.0
    if k9 >= 6.0:
        return -5.0
    return -10.0


def _whip_modifier(whip: float) -> float:
    """WHIP add-on to ERA base score."""
    if whip <= 0.90:
        return 5.0
    if whip <= 1.10:
        return 0.0
    if whip <= 1.30:
        return -5.0
    return -10.0


def calculate_age_adj_performance_pitcher(stints: list[dict]) -> float:
    """Weighted average age-adjusted performance score for pitching prospects.

    Each stint dict must contain:
        level      (str)   — e.g. "High-A", "Double-A"
        ip         (float) — innings pitched (used as weight; skip stints < 10 IP)
        era        (float)
        k9         (float) — strikeouts per 9 innings
        whip       (float)
        player_age (float) — player's age during that stint

    Returns a score in [0, 100].

    Why IP as weight instead of games: a pitcher can have 30 game appearances but
    60% as a reliever; IP better represents the role and sample depth.
    """
    if not stints:
        return 50.0

    total_weight = 0.0
    weighted_sum = 0.0
    for stint in stints:
        level = stint.get("level", "High-A")
        ip = float(stint.get("ip", 0.0))
        era = float(stint.get("era", 4.00))
        k9 = float(stint.get("k9", 7.0))
        whip = float(stint.get("whip", 1.30))
        age = float(stint.get("player_age", 22.0))

        if ip < 10.0:
            # Too small a sample to trust; skip to avoid noise
            continue

        base = _era_base_score(era)
        level_mult = _level_multiplier(level)
        # Use multiplicative age bonus (Improvement 2) — consistent with hitter scoring.
        bonus = _age_bonus(level, age)
        stint_score = min(100.0, (base + _k9_modifier(k9) + _whip_modifier(whip)) * bonus * level_mult)

        weighted_sum += stint_score * ip
        total_weight += ip

    if total_weight == 0:
        return 50.0
    return round(weighted_sum / total_weight, 2)


# ---------------------------------------------------------------------------
# Vertical velocity (0–100)
# ---------------------------------------------------------------------------

def calculate_vertical_velocity(
    levels_in_season: int,
    highest_level: str,
    years_pro: int,
) -> float:
    """Measure how fast the prospect is ascending through the system.

    Two sub-components averaged:
    1. Levels advanced in current/most recent full season.
    2. Highest level reached relative to draft/signing year.

    Why both? A player who blitzed to Double-A in Year 1 is more impressive than
    one who took three years to get there, even if their stat lines are identical.
    """
    # Sub-component 1: levels jumped in the season
    levels_score = {
        1: 30.0,
        2: 65.0,
        3: 90.0,
    }.get(min(levels_in_season, 3), 100.0 if levels_in_season >= 4 else 30.0)

    # Sub-component 2: highest level vs years pro
    level_idx = LEVEL_ORDER.index(highest_level) if highest_level in LEVEL_ORDER else 2
    aa_or_higher = level_idx >= LEVEL_ORDER.index("Double-A")
    aaa_or_higher = level_idx >= LEVEL_ORDER.index("Triple-A")
    high_a_or_higher = level_idx >= LEVEL_ORDER.index("High-A")

    if years_pro <= 1:
        if aa_or_higher:
            career_score = 95.0
        elif high_a_or_higher:
            career_score = 75.0
        else:
            career_score = 40.0
    elif years_pro == 2:
        if aaa_or_higher:
            career_score = 90.0
        elif aa_or_higher:
            career_score = 72.0
        elif high_a_or_higher:
            career_score = 55.0
        else:
            career_score = 35.0
    else:  # year 3+
        if aaa_or_higher:
            career_score = 75.0
        elif aa_or_higher:
            career_score = 55.0
        else:
            career_score = 30.0

    return round((levels_score + career_score) / 2.0, 2)


# ---------------------------------------------------------------------------
# ETA proximity (0–100)
# ---------------------------------------------------------------------------

_ETA_SCORES: dict[str, float] = {
    "mlb_roster":    97.0,
    "aaa_imminent":  85.0,
    "aa_within_1yr": 68.0,
    "aa_within_2yr": 52.0,
    "high_a_near":   40.0,
    "high_a_far":    28.0,
    "low_a_rookie":  15.0,
}


def calculate_eta_proximity(situation: str) -> float:
    """Map the player's current ETA situation to a 0–100 score.

    Situation strings:
        'mlb_roster'    — on MLB 40-man or strong Opening Day candidate
        'aaa_imminent'  — Triple-A, call-up expected within the season
        'aa_within_1yr' — Double-A, MLB debut projected within 12 months
        'aa_within_2yr' — Double-A, MLB debut in 12–24 months
        'high_a_near'   — High-A, 1–2 years out
        'high_a_far'    — High-A, 2+ years out
        'low_a_rookie'  — Low-A or Rookie ball, 2+ years out
    """
    return _ETA_SCORES.get(situation, 40.0)


def derive_eta_situation(
    highest_level: str,
    years_pro: int,
    levels_in_season: int = 1,
) -> str:
    """Infer ETA situation from available data when not explicitly provided.

    This is a rule-based heuristic used by the pipeline when no manual ETA is set.
    """
    level_idx = LEVEL_ORDER.index(highest_level) if highest_level in LEVEL_ORDER else 2

    if level_idx >= LEVEL_ORDER.index("MLB"):
        return "mlb_roster"
    if level_idx >= LEVEL_ORDER.index("Triple-A"):
        return "aaa_imminent"
    if level_idx >= LEVEL_ORDER.index("Double-A"):
        # Velocity signal: rapid advancement suggests sooner
        if years_pro <= 2 or levels_in_season >= 2:
            return "aa_within_1yr"
        return "aa_within_2yr"
    if level_idx >= LEVEL_ORDER.index("High-A"):
        if years_pro <= 2:
            return "high_a_near"
        return "high_a_far"
    return "low_a_rookie"


# ---------------------------------------------------------------------------
# PAV calculation
# ---------------------------------------------------------------------------

def calculate_pav(
    prospect_grade: float,
    age_adj_perf: float,
    vertical_velocity: float,
    eta_proximity: float,
    position: str = "OF",
    pitcher: bool = False,
) -> dict:
    """Compute the final PAV score.

    Args:
        prospect_grade:    0–100 scouting/grade signal (ceiling input)
        age_adj_perf:      0–100 age-adjusted MiLB performance
        vertical_velocity: 0–100 ascent speed through the system
        eta_proximity:     0–100 closeness to MLB debut
        position:          Primary position string (e.g. "SS", "SP")
        pitcher:           True for pitching prospects

    Returns a dict with:
        component_scores   — all four inputs
        multiplier         — the readiness bracket (0.1–1.0)
        pav_pre_position   — PAV before positional scarcity
        pav_final          — after positional scarcity multiplier
        proxy_mlb_rank     — estimated equivalent MLB overall rank
        summary            — human-readable breakdown string
    """
    multiplier = (age_adj_perf + vertical_velocity + eta_proximity) / 300.0
    pav_pre = round(prospect_grade * multiplier, 2)

    if pitcher:
        pos_mult = POSITION_MULTIPLIERS_PITCH.get(position.upper(), 1.00)
    else:
        pos_mult = POSITION_MULTIPLIERS_HIT.get(position.upper(), 1.00)

    pav_final = round(pav_pre * pos_mult, 2)
    proxy_rank = pav_to_proxy_rank(pav_final)

    summary = (
        f"Grade={prospect_grade:.1f} × ({age_adj_perf:.1f}+{vertical_velocity:.1f}+{eta_proximity:.1f})/300"
        f" = {multiplier:.3f} | PAV={pav_pre:.1f} × {pos_mult:.2f}({position}) → {pav_final:.1f}"
        f" → proxy rank ≈ {proxy_rank}"
    )

    return {
        "component_scores": {
            "prospect_grade":    prospect_grade,
            "age_adj_perf":      age_adj_perf,
            "vertical_velocity": vertical_velocity,
            "eta_proximity":     eta_proximity,
        },
        "multiplier":       round(multiplier, 4),
        "pav_pre_position": pav_pre,
        "pav_final":        pav_final,
        "proxy_mlb_rank":   proxy_rank,
        "summary":          summary,
    }


def pav_to_proxy_rank(pav_final: float) -> int:
    """Convert a PAV_final score to an estimated overall MLB rank equivalent.

    Calibration (Griffin PAV ≈ 93 → rank ≈ 46):
        rank = max(PAV_PROXY_MIN, round(PAV_PROXY_OFFSET + (100 - pav) × PAV_PROXY_SCALE))
    """
    return max(PAV_PROXY_MIN, round(PAV_PROXY_OFFSET + (100.0 - pav_final) * PAV_PROXY_SCALE))


# ---------------------------------------------------------------------------
# Convenience wrappers
# ---------------------------------------------------------------------------

def score_prospect(
    name: str,
    team: str,
    position: str = "OF",
    stints: Optional[list] = None,
    levels_in_season: int = 1,
    highest_level: str = "High-A",
    years_pro: int = 1,
    eta_situation: Optional[str] = None,
    pipeline_grade: Optional[float] = None,
    ba_grade: Optional[float] = None,
    fg_grade: Optional[float] = None,
    consensus_rank: Optional[int] = None,
    pitcher: bool = False,
) -> dict:
    """Convenience wrapper: compute all PAV components and return full result.

    All keyword arguments have sensible defaults so partial data works gracefully.
    """
    grade = calculate_prospect_grade(
        pipeline_grade=pipeline_grade,
        ba_grade=ba_grade,
        fg_grade=fg_grade,
        consensus_rank=consensus_rank,
    )

    if pitcher:
        perf = calculate_age_adj_performance_pitcher(stints or [])
    else:
        perf = calculate_age_adj_performance(stints or [])

    velocity = calculate_vertical_velocity(
        levels_in_season=levels_in_season,
        highest_level=highest_level,
        years_pro=years_pro,
    )

    eta = calculate_eta_proximity(
        eta_situation or derive_eta_situation(highest_level, years_pro, levels_in_season)
    )

    result = calculate_pav(
        prospect_grade=grade,
        age_adj_perf=perf,
        vertical_velocity=velocity,
        eta_proximity=eta,
        position=position,
        pitcher=pitcher,
    )

    result["name"] = name
    result["team"] = team
    result["position"] = position
    result["pitcher"] = pitcher
    return result


# ---------------------------------------------------------------------------
# Reference validation: Konnor Griffin
# ---------------------------------------------------------------------------

def validate_griffin() -> dict:
    """Run the reference test case from the PRD and assert expected output ranges.

    Griffin (PIT → DET, SS, age 19, #1 overall prospect, 2024 draft):
        Low-A  (50 G):  .338 BA, .932 OPS, 9 HR, 26 SB   — age 19
        High-A (51 G):  .942 OPS, 7 HR, 33 SB             — age 19
        Double-A (21 G): .960 OPS, 5 HR, 6 SB             — age 19

    Stints now include 'sb' to exercise the SB-value component (Improvement 1).
    BB/K data is not included here (not in original PRD stub), so contact_quality
    falls back to neutral and OPS+SB blending is used.

    The age-relative-to-level bonus (Improvement 2) is substantial for Griffin:
        Low-A:    expected 20.5, actual 19.0 → ×1.18
        High-A:   expected 21.5, actual 19.0 → ×1.30
        Double-A: expected 22.5, actual 19.0 → capped at ×1.40
    This pushes AgeAdjPerf higher than the old additive formula, reflecting how
    extraordinary a 19-year-old in Double-A truly is.

    Expected (updated for new scoring):
        AgeAdjPerformance ≈ 88–100  (age bonus boosts well above old 88–92 range)
        VerticalVelocity  ≈ 90–100
        ETAProximity      ≈ 80–100  (Opening Day 2026 candidate)
        PAV_final (×1.10) ≈ 85–100
        proxy_rank        ≤ 60
    """
    stints = [
        # sb included (Improvement 1); bb/k omitted → OPS+SB blend used
        {"level": "Low-A",    "games": 50, "ops": 0.932, "sb": 26, "player_age": 19.0},
        {"level": "High-A",   "games": 51, "ops": 0.942, "sb": 33, "player_age": 19.0},
        {"level": "Double-A", "games": 21, "ops": 0.960, "sb":  6, "player_age": 19.0},
    ]

    result = score_prospect(
        name="Konnor Griffin",
        team="DET",
        position="SS",
        stints=stints,
        levels_in_season=3,
        highest_level="Double-A",
        years_pro=1,
        eta_situation="aaa_imminent",   # Opening Day 2026 candidate
        consensus_rank=1,               # #1 overall prospect
    )

    perf = result["component_scores"]["age_adj_perf"]
    vel  = result["component_scores"]["vertical_velocity"]
    eta  = result["component_scores"]["eta_proximity"]
    pav  = result["pav_final"]
    rank = result["proxy_mlb_rank"]

    assert 88.0 <= perf <= 100.0, f"AgeAdjPerf out of range: {perf}"
    assert 88.0 <= vel  <= 100.0, f"VerticalVelocity out of range: {vel}"
    assert 80.0 <= eta  <= 100.0, f"ETAProximity out of range: {eta}"
    assert 85.0 <= pav  <= 100.0, f"PAV_final out of range: {pav}"
    assert rank <= 60, f"proxy_rank too high: {rank} (expected ≤ 60)"

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("PAV Scorer — Konnor Griffin validation")
    print("=" * 60)
    r = validate_griffin()
    cs = r["component_scores"]
    print(f"  Prospect Grade:    {cs['prospect_grade']:.1f}")
    print(f"  Age-Adj Perf:      {cs['age_adj_perf']:.1f}")
    print(f"  Vertical Velocity: {cs['vertical_velocity']:.1f}")
    print(f"  ETA Proximity:     {cs['eta_proximity']:.1f}")
    print(f"  Readiness mult:    {r['multiplier']:.3f}")
    print(f"  PAV (pre-pos):     {r['pav_pre_position']:.1f}")
    print(f"  PAV (final):       {r['pav_final']:.1f}")
    print(f"  Proxy MLB rank:    #{r['proxy_mlb_rank']}")
    print()
    print("  ✓ All assertions passed")
