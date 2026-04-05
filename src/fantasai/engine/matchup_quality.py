"""Opponent matchup quality scoring for weekly blurbs.

Computes a matchup quality score for each player based on their opponents
this week. Scores are z-score normalized within batter/pitcher pools so
that relative comparisons are meaningful.

Pitcher matchup score (how easy is the opposing lineup?):
  60%  base_woba    — opponent team wOBA; lower = easier for pitcher
  20%  k_synergy    — amplifier when pitcher K/9 ≥ 9.0 × opponent K%
  15%  gb_defense   — ground-ball defense bonus (small sample caveat early season)
   5%  handedness   — LHP platoon adjustment when throws column is populated

Batter matchup score (how weak is the opposing pitching this week?):
  60%  sp_quality   — opposing probable SP xFIP per start (higher = easier)
  40%  bullpen      — opposing team aggregate ERA (higher = easier)
Both are averaged across the week's games.

Z-score tiers (applied within each pool independently):
  z ≥  1.50  → "Elite matchup"
  z ≥  0.75  → "Very favorable"
  z ≥  0.25  → "Favorable"
  z ≥ -0.25  → "Neutral"
  z ≥ -0.75  → "Unfavorable"
  z ≥ -1.50  → "Tough draw"
  z <  -1.50 → "Nightmare matchup"
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from fantasai.engine.schedule import PlayerSchedule

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# League average baselines (2024/2025 MLB)
# ---------------------------------------------------------------------------

_LEAGUE_AVG_WOBA = 0.320
_LEAGUE_AVG_K_PCT = 0.229     # team batting K rate
_LEAGUE_AVG_SP_XFIP = 4.20   # SP xFIP
_LEAGUE_AVG_TEAM_ERA = 4.10   # team pitching ERA

# Below this many team games played → blend YTD with league average
_EARLY_SEASON_BLEND_THRESHOLD = 15

# ---------------------------------------------------------------------------
# Module-level cache: season → (monotonic_ts, batting_data, pitching_data)
# ---------------------------------------------------------------------------

_TEAM_STATS_CACHE: dict[int, tuple[float, dict, dict]] = {}
_TEAM_STATS_TTL = 6 * 3600   # 6 hours

# ---------------------------------------------------------------------------
# FanGraphs team abbreviation → our internal abbreviation
# ---------------------------------------------------------------------------

_FG_TO_INTERNAL: dict[str, str] = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BOS": "BOS",
    "CHC": "CHC", "CHW": "CHW", "CIN": "CIN", "CLE": "CLE",
    "COL": "COL", "DET": "DET", "HOU": "HOU", "KCR": "KCR",
    "KC":  "KCR", "LAA": "LAA", "LAD": "LAD", "MIA": "MIA",
    "MIL": "MIL", "MIN": "MIN", "NYM": "NYM", "NYY": "NYY",
    "OAK": "OAK", "ATH": "ATH", "PHI": "PHI", "PIT": "PIT",
    "SD":  "SDP", "SDP": "SDP", "SF":  "SFG", "SFG": "SFG",
    "SEA": "SEA", "STL": "STL", "TB":  "TBR", "TBR": "TBR",
    "TEX": "TEX", "TOR": "TOR", "WSN": "WSH", "WSH": "WSH",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class StartMatchup:
    """Quality assessment for one SP start against a specific opponent."""
    opponent_abbr: str
    tier: str     # e.g. "Favorable" — filled after pool z-normalization
    detail: str   # brief context line, e.g. "vs PHI: weak offense (.303 wOBA)"


@dataclass
class MatchupQuality:
    """Matchup quality for a player this week."""
    z_score: float     # z-score within pool (higher = better for player)
    tier: str          # "Elite matchup" … "Nightmare matchup"
    pool_rank: int     # 1 = best matchup in pool
    pool_size: int     # total players in pool
    details: str       # formatted block ready for blurb prompt injection
    starts: list[StartMatchup] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val: object, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _z_to_tier(z: float) -> str:
    if z >= 1.50:
        return "Elite matchup"
    elif z >= 0.75:
        return "Very favorable"
    elif z >= 0.25:
        return "Favorable"
    elif z >= -0.25:
        return "Neutral"
    elif z >= -0.75:
        return "Unfavorable"
    elif z >= -1.50:
        return "Tough draw"
    else:
        return "Nightmare matchup"


def _blend(ytd_val: float, avg_val: float, games: int) -> float:
    """Blend YTD value toward league average when sample is small."""
    w = min(1.0, games / _EARLY_SEASON_BLEND_THRESHOLD)
    return ytd_val * w + avg_val * (1.0 - w)


# ---------------------------------------------------------------------------
# Team stats fetch (pybaseball, cached)
# ---------------------------------------------------------------------------

def _fetch_team_stats(season: int) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (batting_by_team, pitching_by_team) from FanGraphs via pybaseball.

    batting_by_team:  {abbr: {wOBA, K_pct, games}}
    pitching_by_team: {abbr: {ERA, xFIP, K_pct, games}}

    Results are cached for 6 hours. Returns ({}, {}) on any failure so that
    matchup quality degrades gracefully to neutral scores.
    """
    cached = _TEAM_STATS_CACHE.get(season)
    if cached is not None:
        ts, batting, pitching = cached
        if time.monotonic() - ts <= _TEAM_STATS_TTL:
            return batting, pitching

    batting: dict[str, dict] = {}
    pitching: dict[str, dict] = {}

    try:
        from pybaseball import team_batting, team_pitching  # type: ignore[import]

        try:
            bat_df = team_batting(season)
            for _, row in bat_df.iterrows():
                raw = str(row.get("Team") or "").strip()
                abbr = _FG_TO_INTERNAL.get(raw, raw)
                if not abbr:
                    continue
                batting[abbr] = {
                    "wOBA":  _safe_float(row.get("wOBA"),  _LEAGUE_AVG_WOBA),
                    "K_pct": _safe_float(row.get("K%"),    _LEAGUE_AVG_K_PCT),
                    "games": int(_safe_float(row.get("G"), 0)),
                }
        except Exception as exc:
            _log.warning("matchup_quality: team_batting(%d) failed: %s", season, exc)

        try:
            pit_df = team_pitching(season)
            for _, row in pit_df.iterrows():
                raw = str(row.get("Team") or "").strip()
                abbr = _FG_TO_INTERNAL.get(raw, raw)
                if not abbr:
                    continue
                pitching[abbr] = {
                    "ERA":   _safe_float(row.get("ERA"),   _LEAGUE_AVG_TEAM_ERA),
                    "xFIP":  _safe_float(row.get("xFIP"),  _LEAGUE_AVG_SP_XFIP),
                    "K_pct": _safe_float(row.get("K%"),    _LEAGUE_AVG_K_PCT),
                    "games": int(_safe_float(row.get("G"), 0)),
                }
        except Exception as exc:
            _log.warning("matchup_quality: team_pitching(%d) failed: %s", season, exc)

    except ImportError:
        _log.warning("matchup_quality: pybaseball not available; matchup scores will be neutral")

    _TEAM_STATS_CACHE[season] = (time.monotonic(), batting, pitching)
    return batting, pitching


# ---------------------------------------------------------------------------
# Per-pitcher stats from DB
# ---------------------------------------------------------------------------

def _get_pitcher_stats_bulk(
    player_ids: list[int],
    db: "Session",
    season: int,
) -> dict[int, dict]:
    """Return {player_id: {throws, k9, gb_pct, xfip}} for a list of pitchers."""
    from fantasai.models.player import Player, PlayerStats

    players = db.query(Player).filter(Player.player_id.in_(player_ids)).all()
    throws_map = {p.player_id: getattr(p, "throws", None) for p in players}

    rows = (
        db.query(PlayerStats)
        .filter(
            PlayerStats.player_id.in_(player_ids),
            PlayerStats.season == season,
            PlayerStats.stat_type == "pitching",
            PlayerStats.week.is_(None),
        )
        .all()
    )

    # Separate actual vs projection rows per player
    actual: dict[int, PlayerStats] = {}
    proj: dict[int, PlayerStats] = {}
    for row in rows:
        pid = row.player_id
        if row.data_source == "actual":
            actual[pid] = row
        else:
            proj[pid] = row

    result: dict[int, dict] = {}
    for pid in player_ids:
        row = actual.get(pid) or proj.get(pid)
        if row is None:
            result[pid] = {
                "throws": throws_map.get(pid),
                "k9": 0.0, "gb_pct": 0.0, "xfip": _LEAGUE_AVG_SP_XFIP,
            }
            continue

        rate = row.rate_stats or {}
        adv  = row.advanced_stats or {}
        ip   = _safe_float((row.counting_stats or {}).get("IP"), 0.0)

        # Prefer projection for small-sample rate stats
        if ip < 15.0 and pid in proj:
            p_row  = proj[pid]
            p_rate = p_row.rate_stats or {}
            p_adv  = p_row.advanced_stats or {}
            result[pid] = {
                "throws":  throws_map.get(pid),
                "k9":      _safe_float(p_rate.get("K/9"), 0.0),
                "gb_pct":  _safe_float(p_adv.get("GB%") or p_rate.get("GB%"), 0.0),
                "xfip":    _safe_float(
                    p_adv.get("xFIP") or p_rate.get("xFIP"), _LEAGUE_AVG_SP_XFIP
                ),
            }
        else:
            result[pid] = {
                "throws":  throws_map.get(pid),
                "k9":      _safe_float(rate.get("K/9"), 0.0),
                "gb_pct":  _safe_float(adv.get("GB%") or rate.get("GB%"), 0.0),
                "xfip":    _safe_float(
                    adv.get("xFIP") or rate.get("xFIP"), _LEAGUE_AVG_SP_XFIP
                ),
            }
    return result


# ---------------------------------------------------------------------------
# Look up opposing SP xFIP by MLBAM ID
# ---------------------------------------------------------------------------

def _get_sp_xfip_by_mlbam(
    mlbam_ids: set[int],
    db: "Session",
    season: int,
) -> dict[int, float]:
    """Return {mlbam_id: xfip} for probable starters found in our DB."""
    if not mlbam_ids:
        return {}

    from fantasai.models.player import Player, PlayerStats

    players = (
        db.query(Player)
        .filter(Player.mlbam_id.in_(list(mlbam_ids)))
        .all()
    )
    mlbam_to_pid = {p.mlbam_id: p.player_id for p in players if p.mlbam_id}
    if not mlbam_to_pid:
        return {}

    rows = (
        db.query(PlayerStats)
        .filter(
            PlayerStats.player_id.in_(list(mlbam_to_pid.values())),
            PlayerStats.season == season,
            PlayerStats.stat_type == "pitching",
            PlayerStats.week.is_(None),
        )
        .all()
    )

    # Minimum IP thresholds: actual stats need at least 5 IP to be reliable;
    # projection stats are always accepted regardless of IP.
    _MIN_ACTUAL_IP = 5.0

    pid_to_xfip: dict[int, float] = {}
    for row in rows:
        adv   = row.advanced_stats or {}
        rate  = row.rate_stats or {}
        xfip  = _safe_float(adv.get("xFIP") or rate.get("xFIP"), None)  # type: ignore[arg-type]
        if xfip is None or xfip <= 0:
            continue
        ip    = _safe_float((row.counting_stats or {}).get("IP"), 0.0)
        # Skip tiny-sample actual rows — their xFIP is too noisy
        if row.data_source == "actual" and ip < _MIN_ACTUAL_IP:
            continue
        # Prefer actual over projection when sample is sufficient
        if row.player_id not in pid_to_xfip or row.data_source == "actual":
            pid_to_xfip[row.player_id] = xfip

    return {
        mlbam_id: pid_to_xfip[pid]
        for mlbam_id, pid in mlbam_to_pid.items()
        if pid in pid_to_xfip
    }


# ---------------------------------------------------------------------------
# Raw score computation
# ---------------------------------------------------------------------------

def _pitcher_raw_score(
    player_id: int,
    ps: "PlayerSchedule",
    p_stats: dict,
    team_bat: dict[str, dict],
) -> tuple[float, list[StartMatchup]]:
    """Compute raw matchup score for an SP. Higher = easier week for pitcher."""
    opponents = ps.opponent_teams
    if not opponents:
        return 0.0, []

    k9      = p_stats.get("k9", 0.0)
    throws  = p_stats.get("throws")

    starts: list[StartMatchup] = []
    total   = 0.0

    for opp in opponents:
        opp_data = team_bat.get(opp, {})
        games    = opp_data.get("games", 0)

        opp_woba  = _blend(opp_data.get("wOBA",  _LEAGUE_AVG_WOBA),  _LEAGUE_AVG_WOBA,  games)
        opp_k_pct = _blend(opp_data.get("K_pct", _LEAGUE_AVG_K_PCT), _LEAGUE_AVG_K_PCT, games)

        # Component 1 (60%) — base wOBA: lower opponent wOBA = easier
        base = (_LEAGUE_AVG_WOBA - opp_woba) * 10.0   # range ≈ [-0.5, +0.5]

        # Component 2 (20%) — K-synergy: high-K pitcher vs strikeout-prone lineup
        k_syn = 0.0
        if k9 >= 9.0:
            # How much above league avg is opponent K%? Positive = strikeout-prone = easier
            k_syn = max(0.0, (k9 - 9.0) / 3.0) * (opp_k_pct - _LEAGUE_AVG_K_PCT) * 5.0

        # Component 3 (15%) — GB/defense: omitted early season (noisy < 3 weeks)
        gb_def = 0.0

        # Component 4 (5%) — handedness: LHPs face slight platoon disadvantage
        # because most lineups are majority RHH
        hand = -0.05 if throws == "L" else 0.0

        raw = 0.60 * base + 0.20 * k_syn + 0.15 * gb_def + 0.05 * hand

        # Build a detail line
        woba_str = f".{int(opp_woba * 1000):03d}"
        if opp_woba < _LEAGUE_AVG_WOBA - 0.010:
            woba_note = f"weak offense ({woba_str} wOBA)"
        elif opp_woba > _LEAGUE_AVG_WOBA + 0.010:
            woba_note = f"potent offense ({woba_str} wOBA)"
        else:
            woba_note = f"average offense ({woba_str} wOBA)"

        k_note = ""
        if k9 >= 9.0 and opp_k_pct >= _LEAGUE_AVG_K_PCT + 0.020:
            k_note = f", high-K lineup ({opp_k_pct:.1%} K rate)"

        starts.append(StartMatchup(
            opponent_abbr=opp,
            tier="",   # filled after pool z-normalization
            detail=f"vs {opp}: {woba_note}{k_note}",
        ))
        total += raw

    return total / len(opponents), starts


def _batter_raw_score(
    ps: "PlayerSchedule",
    sp_xfip_map: dict[int, float],
    team_pit: dict[str, dict],
) -> tuple[float, list[tuple[str, float]]]:
    """Compute raw matchup score for a batter. Higher = easier week.

    Returns (raw_score, [(opp_abbr, sp_xfip), ...]) for per-start context.
    """
    game_log = ps.batter_game_log
    if not game_log:
        return 0.0, []

    total          = 0.0
    per_start_info: list[tuple[str, float]] = []   # (opp_abbr, sp_xfip_used)

    for game in game_log:
        sp_mlbam = game.get("sp_mlbam_id")
        opp      = str(game.get("opponent_abbr") or "").upper()

        opp_pit  = team_pit.get(opp, {})
        bp_era   = _blend(
            opp_pit.get("ERA", _LEAGUE_AVG_TEAM_ERA),
            _LEAGUE_AVG_TEAM_ERA,
            opp_pit.get("games", 0),
        )

        # Opposing SP xFIP — prefer DB lookup; fall back to team aggregate
        sp_xfip = sp_xfip_map.get(sp_mlbam) if sp_mlbam else None
        if sp_xfip is None:
            sp_xfip = opp_pit.get("xFIP", _LEAGUE_AVG_SP_XFIP)

        # Higher xFIP / ERA = weaker pitching = better for batter
        sp_score  = (sp_xfip - _LEAGUE_AVG_SP_XFIP) * 2.0
        bp_score  = (bp_era  - _LEAGUE_AVG_TEAM_ERA) * 1.5

        game_score = 0.60 * sp_score + 0.40 * bp_score
        total += game_score
        per_start_info.append((opp, sp_xfip))

    return total / len(game_log), per_start_info


# ---------------------------------------------------------------------------
# Z-score normalization
# ---------------------------------------------------------------------------

def _z_normalize(raw_dict: dict[int, float]) -> dict[int, float]:
    if len(raw_dict) < 2:
        return {k: 0.0 for k in raw_dict}
    vals  = list(raw_dict.values())
    mean  = sum(vals) / len(vals)
    var   = sum((v - mean) ** 2 for v in vals) / len(vals)
    std   = var ** 0.5
    if std < 1e-6:
        return {k: 0.0 for k in raw_dict}
    return {k: (v - mean) / std for k, v in raw_dict.items()}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_all_matchup_scores(
    schedule: "dict[int, PlayerSchedule]",
    db: "Session",
    season: int = 2026,
) -> dict[int, MatchupQuality]:
    """Compute z-score-normalized matchup quality for all players in the schedule.

    Called once per weekly blurb generation run (after fetch_weekly_schedule).
    Non-fatal: returns {} on critical failure so blurbs continue without
    matchup context.

    Returns {player_id: MatchupQuality}.
    """
    if not schedule:
        return {}

    team_bat, team_pit = _fetch_team_stats(season)

    # Load player positions
    from fantasai.models.player import Player
    player_ids = list(schedule.keys())
    players = db.query(Player).filter(Player.player_id.in_(player_ids)).all()
    pos_map: dict[int, list[str]] = {p.player_id: (p.positions or []) for p in players}

    # Collect SP mlbam_ids needed for batter matchup xFIP lookups
    all_sp_mlbam: set[int] = set()
    for ps in schedule.values():
        for game in ps.batter_game_log:
            sid = game.get("sp_mlbam_id")
            if sid:
                all_sp_mlbam.add(int(sid))

    sp_xfip_map = _get_sp_xfip_by_mlbam(all_sp_mlbam, db, season)

    # Bulk-fetch pitcher stats for all SPs in schedule
    sp_ids = [
        pid for pid, pos in pos_map.items()
        if "SP" in pos and schedule[pid].opponent_teams
    ]
    pitcher_stats_map = _get_pitcher_stats_bulk(sp_ids, db, season) if sp_ids else {}

    # Compute raw scores
    pitcher_raw: dict[int, tuple[float, list[StartMatchup]]] = {}
    batter_raw:  dict[int, tuple[float, list[tuple[str, float]]]] = {}

    for player_id, ps in schedule.items():
        pos = pos_map.get(player_id, [])
        is_sp = "SP" in pos
        is_rp = "RP" in pos and not is_sp

        if is_sp and ps.opponent_teams:
            p_stats = pitcher_stats_map.get(player_id, {})
            raw, starts = _pitcher_raw_score(player_id, ps, p_stats, team_bat)
            pitcher_raw[player_id] = (raw, starts)
        elif not is_sp and not is_rp and ps.batter_game_log:
            raw, per_start = _batter_raw_score(ps, sp_xfip_map, team_pit)
            batter_raw[player_id] = (raw, per_start)

    # Z-normalize within pools
    p_z = _z_normalize({pid: s for pid, (s, _) in pitcher_raw.items()})
    b_z = _z_normalize({pid: s for pid, (s, _) in batter_raw.items()})

    # Rank maps (1 = best matchup)
    p_ranked = sorted(p_z.items(), key=lambda x: x[1], reverse=True)
    b_ranked = sorted(b_z.items(), key=lambda x: x[1], reverse=True)
    p_rank_map = {pid: i + 1 for i, (pid, _) in enumerate(p_ranked)}
    b_rank_map = {pid: i + 1 for i, (pid, _) in enumerate(b_ranked)}

    result: dict[int, MatchupQuality] = {}

    # ── Pitchers ──────────────────────────────────────────────────────────────
    for player_id, (_, starts) in pitcher_raw.items():
        z    = p_z.get(player_id, 0.0)
        tier = _z_to_tier(z)
        rank = p_rank_map.get(player_id, 0)
        n    = len(pitcher_raw)

        start_lines = [f"  {sm.detail}" for sm in starts]
        body        = "\n".join(start_lines)
        details     = (
            f"Pitcher matchup: {tier} (#{rank} of {n} SP matchup slots this week)\n"
            + body
        ).rstrip()

        result[player_id] = MatchupQuality(
            z_score=z, tier=tier,
            pool_rank=rank, pool_size=n,
            details=details, starts=starts,
        )

    # ── Batters ───────────────────────────────────────────────────────────────
    for player_id, (_, per_start) in batter_raw.items():
        z    = b_z.get(player_id, 0.0)
        tier = _z_to_tier(z)
        rank = b_rank_map.get(player_id, 0)
        n    = len(batter_raw)

        # Build detail lines
        lines: list[str] = [f"Batter matchup: {tier} (#{rank} of {n} batter matchup slots this week)"]
        if per_start:
            valid = [(opp, xfip) for opp, xfip in per_start if xfip > 0]
            if valid:
                avg_xfip = sum(x for _, x in valid) / len(valid)
                lines.append(f"  Avg opposing SP xFIP this week: {avg_xfip:.2f}")
                if len(valid) >= 2:
                    best  = max(valid, key=lambda t: t[1])
                    worst = min(valid, key=lambda t: t[1])
                    if abs(best[1] - worst[1]) >= 0.30:   # only surface meaningful gaps
                        lines.append(
                            f"  Best game: vs {best[0]} ({best[1]:.2f} SP xFIP)"
                        )
                        lines.append(
                            f"  Toughest: vs {worst[0]} ({worst[1]:.2f} SP xFIP)"
                        )

        result[player_id] = MatchupQuality(
            z_score=z, tier=tier,
            pool_rank=rank, pool_size=n,
            details="\n".join(lines),
            starts=[],
        )

    _log.info(
        "matchup_quality: %d pitcher + %d batter scores (season=%d)",
        len(pitcher_raw), len(batter_raw), season,
    )
    return result
