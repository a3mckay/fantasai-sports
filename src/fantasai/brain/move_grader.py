"""Move Grader — grades fantasy baseball transactions A+ through F.

Uses player ranking data and league context to evaluate the quality of
adds, drops, and trades. Calls Claude Haiku for a 2-sentence rationale.
Grade card images are generated separately by grade_card.py.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from fantasai.models.transaction import Transaction
    from fantasai.models.league import League

_log = logging.getLogger(__name__)

# Live rank map cache — rebuilt at most every 5 min (matches _compute_rankings TTL).
# Key: sorted-categories string → (monotonic_ts, {player_id: overall_rank}).
_LIVE_RANK_CACHE: dict[str, tuple[float, dict[int, int]]] = {}
_LIVE_RANK_TTL = 300.0

# Grade → numeric score mapping (4.3-scale GPA)
_GRADE_SCORES: dict[str, float] = {
    "A+": 4.3, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F":  0.0,
}

# Score thresholds for grade boundaries
_SCORE_GRADES = [
    (3.85, "A+"), (3.50, "A"), (3.15, "A-"),
    (2.85, "B+"), (2.50, "B"), (2.15, "B-"),
    (1.85, "C+"), (1.50, "C"), (1.15, "C-"),
    (0.85, "D+"), (0.50, "D"), (0.15, "D-"),
    (-999, "F"),
]


def _score_to_letter(score: float) -> str:
    for threshold, letter in _SCORE_GRADES:
        if score >= threshold:
            return letter
    return "F"


def _get_live_rank_map(db: "Session", categories: list[str]) -> dict[int, int]:
    """Return a player_id → ROS rank dict using the same live path as the Rankings page.

    Uses _compute_rankings (cached up to 5 min) + _inject_prospect_rankings so
    the numbers here always match what the user sees on the Rankings page.
    """
    cache_key = ",".join(sorted(str(c) for c in (categories or [])))
    cached = _LIVE_RANK_CACHE.get(cache_key)
    if cached and (_time.monotonic() - cached[0]) < _LIVE_RANK_TTL:
        return cached[1]
    try:
        from fantasai.api.v1.recommendations import _compute_rankings, _inject_prospect_rankings
        from fantasai.engine.projection import ProjectionHorizon
        # no_autoflush prevents dirty session objects (e.g. partially-graded txn)
        # from being flushed mid-query, which would abort the PG transaction.
        with db.no_autoflush:
            _, predictive = _compute_rankings(db, categories or [], horizon=ProjectionHorizon.SEASON)
            enriched = _inject_prospect_rankings(predictive, db)
        rank_map: dict[int, int] = {r.player_id: r.overall_rank for r in enriched}
        _LIVE_RANK_CACHE[cache_key] = (_time.monotonic(), rank_map)
        return rank_map
    except Exception:
        _log.debug("_get_live_rank_map: failed", exc_info=True)
        return {}


def _get_player_rank(db: "Session", player_id: Optional[int], league_categories: list[str]) -> Optional[int]:
    """Return the live ROS predictive rank for a player — matches the Rankings page exactly."""
    if not player_id:
        return None
    try:
        return _get_live_rank_map(db, league_categories).get(player_id)
    except Exception:
        return None


def _rank_to_value_tier(rank: Optional[int]) -> str:
    """Convert a rank to a descriptive tier string."""
    if rank is None:
        return "unranked"
    if rank <= 30:
        return "elite (top 30)"
    if rank <= 75:
        return "strong (top 75)"
    if rank <= 150:
        return "solid (top 150)"
    if rank <= 250:
        return "fringe (top 250)"
    return "deep/speculative"


def _compute_add_score(
    player_id: Optional[int],
    db: "Session",
    league_categories: list[str],
) -> float:
    """Score an add transaction 0–4.3 based on player's rank."""
    rank = _get_player_rank(db, player_id, league_categories)
    if rank is None:
        return 1.0  # D — unknown player
    if rank <= 20:
        return 4.3   # A+
    if rank <= 40:
        return 4.0   # A
    if rank <= 70:
        return 3.7   # A-
    if rank <= 100:
        return 3.3   # B+
    if rank <= 140:
        return 3.0   # B
    if rank <= 180:
        return 2.7   # B-
    if rank <= 220:
        return 2.3   # C+
    if rank <= 260:
        return 2.0   # C
    if rank <= 300:
        return 1.7   # C-
    if rank <= 350:
        return 1.3   # D+
    return 1.0       # D


def _compute_drop_score(
    player_id: Optional[int],
    db: "Session",
    league_categories: list[str],
) -> float:
    """Score a drop — dropping high-value players is a worse decision."""
    rank = _get_player_rank(db, player_id, league_categories)
    if rank is None:
        return 3.0  # B — dropping unknown player, probably fine
    if rank <= 50:
        return 0.0   # F — never drop a top 50 player
    if rank <= 100:
        return 0.7   # D-
    if rank <= 150:
        return 1.7   # C-
    if rank <= 200:
        return 2.3   # C+
    if rank <= 250:
        return 3.0   # B
    if rank <= 300:
        return 3.7   # A-
    return 4.0       # A — good to drop deep roster filler


def _compute_swap_score(
    add_ids: list[Optional[int]],
    drop_ids: list[Optional[int]],
    db: "Session",
    league_categories: list[str],
) -> float:
    """Score an add+drop swap based on net rank improvement.

    Unlike a blind average of independent add/drop scores, this measures how
    much better the acquired player is vs what was given up.  The result:
      - Large rank improvement (added player much better) → high grade
      - Neutral swap → C range
      - Downgrade (dropped player much better) → D/F range

    This is analogous to trade scoring — the manager is exchanging one asset
    for another and we want to know if the deal was worth it.
    """
    add_ranks = [_get_player_rank(db, pid, league_categories) for pid in add_ids]
    drop_ranks = [_get_player_rank(db, pid, league_categories) for pid in drop_ids]

    # Average ranks (lower = better player)
    add_avg = sum(r for r in add_ranks if r is not None) / max(len([r for r in add_ranks if r is not None]), 1)
    drop_avg = sum(r for r in drop_ranks if r is not None) / max(len([r for r in drop_ranks if r is not None]), 1)

    # Handle unknowns: treat unranked added player conservatively (rank 400),
    # unranked dropped player as fringe (rank 350) — we don't penalize dropping unknown.
    if not any(r is not None for r in add_ranks):
        add_avg = 400.0
    if not any(r is not None for r in drop_ranks):
        drop_avg = 350.0

    # delta = how many rank spots better the added player is vs dropped
    # positive delta = upgrade (added player has lower/better rank number)
    delta = drop_avg - add_avg

    # Also hard-cap: if drop is top-50 that's catastrophic regardless of what was added
    drop_top50 = any(r is not None and r <= 50 for r in drop_ranks)
    if drop_top50:
        return 0.0  # F

    if delta >= 150:  return 4.3  # A+ — massive upgrade
    if delta >= 100:  return 4.0  # A
    if delta >= 60:   return 3.7  # A-
    if delta >= 30:   return 3.3  # B+
    if delta >= 10:   return 3.0  # B  — meaningful upgrade
    if delta >= -10:  return 2.3  # C+ — roughly neutral swap
    if delta >= -30:  return 2.0  # C
    if delta >= -60:  return 1.7  # C-
    if delta >= -100: return 1.3  # D+ — downgrade
    if delta >= -150: return 1.0  # D
    return 0.7                    # D- — significant downgrade


def _is_keeper_league(league: Any) -> bool:
    """Return True if the league is a keeper/dynasty league.

    Uses the explicit is_keeper_league flag stored during Yahoo sync, which
    is derived from uses_roster_import in Yahoo's settings XML (Yahoo doesn't
    expose a dedicated keeper boolean — roster import is how keepers are
    designated before the draft, making it the definitive signal).
    Falls back to keepers_per_team > 0 for leagues synced before this field
    was added.
    """
    settings = getattr(league, "settings", None) or {}
    if settings.get("is_keeper_league"):
        return True
    if settings.get("keepers_per_team", 0) > 0:
        return True
    return False


def _pick_round_to_str(round_val: Any) -> str:
    """Convert a pick round value (int or string) to a label for evaluate_trade."""
    try:
        n = int(round_val)
        return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")
    except (TypeError, ValueError):
        return str(round_val)


def _eval_diff_to_grade_score(adj_diff: float) -> float:
    """Map evaluate_trade value_differential to 0–4.3 grade scale.

    Calibrated against typical PlayerRanking.score values (~0–6 range)
    and the DRAFT_PICK_VALUES constants used by the evaluator.
    """
    if adj_diff >= 5.0:  return 4.3   # A+
    if adj_diff >= 3.0:  return 4.0   # A
    if adj_diff >= 1.5:  return 3.7   # A-
    if adj_diff >= 0.5:  return 3.3   # B+
    if adj_diff >= -0.5: return 3.0   # B  — roughly even
    if adj_diff >= -1.5: return 2.7   # B-
    if adj_diff >= -3.0: return 2.3   # C+
    if adj_diff >= -5.0: return 1.7   # C-
    if adj_diff >= -7.0: return 1.0   # D
    return 0.0                        # F


def _compute_trade_score(
    participants: list[dict],
    db: "Session",
    league_categories: list[str],
    league: Any = None,
    player_ages: Optional[dict] = None,
) -> tuple[float, float]:
    """Score both sides of a trade using the trade_evaluator engine.

    Delegates to evaluate_trade() which applies:
      - Talent-density adjustment (star concentration bonus)
      - Keeper-league age multipliers when applicable
      - Draft pick values (2nd round > 3rd round > later)

    Falls back to a simple rank-delta if the evaluator fails.
    """
    if len(participants) < 2:
        return 2.0, 2.0

    try:
        from fantasai.brain.trade_evaluator import evaluate_trade, TradeContext
        from fantasai.api.v1.recommendations import _compute_rankings

        has_keepers = _is_keeper_league(league) if league else False

        with db.no_autoflush:
            lookback, predictive = _compute_rankings(db, league_categories)

        pred_map = {r.player_id: r for r in predictive}
        lb_map   = {r.player_id: r for r in lookback}

        def _ranking(pid: Optional[int]):
            if not pid:
                return None
            return pred_map.get(pid) or lb_map.get(pid)

        def _picks_for(side: dict, key: str) -> list[str]:
            return [_pick_round_to_str(pk.get("round")) for pk in side.get(key, [])]

        def _score_side(this: dict) -> float:
            giving    = [r for p in this.get("players_dropped", []) if (r := _ranking(p.get("player_id")))]
            receiving = [r for p in this.get("players_added",   []) if (r := _ranking(p.get("player_id")))]
            ctx = TradeContext(
                giving_rankings=giving,
                receiving_rankings=receiving,
                giving_picks=_picks_for(this, "picks_dropped"),
                receiving_picks=_picks_for(this, "picks_added"),
                team_strengths={},
                scoring_categories=league_categories,
                has_keepers=has_keepers,
                player_ages=player_ages or {},
            )
            result = evaluate_trade(ctx)
            return _eval_diff_to_grade_score(result.value_differential)

        return _score_side(participants[0]), _score_side(participants[1])

    except Exception:
        _log.debug("_compute_trade_score: evaluate_trade failed, using rank-delta fallback", exc_info=True)

    # ── Fallback: simple average-rank delta ──────────────────────────────────
    def _side_score_fallback(side: dict) -> float:
        gained = side.get("players_added", [])
        lost   = side.get("players_dropped", [])
        if not gained and not lost:
            return 2.0
        gain_ranks = [_get_player_rank(db, p.get("player_id"), league_categories) for p in gained]
        loss_ranks = [_get_player_rank(db, p.get("player_id"), league_categories) for p in lost]
        avg_gain = (sum(r for r in gain_ranks if r) / max(1, sum(1 for r in gain_ranks if r))) if any(gain_ranks) else 300
        avg_loss = (sum(r for r in loss_ranks if r) / max(1, sum(1 for r in loss_ranks if r))) if any(loss_ranks) else 300
        delta = avg_loss - avg_gain
        if delta >= 100: return 4.3
        if delta >= 60:  return 4.0
        if delta >= 30:  return 3.7
        if delta >= 10:  return 3.3
        if delta >= -10: return 3.0
        if delta >= -30: return 2.3
        if delta >= -60: return 1.7
        if delta >= -100: return 1.0
        return 0.0

    return _side_score_fallback(participants[0]), _side_score_fallback(participants[1])


def _get_player_facts(db: "Session", player_id: Optional[int], player_name: str) -> str:
    """Return a verified-facts string for a player from our live DB.

    Includes team, positions, injury status, and current-season stats with
    clear labels indicating whether stats are real 2026 actuals (with sample
    size) or Steamer full-season projections.  This prevents Claude from
    quoting projected counting stats as if they are current performance.
    """
    if not player_id:
        return (
            f"{player_name}: (no DB record — this player may be a prospect or international "
            f"signing not yet in our system; evaluate based on what you know about their "
            f"prospect status and likely 2026 MLB timeline)"
        )
    try:
        from fantasai.models.player import Player, PlayerStats
        player = db.get(Player, player_id)
        if not player:
            return (
                f"{player_name}: (no DB record — this player may be a prospect or international "
                f"signing not yet in our system; evaluate based on what you know about their "
                f"prospect status and likely 2026 MLB timeline)"
            )

        parts: list[str] = []
        if player.team:
            parts.append(f"team={player.team}")
        if player.positions:
            parts.append(f"positions={'/'.join(player.positions[:3])}")
        if player.status and player.status.upper() not in ("", "ACTIVE", "ACT"):
            parts.append(f"status={player.status}")

        # Fetch 2026 stats rows (actual preferred; fall back to projection)
        stats_rows = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == player_id,
                PlayerStats.season == 2026,
                PlayerStats.week.is_(None),
            )
            .all()
        )
        # Determine player's primary stat type from positions so we pick the
        # right actual row.  Pitchers (SP/RP) should use the pitching row;
        # everyone else uses the batting row.  This matters because the stats
        # pipeline may ingest a spurious batting row for pitchers (FanGraphs
        # includes pitchers in batting tables) which would otherwise win the
        # next() race and report zero IP.
        _pitcher_positions = {"SP", "RP", "P"}
        _is_pitcher = bool(player.positions and any(
            p.upper() in _pitcher_positions for p in player.positions
        ))
        _primary_stat_type = "pitching" if _is_pitcher else "batting"

        actual_rows = [r for r in stats_rows if r.data_source == "actual"]
        # Prefer the row matching the player's primary stat type; within that
        # prefer rows with non-empty counting stats over empty/null ones.
        def _actual_sort_key(r):
            type_match = 0 if r.stat_type == _primary_stat_type else 1
            has_stats = 0 if any(v is not None and v != 0 for v in (r.counting_stats or {}).values()) else 1
            return (type_match, has_stats)
        actual_rows.sort(key=_actual_sort_key)
        actual_row = actual_rows[0] if actual_rows else None

        proj_row = next((r for r in stats_rows if r.data_source == "projection" and r.stat_type == _primary_stat_type), None)
        stats_row = actual_row or proj_row
        is_actual = stats_row is not None and stats_row.data_source == "actual"
        is_proj = stats_row is not None and stats_row.data_source == "projection"

        if stats_row:
            rate = stats_row.rate_stats or {}
            adv = stats_row.advanced_stats or {}
            counting = stats_row.counting_stats or {}

            if stats_row.stat_type == "pitching":
                # Sample size context for actuals
                ip_actual = float(counting.get("IP", 0) or 0) if is_actual else 0.0
                gs_actual = int(float(counting.get("GS", 0) or 0)) if is_actual else 0

                if is_actual:
                    stat_label = f"[2026 actual — {gs_actual} GS, {ip_actual:.1f} IP]"
                else:
                    stat_label = "[2026 Steamer projection — full-season]"
                parts.append(stat_label)

                for k in ["ERA", "WHIP", "K/9", "K9"]:
                    v = rate.get(k)
                    if v is not None:
                        try:
                            parts.append(f"{k}={float(v):.2f}")
                        except (TypeError, ValueError):
                            pass
                for k in ["xERA", "xFIP", "SIERA"]:
                    v = adv.get(k)
                    if v is not None:
                        try:
                            parts.append(f"{k}={float(v):.2f}")
                        except (TypeError, ValueError):
                            pass
                # Only include projected counting stats when labelled as projection
                if is_proj:
                    for k in ["W", "SV", "K"]:
                        v = counting.get(k)
                        if v is not None:
                            try:
                                parts.append(f"proj-{k}={int(float(v))}")
                            except (TypeError, ValueError):
                                pass
                elif is_actual:
                    for k in ["SV", "K"]:
                        v = counting.get(k)
                        if v is not None:
                            try:
                                parts.append(f"{k}={int(float(v))}")
                            except (TypeError, ValueError):
                                pass
            else:
                # Sample size context for actuals
                pa_actual = int(float(counting.get("PA", 0) or counting.get("AB", 0) or 0)) if is_actual else 0
                g_actual = int(float(counting.get("G", 0) or 0)) if is_actual else 0

                if is_actual:
                    stat_label = f"[2026 actual — {g_actual} G, {pa_actual} PA]"
                else:
                    stat_label = "[2026 Steamer projection — full-season]"
                parts.append(stat_label)

                for k in ["AVG", "OBP", "SLG"]:
                    v = rate.get(k)
                    if v is not None:
                        try:
                            parts.append(f"{k}={float(v):.3f}")
                        except (TypeError, ValueError):
                            pass
                # Counting stats: label projected ones clearly
                # H is included so the LLM has hits context and can't hallucinate "0-for-X" slumps
                for k in ["H", "HR", "SB", "R", "RBI"]:
                    v = counting.get(k)
                    if v is not None:
                        try:
                            prefix = "proj-" if is_proj else ""
                            parts.append(f"{prefix}{k}={int(float(v))}")
                        except (TypeError, ValueError):
                            pass

        return f"{player_name}: {', '.join(parts)}" if parts else player_name
    except Exception:
        return player_name


def _league_format_str(league: "League") -> str:
    """Return a human-readable league format string."""
    lt = (league.league_type or "").lower()
    if "h2h" in lt or "head" in lt:
        return "H2H categories"
    if "roto" in lt:
        return "rotisserie"
    if "point" in lt:
        return "points"
    return "H2H categories"  # safe default


# ---------------------------------------------------------------------------
# Streaming detection
# ---------------------------------------------------------------------------

def _detect_stream(
    txn: "Transaction",
    db: "Session",
) -> Optional[dict]:
    """Return streaming context dict if any added player has a start today or tomorrow.

    For pitchers: checks whether the added pitcher is the probable SP in today's or
    tomorrow's game (requires mlbam_id match in schedule data).
    For batters: checks whether the added batter's team has a game today AND the
    team's category needs suggest the batter can help.

    Returns None if no stream is detected, or a dict with keys:
        is_stream: True
        player_id: int
        player_name: str
        stat_type: "pitching" | "batting"
        start_date: "today" | "tomorrow"
        opponent: str | None
        is_home: bool | None
        park: str | None
        park_factor: float | None
        today_sp_name: str | None        # opposing SP (batters only)
        today_sp_throws: str | None      # opposing SP handedness (batters only)
        weather_hr_factor: float
        vegas_run_factor: float
        week_context_text: str | None
    """
    from datetime import date, timedelta
    from fantasai.models.player import Player

    participants = txn.participants or []
    adds = [p for p in participants if p.get("action") == "add"]
    if not adds:
        return None

    today = date.today()
    tomorrow = today + timedelta(days=1)
    today_iso = today.isoformat()
    tomorrow_iso = tomorrow.isoformat()

    # Try to get the week schedule from cache (warm) or skip (non-fatal)
    try:
        from fantasai.api.v1.recommendations import get_cached_week_schedule
        week_sched = get_cached_week_schedule()
    except Exception:
        week_sched = {}

    try:
        from fantasai.engine.schedule import PARK_FACTORS
    except Exception:
        PARK_FACTORS = {}

    for p in adds:
        player_id = p.get("player_id")
        player_name = p.get("player_name", "?")
        if not player_id:
            continue

        player = db.get(Player, player_id)
        if not player:
            continue

        positions = player.positions or []
        is_pitcher = any(pos in ("SP", "RP", "P") for pos in positions)
        mlbam_id = player.mlbam_id

        ps = week_sched.get(player_id)
        if ps is None:
            continue

        game_log = ps.batter_game_log or []

        if is_pitcher and mlbam_id:
            # SP stream: pitcher must be the probable starter today or tomorrow
            for check_date, label in [(today_iso, "today"), (tomorrow_iso, "tomorrow")]:
                entry = next(
                    (g for g in game_log
                     if g.get("date") == check_date and g.get("sp_mlbam_id") == mlbam_id),
                    None,
                )
                if entry:
                    opponent = entry.get("opponent_abbr")
                    is_home = entry.get("is_home", True)
                    park = (ps.home_park or opponent) if is_home else opponent
                    park_factor = PARK_FACTORS.get(park, 1.0) if park else 1.0
                    try:
                        from fantasai.engine.schedule import build_player_week_context
                        week_ctx = build_player_week_context(ps, "pitching", positions)
                    except Exception:
                        week_ctx = None
                    return {
                        "is_stream": True,
                        "player_id": player_id,
                        "player_name": player_name,
                        "stat_type": "pitching",
                        "start_date": label,
                        "opponent": opponent,
                        "is_home": is_home,
                        "park": park,
                        "park_factor": park_factor,
                        "today_sp_name": None,
                        "today_sp_throws": None,
                        "weather_hr_factor": ps.weather_hr_factor,
                        "vegas_run_factor": ps.vegas_run_factor,
                        "week_context_text": week_ctx,
                    }

        elif not is_pitcher:
            # Batter stream: team has a game today
            entry = next((g for g in game_log if g.get("date") == today_iso), None)
            if entry:
                opponent = entry.get("opponent_abbr")
                is_home = entry.get("is_home", True)
                park = (ps.home_park or opponent) if is_home else opponent
                park_factor = PARK_FACTORS.get(park, 1.0) if park else 1.0
                # Look up opposing SP for batter matchup
                today_sp_name = None
                today_sp_throws = None
                sp_mlbam = entry.get("sp_mlbam_id")
                if sp_mlbam:
                    try:
                        sp_player = db.query(Player).filter(Player.mlbam_id == sp_mlbam).first()
                        if sp_player:
                            today_sp_name = sp_player.name
                            today_sp_throws = sp_player.throws
                    except Exception:
                        pass
                try:
                    from fantasai.engine.schedule import build_player_week_context
                    week_ctx = build_player_week_context(ps, "batting", positions)
                except Exception:
                    week_ctx = None
                return {
                    "is_stream": True,
                    "player_id": player_id,
                    "player_name": player_name,
                    "stat_type": "batting",
                    "start_date": "today",
                    "opponent": opponent,
                    "is_home": is_home,
                    "park": park,
                    "park_factor": park_factor,
                    "today_sp_name": today_sp_name,
                    "today_sp_throws": today_sp_throws,
                    "weather_hr_factor": ps.weather_hr_factor,
                    "vegas_run_factor": ps.vegas_run_factor,
                    "week_context_text": week_ctx,
                }

    return None


def _get_matchup_context(
    txn: "Transaction",
    league: "League",
    db: "Session",
) -> Optional[str]:
    """Return a formatted string describing the transacting team's current H2H position.

    Finds the most recent MatchupAnalysis for the team making the transaction
    and returns a compact summary of which categories they're winning/losing,
    so the LLM can assess whether a stream helps in the right spots.

    Returns None if no matchup data is available or the league is not H2H.
    """
    lt = (league.league_type or "").lower()
    if "roto" in lt or "point" in lt:
        return None  # H2H only

    try:
        from fantasai.models.matchup import MatchupAnalysis
        from fantasai.engine.schedule import get_current_week_bounds
        from datetime import date

        # Find which team key belongs to the transacting manager
        participants = txn.participants or []
        team_key: Optional[str] = None
        team_name: Optional[str] = None
        for p in participants:
            if p.get("action") in ("add", "drop") or txn.transaction_type == "trade":
                team_key = p.get("team_key") or p.get("manager_team_key")
                team_name = p.get("manager_name")
                if team_key:
                    break
        if not team_key:
            return None

        # Get current week number
        _, week_end = get_current_week_bounds()
        # Approximate week number: days since April 1 / 7
        days_since_start = (date.today() - date(2026, 3, 30)).days
        current_week = max(1, days_since_start // 7 + 1)

        # Find the matchup that includes this team
        matchup = (
            db.query(MatchupAnalysis)
            .filter(
                MatchupAnalysis.league_id == league.league_id,
                MatchupAnalysis.season == 2026,
                MatchupAnalysis.week == current_week,
            )
            .filter(
                (MatchupAnalysis.team1_key == team_key) |
                (MatchupAnalysis.team2_key == team_key)
            )
            .first()
        )
        if not matchup:
            return None

        is_team1 = matchup.team1_key == team_key
        my_key = "team1" if is_team1 else "team2"
        opp_key = "team2" if is_team1 else "team1"
        opp_name = matchup.team2_name if is_team1 else matchup.team1_name

        # Prefer live stats (mid-week actuals); fall back to projections
        stats = matchup.live_stats or matchup.category_projections or {}
        if not stats:
            return None

        winning: list[str] = []
        losing: list[str] = []
        close: list[str] = []  # within ~5% or tied

        for cat, vals in stats.items():
            if not isinstance(vals, dict):
                continue
            my_val = vals.get(my_key)
            opp_val = vals.get(opp_key)
            if my_val is None or opp_val is None:
                continue
            try:
                my_f = float(my_val)
                opp_f = float(opp_val)
            except (TypeError, ValueError):
                continue

            # For rate stats (AVG, ERA, WHIP, OBP), small absolute diff is still meaningful
            avg_abs = (abs(my_f) + abs(opp_f)) / 2
            pct_diff = abs(my_f - opp_f) / max(avg_abs, 0.001)

            # Determine if "higher is better" — ERA and WHIP invert this
            lower_is_better = cat.upper() in ("ERA", "WHIP", "BB", "BB9", "BB/9")
            my_winning = (my_f > opp_f) if not lower_is_better else (my_f < opp_f)

            if pct_diff < 0.05:  # within 5% — effectively tied
                close.append(cat)
            elif my_winning:
                winning.append(cat)
            else:
                losing.append(cat)

        parts = [f"H2H matchup context (Week {current_week} vs {opp_name}):"]
        if winning:
            parts.append(f"  Winning: {', '.join(winning)}")
        if losing:
            parts.append(f"  Losing / need help: {', '.join(losing)}")
        if close:
            parts.append(f"  Too close to call: {', '.join(close)}")
        parts.append(
            "  (streaming SP most directly impacts: W, IP, K — "
            "streaming batter most directly impacts counting stats in their sport)"
        )
        return "\n".join(parts)

    except Exception:
        _log.debug("_get_matchup_context failed (non-fatal)", exc_info=True)
        return None


def _get_team_strength_summary(team_key: str, categories: list[str], db: "Session") -> str:
    """Return a short string describing a team's category strengths and weaknesses.

    Uses lookback rankings for the team's current roster to compute category scores,
    then returns the top 2 strong and bottom 2 weak categories for prompt injection.
    Returns an empty string on failure (non-fatal).
    """
    try:
        from fantasai.api.v1.recommendations import _compute_rankings
        from fantasai.brain.recommender import _compute_team_strengths
        from fantasai.models.league import Team

        # Use a savepoint so any SQL error (e.g. type mismatch) only rolls back
        # this nested block and does NOT abort the outer transaction.
        with db.begin_nested():
            with db.no_autoflush:
                # team_key is a Yahoo string key (e.g. "469.l.60209.t.11") —
                # must query yahoo_team_key, not the integer team_id PK.
                team = db.query(Team).filter(Team.yahoo_team_key == team_key).first()
                if not team or not team.roster:
                    return ""
                lookback, _ = _compute_rankings(db, categories or [])

        lb_map = {r.player_id: r for r in lookback}
        roster_rankings = [lb_map[pid] for pid in (team.roster or []) if pid in lb_map]
        if not roster_rankings:
            return ""

        strengths = _compute_team_strengths(roster_rankings, categories)
        sorted_cats = sorted(strengths.items(), key=lambda x: x[1], reverse=True)
        strong = [c for c, _ in sorted_cats[:2]]
        weak = [c for c, _ in sorted_cats[-2:]]
        parts = []
        if strong:
            parts.append(f"strong: {', '.join(strong)}")
        if weak:
            parts.append(f"weak: {', '.join(weak)}")
        return "; ".join(parts)
    except Exception:
        _log.debug("_get_team_strength_summary: failed for %s", team_key, exc_info=True)
        return ""


def _build_trade_side_prompt(
    side_idx: int,
    participants: list[dict],
    data_block: str,
    stat_instructions: str,
    early_season_note: str,
    categories: list[str],
    has_keepers: bool,
    player_ages: dict[int, int],
    db: "Session",
) -> str:
    """Build a per-side trade rationale prompt from the perspective of side `side_idx`."""
    if len(participants) < 2:
        return ""

    this_side = participants[side_idx]
    other_side = participants[1 - side_idx]

    this_mgr = this_side.get("manager_name", f"Team {side_idx + 1}")
    this_grade = this_side.get("_grade_letter", "?")
    this_team_key = this_side.get("team_key", "")

    receives_players = ", ".join(p.get("player_name", "?") for p in this_side.get("players_added", []))
    gives_up_players = ", ".join(p.get("player_name", "?") for p in this_side.get("players_dropped", []))

    # Format draft picks received / given up for this side
    def _pick_label(pick: dict) -> str:
        rnd = pick.get("round", "?")
        orig = pick.get("original_team_name", "")
        return f"Round {rnd} pick" + (f" (originally {orig})" if orig else "")

    picks_added = this_side.get("picks_added", [])
    picks_dropped = this_side.get("picks_dropped", [])
    receives_picks = ", ".join(_pick_label(pk) for pk in picks_added)
    gives_up_picks = ", ".join(_pick_label(pk) for pk in picks_dropped)

    receives = ", ".join(filter(None, [receives_players, receives_picks])) or "(nothing)"
    gives_up = ", ".join(filter(None, [gives_up_players, gives_up_picks])) or "(nothing)"

    # Team strengths/weaknesses context
    strength_summary = ""
    if this_team_key:
        s = _get_team_strength_summary(this_team_key, categories, db)
        if s:
            strength_summary = f"\n{this_mgr}'s team categories — {s}."

    # Keeper-league age + pick context
    keeper_note = ""
    if has_keepers:
        keeper_parts = []
        if player_ages:
            age_parts = []
            for p in this_side.get("players_added", []) + this_side.get("players_dropped", []):
                pid = p.get("player_id")
                if pid and pid in player_ages:
                    age_parts.append(f"{p.get('player_name', '?')} age {player_ages[pid]}")
            if age_parts:
                keeper_parts.append(f"player ages: {', '.join(age_parts)}")
        if picks_added or picks_dropped:
            pick_desc_parts = []
            if picks_added:
                pick_desc_parts.append(f"gains {receives_picks}")
            if picks_dropped:
                pick_desc_parts.append(f"gives up {gives_up_picks}")
            keeper_parts.append("draft picks: " + "; ".join(pick_desc_parts))
        if keeper_parts:
            keeper_note = (
                f"\nKEEPER LEAGUE: Factor in future value — {'; '.join(keeper_parts)}. "
                "Younger players (≤25) and early-round picks carry more dynasty weight."
            )

    txn_desc = (
        f"TRADE — {this_mgr}'s perspective:\n"
        f"  {this_mgr} RECEIVES: {receives}\n"
        f"  {this_mgr} GIVES UP: {gives_up}\n"
        f"GRADE FOR {this_mgr.upper()}: {this_grade}"
    )

    pick_instruction = (
        " If draft picks are included, factor in round value — earlier rounds are significant "
        "dynasty capital, especially in keeper leagues."
        if (picks_added or picks_dropped) else ""
    )

    return (
        f"{data_block}{early_season_note}"
        f"{strength_summary}{keeper_note}\n\n"
        f"{txn_desc}\n\n"
        f"{stat_instructions}\n\n"
        f"Write a 2-sentence verdict on whether {this_mgr} won or lost this trade. "
        f"Be specific about what they gain and give up, including any draft picks.{pick_instruction} "
        f"Use ONLY the player data above — never cite stats or injuries not listed. "
        f"Direct, opinionated, no hedging."
    )


def _build_txn_shared_context(
    txn: "Transaction",
    league: "League",
    db: "Session",
) -> tuple[str, str, str]:
    """Return (data_block, stat_instructions, early_season_note) for any transaction.

    Extracted so the trade grader can reuse these when building per-side prompts
    without calling _build_prompt twice.
    """
    categories = league.scoring_categories or []
    cat_str = ", ".join(str(c) for c in categories[:8]) if categories else "H/AB, R, HR, RBI, SB, AVG, OPS, IP"
    league_format = _league_format_str(league)

    txn_type = txn.transaction_type
    participants = txn.participants or []

    data_block_lines: list[str] = [
        f"LEAGUE FORMAT: {league_format}",
        f"SCORING CATEGORIES: {cat_str}",
        "PLAYER DATA (live DB — authoritative; ignore any conflicting training knowledge):",
    ]

    # Build a map of player_id → rank_at_move.
    # Prefer stored historical rank (set at original grading time) so the rank
    # shown in a blurb never silently changes if the player is re-graded later.
    # Fall back to live lookup (first grading, or participant has no stored rank).
    stored_rank_map: dict[int, int] = {}
    if txn_type in ("add", "drop"):
        for p in participants:
            pid = p.get("player_id")
            stored = p.get("_ros_rank_at_grade")
            if pid and stored is not None:
                stored_rank_map[pid] = stored
    else:
        for side in participants:
            for p in side.get("players_added", []) + side.get("players_dropped", []):
                pid = p.get("player_id")
                stored = p.get("_ros_rank_at_grade")
                if pid and stored is not None:
                    stored_rank_map[pid] = stored

    players_to_lookup: list[tuple[Optional[int], str]] = []
    if txn_type in ("add", "drop"):
        for p in participants:
            players_to_lookup.append((p.get("player_id"), p.get("player_name", "?")))
    else:
        for side in participants:
            for p in side.get("players_added", []) + side.get("players_dropped", []):
                players_to_lookup.append((p.get("player_id"), p.get("player_name", "?")))

    seen_ids: set = set()
    for pid, pname in players_to_lookup:
        key = pid or pname
        if key in seen_ids:
            continue
        seen_ids.add(key)
        rank = stored_rank_map.get(pid) if pid else None
        if rank is None:
            rank = _get_player_rank(db, pid, categories)
        rank_str = f" | ROS-rank-at-move=#{rank}" if rank else ""
        facts = _get_player_facts(db, pid, pname)
        data_block_lines.append(f"  - {facts}{rank_str}")

    # Include draft picks for trade transactions (keeper leagues).
    if txn_type == "trade":
        all_picks: list[str] = []
        for side in participants:
            team_name = side.get("team_name", side.get("team_key", "?"))
            for pick in side.get("picks_added", []):
                orig = pick.get("original_team_name", "")
                round_label = f"Round {pick.get('round', '?')}"
                owner_note = f" (originally {orig})" if orig and orig != team_name else ""
                all_picks.append(f"  - {team_name} RECEIVES: {round_label} draft pick{owner_note}")
        if all_picks:
            data_block_lines.append("DRAFT PICKS INCLUDED IN TRADE:")
            data_block_lines.extend(all_picks)

    data_block = "\n".join(data_block_lines)

    from datetime import date as _date
    _season_start = _date(2026, 3, 25)
    _days_in = (_date.today() - _season_start).days
    _early_season = _days_in < 28
    early_season_note = (
        "\nEARLY SEASON CONTEXT: The 2026 season just started. Stats labeled "
        "'2026 actual' have very small samples — flag this and blend in the "
        "Steamer projection context where useful. Stats labeled 'Steamer projection' "
        "are full-season projections, not current-year accumulations."
        if _early_season else ""
    )

    stat_instructions = (
        "When referencing stats:\n"
        "- Stats labeled '[2026 actual — N G/PA/IP]' are real 2026 performance; "
        "mention the sample size if small (under 50 PA or 5 GS).\n"
        "- Stats labeled '[2026 Steamer projection]' are full-season projections; "
        "say 'projects for X' or 'Steamer projects', never 'has X'.\n"
        "- 'proj-HR', 'proj-K', etc. are projected season totals, not current stats.\n"
        "- 'ROS-rank-at-move' is the player's Rest-of-Season predictive rank AT THE TIME this move happened — "
        "a historical snapshot, not the current rank. Say 'ranked #N at the time of this move' or "
        "'was ranked #N when this trade happened'. Never say 'currently ranked' or 'is ranked'.\n"
        "K/9 benchmarks for starters: elite=10.0+, above avg=9.0-9.9, avg=8.0-8.9, "
        "below avg=7.0-7.9, poor=<7.0. Do not call anything below 9.0 'elite'.\n"
        "POSITIONS ARE AUTHORITATIVE: A player's eligible position(s) are listed "
        "in the data block as 'positions='. Use ONLY those — never infer, assume, "
        "or recall a position from training knowledge. If a player is listed as "
        "'positions=2B/OF', call them a 2B or outfielder, never a shortstop or "
        "catcher regardless of what you know about their history."
    )

    return data_block, stat_instructions, early_season_note


def _build_prompt(
    txn: "Transaction",
    league: "League",
    db: "Session",
    stream_ctx: Optional[dict] = None,
    matchup_ctx: Optional[str] = None,
) -> str:
    """Build a Claude prompt for the move grade rationale."""
    categories = league.scoring_categories or []

    txn_type = txn.transaction_type
    participants = txn.participants or []

    data_block, stat_instructions, early_season_note = _build_txn_shared_context(txn, league, db)

    # ── Stream context block ─────────────────────────────────────────────────
    stream_block = ""
    if stream_ctx and stream_ctx.get("is_stream"):
        sc = stream_ctx
        home_away = "vs" if sc.get("is_home") else "@"
        opponent = sc.get("opponent") or "?"
        park = sc.get("park")
        park_factor = sc.get("park_factor")
        weather = sc.get("weather_hr_factor", 1.0)
        vegas = sc.get("vegas_run_factor", 1.0)
        when = sc.get("start_date", "today")
        stat_type = sc.get("stat_type", "pitching")

        env_parts = []
        if park_factor is not None and abs(park_factor - 1.0) >= 0.05:
            pct = int(round((park_factor - 1.0) * 100))
            sign = "+" if pct > 0 else ""
            env_parts.append(f"park factor {sign}{pct}% HR ({park})")
        if abs(vegas - 1.0) >= 0.05:
            implied = round(4.4 * vegas, 1)
            sign = "+" if vegas > 1 else ""
            env_parts.append(f"Vegas {implied} R/G implied ({sign}{int(round((vegas-1)*100))}% vs avg)")
        if abs(weather - 1.0) >= 0.05:
            sign = "+" if weather > 1 else ""
            env_parts.append(f"weather {sign}{int(round((weather-1)*100))}% HR environment")
        if stat_type == "batting":
            sp = sc.get("today_sp_name")
            hand = sc.get("today_sp_throws")
            if sp:
                env_parts.append(f"vs {sp}{' (' + hand + ')' if hand else ''}")
        env_str = f" | {', '.join(env_parts)}" if env_parts else ""

        stream_block = (
            f"\nSTREAM DETECTION: This appears to be a streaming pickup — "
            f"{sc.get('player_name')} has a {'start' if stat_type == 'pitching' else 'game'} "
            f"{when} ({home_away} {opponent}{env_str}).\n"
        )
        if sc.get("week_context_text"):
            stream_block += f"Week context: {sc['week_context_text']}\n"

    # ── Matchup context block ────────────────────────────────────────────────
    matchup_block = f"\n{matchup_ctx}\n" if matchup_ctx else ""

    # ── Per-type prompt bodies ────────────────────────────────────────────────
    if txn_type == "add":
        adds = [p for p in participants if p.get("action") == "add"]
        drops = [p for p in participants if p.get("action") == "drop"]
        manager = adds[0].get("manager_name", "A manager") if adds else "A manager"
        added_names = ", ".join(p.get("player_name", "?") for p in adds)

        # ── Stream verdict instructions ──────────────────────────────────────
        if stream_ctx and stream_ctx.get("is_stream"):
            is_sp_stream = stream_ctx.get("stat_type") == "pitching"
            start_label = "today's start" if is_sp_stream else "today's game"
            cite_label = "matchup, park factor, and Vegas environment" if is_sp_stream else "matchup and relevant categories"
            verdict_instructions = (
                f"This is a STREAMING pickup. Write 3 sentences structured as:\n"
                f"SHORT-TERM: Is this a reasonable stream for {start_label}? "
                f"Cite the {cite_label}. "
                f"One sentence — verdict first.\n"
                f"LONG-TERM: Is {added_names} a keeper or a drop-after-today arm? "
                f"Use the ROS ranking and underlying stats. One sentence.\n"
                f"MATCHUP: If H2H matchup context is provided, one sentence on whether this stream "
                f"specifically helps the categories where the team is losing — or wastes a roster move."
            )
        elif drops:
            drop_names = ", ".join(p.get("player_name", "?") for p in drops)

            def _positions_for(pid: Optional[int], pname: str) -> list[str]:
                if not pid:
                    return []
                from fantasai.models.player import Player as _Player
                row = db.query(_Player.positions).filter(_Player.player_id == pid).first()
                return row[0] if row and row[0] else []

            add_positions = [pos for p in adds for pos in _positions_for(p.get("player_id"), p.get("player_name", ""))]
            drop_positions = [pos for p in drops for pos in _positions_for(p.get("player_id"), p.get("player_name", ""))]
            shared_pos = set(add_positions) & set(drop_positions)

            if shared_pos:
                swap_context = f"SWAP CONTEXT: Same-position upgrade — both players share eligibility at {'/'.join(sorted(shared_pos))}."
            else:
                add_pos_str = "/".join(sorted(set(add_positions))) or "unknown"
                drop_pos_str = "/".join(sorted(set(drop_positions))) or "unknown"
                swap_context = (
                    f"SWAP CONTEXT: Roster rebalancing — adding {add_pos_str} depth, "
                    f"dropping {drop_pos_str} depth. Consider whether this addresses a team need."
                )
            verdict_instructions = (
                f"{swap_context}\n\n"
                f"Write a 2-sentence verdict on this roster swap. "
                f"Judge whether {added_names} is worth more than {drop_names} for the rest of the season, "
                f"and whether the positional trade-off makes sense for this team."
            )
        else:
            verdict_instructions = (
                f"Write a 2-sentence verdict on this add."
            )

        if drops and stream_ctx and stream_ctx.get("is_stream"):
            drop_names = ", ".join(p.get("player_name", "?") for p in drops)
            txn_line = f"TRANSACTION: {manager} drops {drop_names} to add {added_names} (streaming pickup)"
        elif stream_ctx and stream_ctx.get("is_stream"):
            txn_line = f"TRANSACTION: {manager} adds {added_names} (streaming pickup)"
        elif drops:
            drop_names = ", ".join(p.get("player_name", "?") for p in drops)
            txn_line = f"TRANSACTION: {manager} drops {drop_names} to add {added_names}"
        else:
            txn_line = f"TRANSACTION: {manager} adds {added_names}"

        prompt = (
            f"{data_block}{early_season_note}"
            f"{stream_block}{matchup_block}\n"
            f"{txn_line}\n"
            f"GRADE: {txn.grade_letter}\n\n"
            f"{stat_instructions}\n\n"
            f"{verdict_instructions}\n"
            f"Use ONLY the player data above — never cite team, stats, or injuries "
            f"not listed there. Direct, specific, no hedging."
        )

    elif txn_type == "drop":
        manager = participants[0].get("manager_name", "A manager") if participants else "A manager"
        drop_names = ", ".join(p.get("player_name", "?") for p in participants)

        prompt = (
            f"{data_block}{early_season_note}"
            f"{matchup_block}\n"
            f"TRANSACTION: {manager} drops {drop_names}\n"
            f"GRADE: {txn.grade_letter}\n\n"
            f"{stat_instructions}\n\n"
            f"Write a 2-sentence verdict on this drop. "
            f"Use ONLY the player data above — never cite team, stats, or injuries "
            f"not listed there. Direct, specific, no hedging."
        )

    else:  # trade
        side_lines: list[str] = []
        for side in participants:
            added = ", ".join(p.get("player_name", "?") for p in side.get("players_added", []))
            dropped = ", ".join(p.get("player_name", "?") for p in side.get("players_dropped", []))
            mgr = side.get("manager_name", "Manager")
            side_lines.append(f"{mgr} receives: {added} | gives up: {dropped}")

        prompt = (
            f"{data_block}{early_season_note}"
            f"{matchup_block}\n"
            f"TRANSACTION (trade):\n" + "\n".join(side_lines) + "\n"
            f"OVERALL GRADE: {txn.grade_letter}\n\n"
            f"{stat_instructions}\n\n"
            f"Write a 2-sentence verdict identifying who won this trade and why. "
            f"Use ONLY the player data above — never cite team, stats, or injuries "
            f"not listed there. Direct, specific, no hedging."
        )

    return prompt


def grade_transaction(
    db: "Session",
    txn: "Transaction",
    league: "League",
) -> None:
    """Grade a transaction in-place: sets grade_letter, grade_score, grade_rationale, graded_at.

    Does NOT commit — caller is responsible for db.commit().
    """
    from fantasai.config import settings

    categories: list[str] = []
    if league.scoring_categories:
        categories = [
            c.get("display_name", c.get("name", "")) if isinstance(c, dict) else str(c)
            for c in league.scoring_categories
        ]

    participants = txn.participants or []
    txn_type = txn.transaction_type

    # ── Detect streaming pickup ────────────────────────────────────────────────
    stream_ctx: Optional[dict] = None
    matchup_ctx: Optional[str] = None
    if txn_type == "add":
        stream_ctx = _detect_stream(txn, db)
        matchup_ctx = _get_matchup_context(txn, league, db)

    # Compute grade score
    if txn_type == "add":
        adds = [p for p in participants if p.get("action") == "add"]
        drops = [p for p in participants if p.get("action") == "drop"]
        if drops:
            # Add+drop swap: score as net upgrade, not a blind average
            grade_score = _compute_swap_score(
                [p.get("player_id") for p in adds],
                [p.get("player_id") for p in drops],
                db, categories,
            )
        else:
            # Simple add (no drop): score on the added player's rank alone
            add_scores = [_compute_add_score(p.get("player_id"), db, categories) for p in adds]
            grade_score = sum(add_scores) / len(add_scores) if add_scores else 2.0

        # ── Stream grade boost ────────────────────────────────────────────────
        # A streaming pickup with a good matchup earns a contextual bonus.
        # A fringe arm (rank 280–350) streaming into a great matchup can
        # be a legitimate C+/B- decision even though ROS value alone says C/D.
        # Cap: streaming can lift by at most 1 full grade tier (0.7 points),
        # and never above B (3.0) — we don't want a streaming fringe arm
        # graded as an A just because the park is friendly today.
        if stream_ctx and stream_ctx.get("is_stream") and stream_ctx.get("stat_type") == "pitching":
            park_factor = stream_ctx.get("park_factor", 1.0) or 1.0
            vegas = stream_ctx.get("vegas_run_factor", 1.0) or 1.0
            # Pitcher-friendly environment: low park factor (pitcher's park) and low Vegas total
            # park_factor > 1 = hitter's park (bad for streaming SP)
            # vegas > 1 = high-scoring game (bad for streaming SP)
            env_score = (2.0 - park_factor) * 0.3 + (2.0 - vegas) * 0.3  # ~0 neutral, up to 0.6 ideal
            # Also factor in matchup category urgency from matchup_ctx
            # Simple heuristic: if losing W, IP, or K, boost by 0.1 each
            cat_urgency = 0.0
            if matchup_ctx:
                mc_lower = matchup_ctx.lower()
                for cat in ("wins", " w,", "ip", "k,", "strikeouts", " k "):
                    if cat in mc_lower and "losing" in mc_lower:
                        cat_urgency = min(cat_urgency + 0.1, 0.3)
                        break
            stream_boost = min(env_score + cat_urgency, 0.7)
            grade_score = min(grade_score + stream_boost, 3.0)  # cap at B
        elif stream_ctx and stream_ctx.get("is_stream") and stream_ctx.get("stat_type") == "batting":
            # Smaller boost for batter streams — less reliable/impactful than SP streams
            cat_urgency = 0.0
            if matchup_ctx:
                # Check if team is losing categories this batter would help
                cat_urgency = 0.2
            stream_boost = min(0.3 + cat_urgency, 0.5)
            grade_score = min(grade_score + stream_boost, 2.7)  # cap at B-

    elif txn_type == "drop":
        all_scores = [_compute_drop_score(p.get("player_id"), db, categories) for p in participants]
        grade_score = sum(all_scores) / len(all_scores) if all_scores else 2.0

    else:  # trade
        has_keepers = _is_keeper_league(league)
        player_ages: dict[int, int] = {}
        if has_keepers:
            try:
                from fantasai.api.v1.analysis import _fetch_player_ages
                all_trade_ids = [
                    p.get("player_id")
                    for side in participants
                    for p in side.get("players_added", []) + side.get("players_dropped", [])
                    if p.get("player_id")
                ]
                player_ages = _fetch_player_ages(all_trade_ids, db)
            except Exception:
                pass

        score_a, score_b = _compute_trade_score(participants, db, categories, league=league, player_ages=player_ages)
        grade_score = (score_a + score_b) / 2
        if len(participants) >= 1:
            participants[0]["_grade_score"] = score_a
            participants[0]["_grade_letter"] = _score_to_letter(score_a)
        if len(participants) >= 2:
            participants[1]["_grade_score"] = score_b
            participants[1]["_grade_letter"] = _score_to_letter(score_b)
        txn.participants = participants

    grade_letter = _score_to_letter(grade_score)
    txn.grade_letter = grade_letter
    txn.grade_score = grade_score
    txn.graded_at = datetime.now(tz=timezone.utc)

    # Freeze ranks at grading time so re-grades of old transactions show the
    # historical rank (not today's rank).  Only written if not already stored.
    try:
        rank_map = _get_live_rank_map(db, categories)
        if rank_map:
            _participants = txn.participants or []
            _changed = False
            if txn_type in ("add", "drop"):
                for p in _participants:
                    pid = p.get("player_id")
                    if pid and "_ros_rank_at_grade" not in p and pid in rank_map:
                        p["_ros_rank_at_grade"] = rank_map[pid]
                        _changed = True
            else:
                for side in _participants:
                    for p in side.get("players_added", []) + side.get("players_dropped", []):
                        pid = p.get("player_id")
                        if pid and "_ros_rank_at_grade" not in p and pid in rank_map:
                            p["_ros_rank_at_grade"] = rank_map[pid]
                            _changed = True
            if _changed:
                txn.participants = _participants
    except Exception:
        pass

    from fantasai.brain.writer_persona import SYSTEM_PROMPT as _WRITER_PERSONA
    _move_grade_system = _WRITER_PERSONA + (
        "\n\n"
        "───────────────────────────────────────\n"
        "MOVE GRADER — TRANSACTION VERDICT MODE\n"
        "───────────────────────────────────────\n"
        "You are writing a brief, direct fantasy transaction verdict. Same voice as always — \n"
        "opinionated, never generic. 2–3 sentences maximum.\n"
        "HARD RULE — PERSONALITY MINIMUM: MINIMUM TWO personality elements required even in 2 sentences. "
        "Include AT LEAST ONE analogy or cultural reference AND AT LEAST ONE signature phrase or "
        "irreverent observation. 'Witty when earned' is not a standard — the voice is always on.\n\n"
        "MOVE GRADER RULES (non-negotiable):\n"
        "1. Use ONLY the player data, team names, stats, and league format in the prompt. "
        "Your training knowledge about players is outdated — the provided data is authoritative.\n"
        "2. Never mention injuries, surgeries, or health history unless explicitly listed.\n"
        "3. Never reference a league format other than the one stated in the prompt.\n"
        "4. If a player's team is listed, use that team. Never substitute a different team.\n"
        "5. NEVER begin with 'VERDICT:', 'PASS', 'FAIL', or any verdict label. Lead with opinion.\n"
        "6. Stats labeled '[2026 Steamer projection — full-season]' are projections — always "
        "frame them as such: 'Steamer projects X this season'. Never quote as observed fact.\n"
        "   Stats labeled '[2026 actual — N G/PA/IP]' are real; flag the sample if under 50 PA / 5 GS.\n"
        "7. 'ROS-rank-at-move' = the player's Rest-of-Season rank AT THE TIME this move happened. "
        "Say 'ranked #N at the time of this move' or 'was ranked #N when this trade happened'. "
        "Never say 'currently ranked' or 'is ranked #N'.\n"
        "8. K/9 benchmarks: elite=10.0+, above avg=9.0–9.9, avg=8.0–8.9, below avg=7.0–7.9.\n"
        "9. Always 'Name (TEAM)' on first mention when team is provided.\n"
        "10. Apply the ADVANCED STATS FRAMEWORK: if the verdict hinges on xERA vs ERA, \n"
        "    Barrel% vs surface AVG, or HR/FB luck — say so in your voice. One analytical \n"
        "    insight beats three plain stat citations."
    )

    # Generate rationale via Claude Haiku
    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

            if txn_type == "trade" and len(participants) >= 2:
                # ── Two per-side rationales for trades ────────────────────────
                data_block, stat_instructions, early_season_note = _build_txn_shared_context(
                    txn, league, db
                )
                side_rationales: list[str] = []
                for side_idx in range(2):
                    side_prompt = _build_trade_side_prompt(
                        side_idx=side_idx,
                        participants=participants,
                        data_block=data_block,
                        stat_instructions=stat_instructions,
                        early_season_note=early_season_note,
                        categories=categories,
                        has_keepers=has_keepers,
                        player_ages=player_ages,
                        db=db,
                    )
                    try:
                        resp = client.messages.create(
                            model="claude-haiku-4-5",
                            max_tokens=300,
                            system=_move_grade_system,
                            messages=[{"role": "user", "content": side_prompt}],
                        )
                        side_rationale = resp.content[0].text.strip()
                    except Exception:
                        _log.error(
                            "grade_transaction: Claude call failed for trade side %d txn %s",
                            side_idx, txn.yahoo_transaction_id, exc_info=True
                        )
                        side_rationale = f"Grade: {participants[side_idx].get('_grade_letter', '?')}."
                    side_rationales.append(side_rationale)
                    participants[side_idx]["_grade_rationale"] = side_rationale

                txn.participants = participants
                # Combined rationale: both sides' verdicts separated by a divider
                txn.grade_rationale = "\n\n".join(side_rationales)

            else:
                prompt = _build_prompt(txn, league, db, stream_ctx=stream_ctx, matchup_ctx=matchup_ctx)
                response = client.messages.create(
                    model="claude-haiku-4-5",
                    max_tokens=300,
                    system=_move_grade_system,
                    messages=[{"role": "user", "content": prompt}],
                )
                txn.grade_rationale = response.content[0].text.strip()

        except Exception:
            _log.error("grade_transaction: Claude call failed for txn %s", txn.yahoo_transaction_id, exc_info=True)
            txn.grade_rationale = f"Grade: {grade_letter}. Analysis unavailable."

    # Generate grade card image(s)
    try:
        from fantasai.brain.grade_card import render_grade_card, render_trade_side_cards
        if txn_type == "trade" and len(participants) >= 2:
            side_paths = render_trade_side_cards(txn, db)
            if side_paths:
                for i, path in enumerate(side_paths):
                    if i < len(participants):
                        participants[i]["_card_image_path"] = path
                txn.participants = participants
                # Primary card_image_path = side 0 for backwards compat
                txn.card_image_path = side_paths[0] if side_paths else None
        else:
            card_path = render_grade_card(txn, db)
            if card_path:
                txn.card_image_path = card_path
    except Exception:
        _log.warning("grade_transaction: card render failed for %s", txn.yahoo_transaction_id, exc_info=True)

    # Force SQLAlchemy to write the final participants state.
    # JSON columns don't track in-place mutations of nested dicts — repeated
    # assignments to the same Python list object can be no-ops for change
    # detection.  flag_modified guarantees the current value is flushed.
    try:
        from sqlalchemy.orm import attributes as _sa_attrs
        _sa_attrs.flag_modified(txn, "participants")
    except Exception:
        pass

    _log.info(
        "grade_transaction: %s → %s (%.2f) for %s",
        txn.yahoo_transaction_id, grade_letter, grade_score, txn.transaction_type,
    )


# ── Lookback grading ──────────────────────────────────────────────────────────


def _was_player_re_added(
    db: "Session",
    txn: "Transaction",
    player_id: int,
    team_key: str,
) -> bool:
    """Return True if the same manager re-added this player after the drop."""
    try:
        from fantasai.models.transaction import Transaction as Txn
        from sqlalchemy import and_

        # Look for any ADD transaction by the same team_key after this transaction's timestamp
        # that contains this player_id in its participants
        candidates = (
            db.query(Txn)
            .filter(
                and_(
                    Txn.transaction_type == "add",
                    Txn.yahoo_timestamp > txn.yahoo_timestamp,
                    Txn.league_id == txn.league_id,
                )
            )
            .all()
        )
        for candidate in candidates:
            for p in (candidate.participants or []):
                if (
                    p.get("action") == "add"
                    and p.get("player_id") == player_id
                    and p.get("team_key") == team_key
                ):
                    return True
        return False
    except Exception:
        return False


def _get_actual_stats_summary(db: "Session", player_id: int) -> dict:
    """Return actual 2026 stats for a player and whether there is enough sample to assess."""
    result: dict = {
        "has_sample": False,
        "stat_type": None,
        "actual_label": None,
        "stats_str": None,
        "vs_proj": "unknown",
        "g": 0,
        "pa": 0,
    }
    try:
        from fantasai.models.player import PlayerStats

        actual_row = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == player_id,
                PlayerStats.season == 2026,
                PlayerStats.data_source == "actual",
                PlayerStats.week.is_(None),
            )
            .first()
        )
        if not actual_row:
            return result

        proj_row = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == player_id,
                PlayerStats.season == 2026,
                PlayerStats.data_source == "projection",
                PlayerStats.week.is_(None),
            )
            .first()
        )

        rate = actual_row.rate_stats or {}
        adv = actual_row.advanced_stats or {}
        counting = actual_row.counting_stats or {}
        stat_type = actual_row.stat_type or "batting"

        result["stat_type"] = stat_type

        if stat_type == "pitching":
            gs = int(float(counting.get("GS", 0) or 0))
            ip = float(counting.get("IP", 0) or 0)
            result["gs"] = gs
            result["ip"] = ip
            has_sample = gs >= 3 or ip >= 15
            result["has_sample"] = has_sample
            result["actual_label"] = f"[2026 actual — {gs} GS, {ip:.1f} IP]"

            stat_parts: list[str] = []
            era = rate.get("ERA")
            whip = rate.get("WHIP")
            k9 = rate.get("K/9") or rate.get("K9")
            if era is not None:
                stat_parts.append(f"ERA={float(era):.2f}")
            if whip is not None:
                stat_parts.append(f"WHIP={float(whip):.2f}")
            if k9 is not None:
                stat_parts.append(f"K/9={float(k9):.2f}")
            for k in ["xERA", "SV"]:
                v = adv.get(k) or counting.get(k)
                if v is not None:
                    try:
                        stat_parts.append(f"{k}={float(v):.2f}")
                    except (TypeError, ValueError):
                        pass
            result["stats_str"] = ", ".join(stat_parts) if stat_parts else "no stats"

            # Compare to projection
            if proj_row and era is not None:
                proj_rate = proj_row.rate_stats or {}
                proj_era = proj_rate.get("ERA")
                proj_whip = proj_rate.get("WHIP")
                if proj_era is not None:
                    try:
                        delta_era = float(proj_era) - float(era)  # positive = better than proj
                        delta_whip = (
                            float(proj_whip) - float(whip)
                            if (proj_whip is not None and whip is not None)
                            else 0.0
                        )
                        if delta_era >= 0.30:
                            result["vs_proj"] = "above projection"
                        elif delta_era <= -0.30:
                            result["vs_proj"] = "below projection"
                        else:
                            result["vs_proj"] = "on track"
                    except (TypeError, ValueError):
                        pass

        else:  # batting
            pa = int(float(counting.get("PA", 0) or counting.get("AB", 0) or 0))
            g = int(float(counting.get("G", 0) or 0))
            result["pa"] = pa
            result["g"] = g
            has_sample = pa >= 20
            result["has_sample"] = has_sample
            result["actual_label"] = f"[2026 actual — {g} G, {pa} PA]"

            stat_parts = []
            avg = rate.get("AVG")
            obp = rate.get("OBP")
            slg = rate.get("SLG")
            if avg is not None:
                stat_parts.append(f"AVG={float(avg):.3f}")
            if obp is not None:
                stat_parts.append(f"OBP={float(obp):.3f}")
            if slg is not None:
                stat_parts.append(f"SLG={float(slg):.3f}")
            for k in ["HR", "SB", "R", "RBI"]:
                v = counting.get(k)
                if v is not None:
                    try:
                        stat_parts.append(f"{k}={int(float(v))}")
                    except (TypeError, ValueError):
                        pass
            result["stats_str"] = ", ".join(stat_parts) if stat_parts else "no stats"

            # Compare to projection
            if proj_row and avg is not None and obp is not None:
                proj_rate = proj_row.rate_stats or {}
                proj_avg = proj_rate.get("AVG")
                proj_obp = proj_rate.get("OBP")
                if proj_avg is not None and proj_obp is not None:
                    try:
                        avg_delta = float(avg) - float(proj_avg)
                        obp_delta = float(obp) - float(proj_obp)
                        if avg_delta >= 0.010 and obp_delta >= 0.010:
                            result["vs_proj"] = "above projection"
                        elif avg_delta <= -0.010 and obp_delta <= -0.010:
                            result["vs_proj"] = "below projection"
                        else:
                            result["vs_proj"] = "on track"
                    except (TypeError, ValueError):
                        pass

    except Exception:
        _log.debug("_get_actual_stats_summary failed for player_id=%s", player_id, exc_info=True)

    return result


def _build_lookback_prompt(
    txn: "Transaction",
    league: "League",
    db: "Session",
    context: dict,
) -> str:
    """Build a Claude prompt for the lookback (hindsight) grade rationale."""
    categories = league.scoring_categories or []
    cat_str = ", ".join(str(c) for c in categories[:8]) if categories else "H/AB, R, HR, RBI, SB, AVG, OPS, IP"
    league_format = _league_format_str(league)

    txn_type = context.get("txn_type", txn.transaction_type)
    original_grade = context.get("original_grade", txn.grade_letter or "?")
    player_summaries = context.get("player_summaries", [])
    re_add_note = context.get("re_add_note")
    scenario = context.get("scenario", "normal")

    lines: list[str] = [
        f"LEAGUE FORMAT: {league_format}",
        f"SCORING CATEGORIES: {cat_str}",
        f"ORIGINAL GRADE AT TIME OF TRANSACTION: {original_grade}",
        "",
        "ACTUAL 2026 PLAYER PERFORMANCE (from live DB — authoritative):",
    ]
    for ps in player_summaries:
        role = ps.get("role", "")
        name = ps.get("name", "?")
        actual_label = ps.get("actual_label") or "[no 2026 stats]"
        stats_str = ps.get("stats_str") or "no stats"
        vs_proj = ps.get("vs_proj", "unknown")
        lines.append(f"  - {name} ({role}): {actual_label} {stats_str} — vs projection: {vs_proj}")

    if re_add_note:
        lines.append(f"\nNOTE: {re_add_note}")

    # Tone guidance based on scenario
    if txn_type == "add":
        if scenario == "normal":
            tone = (
                "This was an add. Evaluate how the player actually performed versus what was expected. "
                "If the player exceeded projection, confirm the add aged well. "
                "If the player disappointed, call it out directly."
            )
    elif txn_type == "drop":
        if scenario == "re_added_good":
            tone = (
                "The manager dropped this player then brought them back. "
                "The player has performed well since being re-added. "
                "Acknowledge the full arc: acknowledge the initial drop was questionable, "
                "but credit the course correction."
            )
        elif scenario == "re_added_bad":
            tone = (
                "The manager dropped this player then brought them back — "
                "and the player is still struggling. Be playful but fair about going back "
                "to a player who hasn't delivered."
            )
        else:
            if context.get("vs_proj_main") == "above projection":
                tone = (
                    "This was a drop. The player has since thrived — call out this was a bad drop in hindsight."
                )
            elif context.get("vs_proj_main") == "below projection":
                tone = (
                    "This was a drop. The player has since flopped — validate the decision was correct."
                )
            else:
                tone = "This was a drop. Assess whether it aged well based on the player's actual performance."
    else:  # trade
        tone = (
            "This was a trade. Looking back, evaluate which side got the better end of it based on actual performance."
        )

    lines.append("")
    lines.append(tone)
    lines.append("")
    lines.append(
        "Write a 2-sentence HINDSIGHT verdict. "
        "Refer to the outcome as 'in hindsight' or 'looking back'. "
        "Be witty but fair. Use ONLY the player data above."
    )

    return "\n".join(lines)


def grade_transaction_lookback(
    db: "Session",
    txn: "Transaction",
    league: "League",
) -> None:
    """Grade a transaction in hindsight 4+ weeks after it occurred.

    Sets lookback_grade_letter, lookback_grade_score, lookback_grade_rationale,
    lookback_graded_at on txn.  Does NOT commit — caller is responsible.
    """
    from datetime import datetime, timedelta, timezone
    from fantasai.config import settings

    # Safety guard — never run on transactions less than 4 weeks old
    if txn.yahoo_timestamp:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(weeks=4)
        if txn.yahoo_timestamp > cutoff:
            _log.debug(
                "grade_transaction_lookback: skipping %s — less than 4 weeks old",
                txn.yahoo_transaction_id,
            )
            return

    txn_type = txn.transaction_type
    participants = txn.participants or []

    lookback_score: Optional[float] = None
    context: dict = {"txn_type": txn_type, "original_grade": txn.grade_letter or "?"}
    player_summaries: list[dict] = []

    if txn_type == "add":
        adds = [p for p in participants if p.get("action") == "add"]
        if not adds:
            return
        player = adds[0]
        player_id = player.get("player_id")
        if not player_id:
            return

        summary = _get_actual_stats_summary(db, player_id)
        if not summary["has_sample"]:
            _log.debug(
                "grade_transaction_lookback: skipping %s — not enough sample for add player %s",
                txn.yahoo_transaction_id, player_id,
            )
            return

        player_summaries.append({
            "name": player.get("player_name", "?"),
            "actual_label": summary.get("actual_label"),
            "stats_str": summary.get("stats_str"),
            "vs_proj": summary.get("vs_proj", "unknown"),
            "role": "added",
        })

        vs_proj = summary.get("vs_proj", "unknown")
        base_score = txn.grade_score or 2.5
        if vs_proj == "above projection":
            lookback_score = min(4.3, base_score + 0.5)
        elif vs_proj == "below projection":
            lookback_score = max(0.0, base_score - 0.7)
        else:
            lookback_score = base_score

        context["scenario"] = "normal"

    elif txn_type == "drop":
        if not participants:
            return
        player = participants[0]
        player_id = player.get("player_id")
        if not player_id:
            return
        team_key = player.get("team_key", "")

        summary = _get_actual_stats_summary(db, player_id)
        re_added = _was_player_re_added(db, txn, player_id, team_key)

        player_summaries.append({
            "name": player.get("player_name", "?"),
            "actual_label": summary.get("actual_label"),
            "stats_str": summary.get("stats_str"),
            "vs_proj": summary.get("vs_proj", "unknown"),
            "role": "dropped",
        })

        if re_added:
            vs_proj = summary.get("vs_proj", "unknown")
            if vs_proj in ("above projection", "on track"):
                scenario = "re_added_good"
                lookback_score = 2.3  # C+ — drop was questionable but course corrected
            else:
                scenario = "re_added_bad"
                lookback_score = 1.0  # D — went back to a struggling player

            # Estimate days until re-add for the note
            context["re_add_note"] = "Manager re-added this player after the drop"
            context["scenario"] = scenario
        else:
            if not summary["has_sample"]:
                _log.debug(
                    "grade_transaction_lookback: skipping %s — not enough sample for drop player %s",
                    txn.yahoo_transaction_id, player_id,
                )
                return

            vs_proj = summary.get("vs_proj", "unknown")
            context["scenario"] = "normal"
            context["vs_proj_main"] = vs_proj

            if vs_proj == "above projection":
                # Player thrived after being dropped → bad drop
                lookback_score = max(0.0, (txn.grade_score - 1.0) if txn.grade_score is not None else 1.0)
            elif vs_proj == "below projection":
                # Player flopped → good drop
                lookback_score = min(4.3, (txn.grade_score or 3.0) + 0.5)
            else:
                lookback_score = txn.grade_score or 2.5

    elif txn_type == "trade":
        if len(participants) < 2:
            return

        # Collect summaries for all players; require at least 1 player per side with a sample
        side0 = participants[0]
        side1 = participants[1]

        side0_received = side0.get("players_added", [])
        side0_given = side0.get("players_dropped", [])

        all_player_ids: list[int] = []
        for p in side0_received + side0_given:
            pid = p.get("player_id")
            if pid:
                all_player_ids.append(pid)
        for p in side1.get("players_added", []) + side1.get("players_dropped", []):
            pid = p.get("player_id")
            if pid:
                all_player_ids.append(pid)

        if not all_player_ids:
            return

        summaries_by_id: dict[int, dict] = {}
        for pid in all_player_ids:
            summaries_by_id[pid] = _get_actual_stats_summary(db, pid)

        # Need at least 1 player with a sample per side
        side0_received_has_sample = any(
            summaries_by_id.get(p.get("player_id", 0), {}).get("has_sample")
            for p in side0_received
            if p.get("player_id")
        )
        side1_received_has_sample = any(
            summaries_by_id.get(p.get("player_id", 0), {}).get("has_sample")
            for p in side1.get("players_added", [])
            if p.get("player_id")
        )

        if not side0_received_has_sample and not side1_received_has_sample:
            _log.debug(
                "grade_transaction_lookback: skipping trade %s — no player has enough sample yet",
                txn.yahoo_transaction_id,
            )
            return

        # Build player_summaries for prompt
        for p in side0_received:
            pid = p.get("player_id")
            s = summaries_by_id.get(pid or 0, {})
            player_summaries.append({
                "name": p.get("player_name", "?"),
                "actual_label": s.get("actual_label"),
                "stats_str": s.get("stats_str"),
                "vs_proj": s.get("vs_proj", "unknown"),
                "role": f"received by {side0.get('manager_name', 'side A')}",
            })
        for p in side0_given:
            pid = p.get("player_id")
            s = summaries_by_id.get(pid or 0, {})
            player_summaries.append({
                "name": p.get("player_name", "?"),
                "actual_label": s.get("actual_label"),
                "stats_str": s.get("stats_str"),
                "vs_proj": s.get("vs_proj", "unknown"),
                "role": f"given up by {side0.get('manager_name', 'side A')}",
            })

        # Score: compare vs_proj for received vs given for side 0
        def _count_direction(player_list: list[dict], direction: str) -> int:
            return sum(
                1 for p in player_list
                if summaries_by_id.get(p.get("player_id", 0), {}).get("vs_proj") == direction
            )

        received_above = _count_direction(side0_received, "above projection")
        received_below = _count_direction(side0_received, "below projection")
        given_above = _count_direction(side0_given, "above projection")
        given_below = _count_direction(side0_given, "below projection")

        base_score = txn.grade_score or 2.5

        # Side 0 won if received more "above projection" players than they gave up
        if received_above > given_above and received_above > received_below:
            lookback_score = min(4.3, base_score + 0.5)
        elif given_above > received_above and given_below < given_above:
            lookback_score = max(0.0, base_score - 0.7)
        else:
            lookback_score = base_score

        context["scenario"] = "normal"

    else:
        return

    if lookback_score is None:
        return

    lookback_letter = _score_to_letter(lookback_score)

    context["player_summaries"] = player_summaries

    # Generate rationale via Claude Haiku
    lookback_rationale: Optional[str] = None
    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            prompt = _build_lookback_prompt(txn, league, db, context)
            from fantasai.brain.writer_persona import SYSTEM_PROMPT as _WRITER_PERSONA
            _lookback_system = _WRITER_PERSONA + (
                "\n\n"
                "───────────────────────────────────────\n"
                "MOVE GRADER — HINDSIGHT REVIEW MODE\n"
                "───────────────────────────────────────\n"
                "You are writing a hindsight verdict on a past fantasy transaction. Same voice — \n"
                "opinionated, never generic. 2–3 sentences maximum.\n"
                "HARD RULE — PERSONALITY MINIMUM: MINIMUM TWO personality elements required even in 2 sentences. "
                "Include AT LEAST ONE analogy or cultural reference AND AT LEAST ONE signature phrase or "
                "irreverent observation. 'Witty when earned' is not a standard — the voice is always on.\n\n"
                "HINDSIGHT RULES (non-negotiable):\n"
                "1. You are reviewing in retrospect — 'in hindsight' or 'looking back'. Not real-time.\n"
                "2. Be witty but fair. If a drop aged well, validate it with dry satisfaction. "
                "If a pickup flopped, call it out directly — the persona has opinions.\n"
                "3. Use ONLY the player data provided — do not cite stats or events not listed.\n"
                "4. NEVER begin with 'VERDICT:', 'PASS', 'FAIL', or any verdict label. Jump straight in.\n"
                "5. K/9 benchmarks: elite=10.0+, above avg=9.0–9.9, avg=8.0–8.9, below avg=7.0–7.9.\n"
                "6. Apply the ADVANCED STATS FRAMEWORK in retrospect: if the original pick was good \n"
                "   because xERA was better than ERA, or bad because HR/FB was clearly unsustainable — \n"
                "   say so. The hindsight is more satisfying when it's analytically grounded."
            )
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=300,
                system=_lookback_system,
                messages=[{"role": "user", "content": prompt}],
            )
            lookback_rationale = response.content[0].text.strip()
        except Exception:
            _log.error(
                "grade_transaction_lookback: Claude call failed for txn %s",
                txn.yahoo_transaction_id,
                exc_info=True,
            )
            lookback_rationale = f"Lookback grade: {lookback_letter}."

    txn.lookback_grade_letter = lookback_letter
    txn.lookback_grade_score = lookback_score
    txn.lookback_grade_rationale = lookback_rationale
    txn.lookback_graded_at = datetime.now(tz=timezone.utc)

    _log.info(
        "grade_transaction_lookback: %s → lookback %s (%.2f) for %s",
        txn.yahoo_transaction_id, lookback_letter, lookback_score, txn_type,
    )
