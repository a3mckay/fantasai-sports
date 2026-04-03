"""Scheduled blurb generation for player rankings.

Generates 2-3 sentence fantasy baseball analysis notes for the top N players
in each ranking mode, stored in the Ranking table for display on the Rankings page.

Called by the Monday 4am EST APScheduler job in main.py.
Also exposed via POST /rankings/generate-blurbs for on-demand generation.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

# Abbreviation → full team name (2026 rosters)
_MLB_TEAM_NAMES: dict[str, str] = {
    "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHC": "Chicago Cubs",
    "CWS": "Chicago White Sox",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KC":  "Kansas City Royals",
    "KCR": "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "LAD": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYM": "New York Mets",
    "NYY": "New York Yankees",
    "OAK": "Oakland Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SD":  "San Diego Padres",
    "SDP": "San Diego Padres",
    "SF":  "San Francisco Giants",
    "SFG": "San Francisco Giants",
    "SEA": "Seattle Mariners",
    "STL": "St. Louis Cardinals",
    "TB":  "Tampa Bay Rays",
    "TBR": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WSH": "Washington Nationals",
    "WSN": "Washington Nationals",
}

# Mode → (ranking_type, horizon) mapping
_MODE_MAP: dict[str, tuple[str, str | None]] = {
    "season": ("predictive", "season"),
    "week":   ("predictive", "week"),
    "month":  ("predictive", "month"),
    "current": ("current", None),
}


def generate_rankings_blurbs(
    db: Session,
    api_key: str | None,
    mode: str = "season",
    top_n: int = 300,
    _batch_requests_out: list | None = None,
) -> dict:
    """Generate AI blurbs for the top N players in the given ranking mode.

    Args:
        db: SQLAlchemy session.
        api_key: Anthropic API key. If None or empty, returns immediately
            (unless _batch_requests_out is set — prompt-building needs no key).
        mode: One of "season", "week", "month", "current".
        top_n: Number of top players to generate blurbs for.
        _batch_requests_out: Internal — when set to a list, the function runs
            only the prompt-building loop and appends
            {custom_id, prompt, max_tokens, player_metadata} dicts instead of
            making API calls. Used by submit_rankings_blurbs_batch().

    Returns:
        Dict with keys: generated, skipped, errors, mode.
        When _batch_requests_out is set: {collected, mode}.
    """
    if not api_key and _batch_requests_out is None:
        _log.warning("blurb_scheduler: no API key set, skipping blurb generation")
        return {"generated": 0, "skipped": 0, "errors": 0, "mode": mode}

    try:
        import anthropic as _anthropic
    except ImportError:
        if _batch_requests_out is None:
            _log.warning("blurb_scheduler: anthropic package not installed, skipping")
            return {"generated": 0, "skipped": 0, "errors": 0, "mode": mode}
        _anthropic = None  # type: ignore[assignment]

    from fantasai.api.v1.rankings import RANKINGS_DEFAULT_CATEGORIES

    # Use mode-specific period strings so season/week/month blurbs each get
    # their own Ranking row and never overwrite each other.
    _PERIOD_MAP: dict[str, str] = {
        "season":  "2026-season",
        "week":    "2026-week",
        "month":   "2026-month",
        "current": "2026-current",
    }
    CURRENT_PERIOD = _PERIOD_MAP.get(mode, "2026-season")
    from fantasai.api.v1.recommendations import _compute_rankings
    from fantasai.brain.writer_persona import SYSTEM_PROMPT
    from fantasai.engine.projection import ProjectionHorizon
    from fantasai.engine.schedule import (
        build_player_week_context,
        fetch_weekly_schedule,
        get_current_week_bounds,
    )
    from fantasai.models.ranking import Ranking

    if mode not in _MODE_MAP:
        _log.error("blurb_scheduler: unknown mode %r", mode)
        return {"generated": 0, "skipped": 0, "errors": 0, "mode": mode}

    ranking_type, horizon = _MODE_MAP[mode]
    categories = RANKINGS_DEFAULT_CATEGORIES

    # Fetch the rankings list
    try:
        if ranking_type == "current":
            current_list, _ = _compute_rankings(db, categories, ranking_type="current")
            player_rankings = current_list
        else:
            _, predictive_list = _compute_rankings(
                db, categories, horizon=ProjectionHorizon(horizon)
            )
            player_rankings = predictive_list
    except Exception:
        _log.error("blurb_scheduler: failed to compute rankings for mode=%r", mode, exc_info=True)
        return {"generated": 0, "skipped": 0, "errors": 0, "mode": mode}

    if not player_rankings:
        _log.warning("blurb_scheduler: no rankings found for mode=%r", mode)
        return {"generated": 0, "skipped": 0, "errors": 0, "mode": mode}

    top_players = player_rankings[:top_n]
    top_player_ids = [p.player_id for p in top_players]

    # For week mode, pre-fetch the schedule so we can enrich blurb prompts
    # with starts, opponent teams, park factors, Vegas odds, and weather.
    week_player_schedules: dict = {}
    if mode == "week":
        try:
            week_start, week_end = get_current_week_bounds()
            week_player_schedules = fetch_weekly_schedule(week_start, week_end, db)
        except Exception:
            _log.warning("blurb_scheduler: could not fetch weekly schedule for blurb context", exc_info=True)

    # Bulk-fetch key raw stats for all top players so we can include actual
    # metric values in prompts (SwStr%, xFIP, Barrel%, xwOBA, etc.)
    from fantasai.models.player import PlayerStats as _PlayerStats
    raw_stats_map: dict[int, dict] = {}
    proj_map: dict[int, dict] = {}  # Steamer projection rows — declared here so accessible in player loop
    try:
        # Prefer actual stats, fall back to projection if actual is absent
        all_stat_rows = (
            db.query(_PlayerStats)
            .filter(
                _PlayerStats.player_id.in_(top_player_ids),
                _PlayerStats.season == 2026,
                _PlayerStats.week.is_(None),
            )
            .all()
        )
        # Track two-way players: anyone with both batting and pitching stat rows
        _stat_types_seen: dict[int, set[str]] = {}
        for row in all_stat_rows:
            _stat_types_seen.setdefault(row.player_id, set()).add(row.stat_type or "batting")
        two_way_player_ids: set[int] = {
            pid for pid, types in _stat_types_seen.items()
            if "batting" in types and "pitching" in types
        }

        # Build per player: track both actual and projection rows separately.
        # For tiny samples (< 5 IP / < 20 PA), use projection rate/advanced stats
        # so we don't feed distorted small-sample rates (45.00 BB/9 from 0.2 IP)
        # into the blurb prompt. Actual counting stats are always used.
        actual_map: dict[int, dict] = {}
        proj_map = {}  # reset (already declared above)
        for row in all_stat_rows:
            pid = row.player_id
            data = {
                "counting": row.counting_stats or {},
                "rate":     row.rate_stats or {},
                "advanced": row.advanced_stats or {},
                "stat_type": row.stat_type or "batting",
            }
            if row.data_source == "actual":
                actual_map[pid] = data
            else:
                if pid not in proj_map:
                    proj_map[pid] = data

        for pid in set(list(actual_map.keys()) + list(proj_map.keys())):
            actual = actual_map.get(pid, {})
            proj   = proj_map.get(pid, {})
            stype  = actual.get("stat_type") or proj.get("stat_type", "batting")
            cnt    = actual.get("counting", {})

            # Detect tiny sample to decide whether rate stats are trustworthy
            if stype == "pitching":
                _sample = float(cnt.get("IP") or cnt.get("ip") or 0)
                _tiny   = _sample < 5.0
            else:
                _sample = int(float(cnt.get("PA") or cnt.get("pa") or 0))
                _tiny   = _sample < 20

            if _tiny and proj:
                # Use projection rate/advanced; actual counting (for citation guard)
                raw_stats_map[pid] = {
                    "counting": cnt,
                    "rate":     proj.get("rate", {}),
                    "advanced": proj.get("advanced", {}),
                    "stat_type": stype,
                }
            elif actual:
                raw_stats_map[pid] = actual
            elif proj:
                raw_stats_map[pid] = proj
    except Exception:
        _log.warning("blurb_scheduler: could not bulk-fetch raw stats", exc_info=True)

    # Pre-fetch previous ranks for current mode (used to populate rank movement in blurbs).
    # Compare against 7 days ago since current rankings refresh weekly.
    current_prev_rank_map: dict[int, int] = {}
    if mode == "current":
        try:
            from datetime import timedelta as _td
            from fantasai.models.ranking import RankingSnapshot as _RankingSnapshot
            _compare_date = date.today() - _td(days=7)
            _prev_snaps = (
                db.query(_RankingSnapshot)
                .filter(
                    _RankingSnapshot.player_id.in_(top_player_ids),
                    _RankingSnapshot.ranking_type == "current",
                    _RankingSnapshot.horizon == "current",
                    _RankingSnapshot.snapshot_date == _compare_date,
                )
                .all()
            )
            for _s in _prev_snaps:
                current_prev_rank_map[_s.player_id] = _s.overall_rank
        except Exception:
            _log.warning("blurb_scheduler: could not pre-fetch rank deltas for current mode", exc_info=True)

    # Only create the API client when we'll actually make calls.
    # In collect-only mode (_batch_requests_out set) no calls are made.
    client = _anthropic.Anthropic(api_key=api_key) if _batch_requests_out is None else None

    generated = 0
    skipped = 0
    errors = 0

    # Key predictive metrics to surface per stat type
    _PITCHER_METRICS = ["SwStr%", "CSW%", "K/9", "K9", "BB/9", "BB9", "BB%", "xERA", "xFIP", "SIERA", "WHIP", "GB%",
                        "Stuff+", "vFA", "Ext", "SpinRate", "K-BB%"]
    _BATTER_METRICS  = ["Barrel%", "HardHit%", "xwOBA", "xBA", "xSLG", "OBP", "AVG", "BB%", "K%",
                        "Sweet-Spot%", "PulledFB%", "Sprint Speed", "wRC+"]

    def _fmt_metric(key: str, val: float) -> str:
        """Format a raw metric value for a prompt data block."""
        pct_keys = {"SwStr%", "CSW%", "BB%", "K%", "GB%", "HardHit%", "Barrel%", "Sweet-Spot%", "PulledFB%", "K-BB%"}
        if key in pct_keys:
            return f"{key}: {val:.1f}%"
        if key in {"xwOBA", "xBA", "xSLG", "OBP", "AVG"}:
            return f"{key}: {val:.3f}"
        if key == "Sprint Speed":
            return f"{key}: {val:.1f} ft/s"
        if key in {"vFA", "Ext"}:
            return f"{key}: {val:.1f}"
        if key == "wRC+":
            return f"{key}: {int(val)}"
        return f"{key}: {val:.2f}"

    def _fmt_metric_with_pct(key: str, val: float, pct_entry: dict) -> str:
        """Format a metric value with its percentile label and population average."""
        base = _fmt_metric(key, val)
        label = pct_entry.get("label", "")
        avg   = pct_entry.get("avg")
        pct   = pct_entry.get("pct")
        if label and avg is not None and pct is not None:
            avg_fmt = _fmt_metric(key, float(avg)).split(": ", 1)[-1]
            return f"{base} [{label} — {pct:.0f}th pct; avg: {avg_fmt}]"
        return base

    # Days into the 2026 season — used to build small-sample context
    _SEASON_START_2026 = date(2026, 3, 26)
    _days_into_season = max(0, (date.today() - _SEASON_START_2026).days)

    def _rank_length_target(rank: int, outperformer_flag: int | None, prev_rank: int | None) -> str:
        """Return length guidance string for the given rank tier."""
        is_mover = prev_rank is not None and abs(rank - prev_rank) >= 20
        is_outperformer = outperformer_flag is not None and outperformer_flag <= 2

        if rank <= 50:
            base = "5–7 sentences"
        elif rank <= 150:
            base = "3–5 sentences"
        else:
            base = "2–3 sentences"

        if is_mover or is_outperformer:
            # Expand one tier
            if rank <= 50:
                return "5–7 sentences (expand: notable situation)"
            else:
                return "4–5 sentences (expand: notable situation)"
        return base

    def _build_outperformer_note(outperformer_flag: int | None, stat_type: str) -> str | None:
        """Return an outperformer context line for the prompt, if applicable."""
        if outperformer_flag == 1:
            if stat_type == "pitching":
                return ("OUTPERFORMER FLAG: Tier 1 (sustained) — ERA has beaten xERA/SIERA "
                        "for multiple seasons. xStats keep calling for regression; it hasn't arrived. "
                        "Bemused tone warranted: 'We're not sure how they keep doing it, but they haven't stopped.'")
            return ("OUTPERFORMER FLAG: Tier 1 (sustained) — actual AVG/OBP has beaten xBA/xOBP "
                    "for multiple seasons. Bemused tone: 'Along for the ride.'")
        if outperformer_flag == 2:
            if stat_type == "pitching":
                return ("OUTPERFORMER FLAG: Tier 2 (single season) — ERA meaningfully below xERA/SIERA. "
                        "Flag regression risk clearly: 'xStats say the correction is coming.'")
            return ("OUTPERFORMER FLAG: Tier 2 (single season) — actual AVG meaningfully above xBA. "
                    "Flag regression risk: 'Sell high opportunity exists.'")
        if outperformer_flag == 3:
            return ("OUTPERFORMER FLAG: Tier 3 (small sample) — hot start, not enough PA/IP to know if real. "
                    "Do NOT treat the surface stats as evidence of sustained skill. Use hot-start language.")
        return None

    def _build_key_stats(
        player_id: int,
        stat_type: str,
        percentile_data: dict | None = None,
    ) -> str:
        raw = raw_stats_map.get(player_id)
        if not raw:
            return "(stats not yet available this season)"
        cnt  = raw.get("counting", {})
        rate = raw.get("rate", {})
        adv  = raw.get("advanced", {})
        keys = _PITCHER_METRICS if stat_type == "pitching" else _BATTER_METRICS
        pct  = percentile_data or {}
        parts = []
        for k in keys:
            v = rate.get(k) if rate.get(k) is not None else adv.get(k)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            pct_entry = pct.get(k)
            if pct_entry:
                parts.append(_fmt_metric_with_pct(k, fv, pct_entry))
            else:
                parts.append(_fmt_metric(k, fv))

        stats_line = "\n  ".join(parts[:8]) if parts else "(stats not yet available this season)"

        # Prepend sample size so the blurb model knows how much to trust the stats.
        # Rule 10 in writer_persona.py uses this to apply appropriate language.
        if stat_type == "pitching":
            sample_val = cnt.get("IP") or cnt.get("ip") or 0.0
            try:
                sample_val = float(sample_val)
            except (TypeError, ValueError):
                sample_val = 0.0
            sample_line = f"SAMPLE SIZE: {sample_val:.1f} IP / day {_days_into_season} of season"
        else:
            sample_val = cnt.get("PA") or cnt.get("pa") or 0.0
            try:
                sample_val = int(float(sample_val))
            except (TypeError, ValueError):
                sample_val = 0
            sample_line = f"SAMPLE SIZE: {sample_val} PA / day {_days_into_season} of season"

        return f"{sample_line}\n  {stats_line}"

    def _build_steamer_comparison(player_id: int, stype: str) -> str:
        """Build a Steamer projection comparison block for current-mode prompts.

        Returns an empty string if no projection data is available.
        """
        steamer = proj_map.get(player_id)
        if not steamer:
            return ""
        s_rate = steamer.get("rate", {})
        s_adv  = steamer.get("advanced", {})
        parts: list[str] = []
        if stype == "pitching":
            for k in ["ERA", "WHIP", "K/9", "K9", "BB/9", "xFIP", "SIERA"]:
                v = s_rate.get(k) if s_rate.get(k) is not None else s_adv.get(k)
                if v is not None:
                    try:
                        parts.append(_fmt_metric(k, float(v)))
                    except (TypeError, ValueError):
                        pass
        else:
            for k in ["AVG", "OBP", "OPS", "wRC+", "xwOBA", "xBA"]:
                v = s_rate.get(k) if s_rate.get(k) is not None else s_adv.get(k)
                if v is not None:
                    try:
                        parts.append(_fmt_metric(k, float(v)))
                    except (TypeError, ValueError):
                        pass
        if not parts:
            return ""
        return "STEAMER PROJECTION (what was expected this season — compare to YTD actuals):\n  " + ", ".join(parts[:5])

    for player in top_players:
        # Skip MiLB prospects — they have PAV blurbs
        if getattr(player, "is_prospect", False):
            skipped += 1
            continue

        positions = getattr(player, "positions", []) or []
        contributions: dict = getattr(player, "category_contributions", {}) or {}

        # Top 4 category contributions by absolute z-score.
        # Convert z-scores to plain English — never expose σ notation to the model
        # as it bleeds straight into the blurb text.
        def _z_to_tier(z: float) -> str:
            if z >= 2.0:  return "elite contributor"
            if z >= 1.0:  return "strong contributor"
            if z >= 0.3:  return "above-average contributor"
            if z >= -0.3: return "average contributor"
            if z >= -1.0: return "below-average contributor"
            return "weak contributor"

        top_cats = sorted(contributions.items(), key=lambda x: abs(x[1]), reverse=True)[:4]
        cat_summary = ", ".join(
            f"{c} ({_z_to_tier(v)})" for c, v in top_cats
        )

        # Build actual YTD counting stats block — grounded numbers the model MAY cite.
        def _build_ytd_counting(pid: int, stype: str) -> str:
            raw = raw_stats_map.get(pid)
            if not raw:
                return "(no YTD counting stats yet)"
            cnt = raw.get("counting", {})
            if not cnt:
                return "(no YTD counting stats yet)"
            if stype == "pitching":
                keys = ["IP", "W", "SV", "HLD", "SO", "K", "ER"]
            else:
                keys = ["PA", "AB", "H", "HR", "R", "RBI", "SB", "BB", "SO"]
            parts = []
            for k in keys:
                v = cnt.get(k)
                if v is not None:
                    try:
                        fv = float(v)
                        parts.append(f"{k}: {int(fv) if fv == int(fv) else fv}")
                    except (TypeError, ValueError):
                        pass
            return ", ".join(parts) if parts else "(no YTD counting stats yet)"

        stat_type = getattr(player, "stat_type", "batting") or "batting"
        full_team = _MLB_TEAM_NAMES.get(player.team, player.team)

        # Classify pitcher role — use positions list, not stat_type alone,
        # so two-way Ohtani batting entries (stat_type="batting") are never
        # confused with his pitching entry.
        is_sp = stat_type == "pitching" and "SP" in positions
        is_rp = stat_type == "pitching" and not is_sp  # RP/CL — no starts

        # Build week context note
        week_context_note: str | None = None
        if mode == "week":
            ps = week_player_schedules.get(player.player_id)
            if ps is not None:
                week_context_note = build_player_week_context(
                    player.player_id, ps, stat_type, positions
                )
            elif is_sp:
                # SP with no schedule data — flag clearly
                week_context_note = "no probable start confirmed yet (schedule data unavailable)"
            elif is_rp:
                # RPs don't have probable-start entries; give neutral fallback
                week_context_note = "standard relief appearances expected this week (no scheduled starts)"

        # Pull percentile data and outperformer flag from PlayerRanking if available
        pct_data      = getattr(player, "percentile_data", None) or {}
        outperformer  = getattr(player, "outperformer_flag", None)
        prev_rank     = current_prev_rank_map.get(player.player_id) if mode == "current" else None

        length_target     = _rank_length_target(player.overall_rank, outperformer, prev_rank)
        outperformer_note = _build_outperformer_note(outperformer, stat_type)

        # Key predictive metrics for the data block (with percentiles when available)
        key_stats = _build_key_stats(player.player_id, stat_type, pct_data)

        # ── Prompt — week mode gets a schedule-first, metric-specific prompt ──
        if mode == "week":
            if is_sp:
                prompt = (
                    f"THIS WEEK FANTASY NOTE — {player.name} "
                    f"({full_team}, {'/'.join(positions)}, rank #{player.overall_rank})\n\n"
                    f"PLAYER FACTS (non-negotiable): {player.name} plays for the {full_team}. "
                    f"Do not reference any other team.\n\n"
                    f"WEEKLY SCHEDULE:\n{week_context_note or 'no probable start confirmed yet'}\n\n"
                    f"KEY PREDICTIVE METRICS:\n{key_stats}\n\n"
                    f"CATEGORY SIGNALS: {cat_summary}\n\n"
                    f"Write 2-3 sentences for a fantasy owner deciding whether to start this pitcher "
                    f"this week. LEAD with the start count and opponent(s). Cite at least one specific "
                    f"predictive metric by actual value (e.g. '14.2% SwStr%', '3.1 xFIP'). "
                    f"Note any significant park or weather concern. Direct and specific — no hedging."
                )
            elif is_rp:
                prompt = (
                    f"THIS WEEK FANTASY NOTE — {player.name} "
                    f"({full_team}, {'/'.join(positions)}, rank #{player.overall_rank})\n\n"
                    f"PLAYER FACTS (non-negotiable): {player.name} plays for the {full_team}. "
                    f"Do not reference any other team.\n\n"
                    f"ROLE: Relief pitcher / closer — makes NO starts. "
                    f"Value comes from saves, strikeouts, ERA, and WHIP in high-leverage appearances.\n\n"
                    f"KEY PREDICTIVE METRICS:\n{key_stats}\n\n"
                    f"CATEGORY SIGNALS: {cat_summary}\n\n"
                    f"Write 2-3 sentences for a fantasy owner. Focus on save opportunities, "
                    f"strikeout rate, and ratios — never mention starts. "
                    f"Cite at least one specific metric value. Direct and specific — no hedging."
                )
            else:
                prompt = (
                    f"THIS WEEK FANTASY NOTE — {player.name} "
                    f"({full_team}, {'/'.join(positions)}, rank #{player.overall_rank})\n\n"
                    f"PLAYER FACTS (non-negotiable): {player.name} plays for the {full_team}. "
                    f"Do not reference any other team.\n\n"
                    f"WEEKLY SCHEDULE:\n{week_context_note or 'standard 6-game week'}\n\n"
                    f"KEY PREDICTIVE METRICS:\n{key_stats}\n\n"
                    f"CATEGORY SIGNALS: {cat_summary}\n\n"
                    f"Write 2-3 sentences for a fantasy owner deciding whether to start this batter "
                    f"this week. Cite at least one actual metric value (e.g. '12% Barrel%', '.384 xwOBA'). "
                    f"Note any significant run environment, park factor, or schedule advantage. "
                    f"Direct and specific — no hedging."
                )
        else:
            # ── Non-week modes: season/month/current — talent-focused ──────────
            _role_note = ""
            if is_sp:
                _role_note = "Starting pitcher — lead with STUFF (velocity, Stuff+, spin), not outcomes."
            elif is_rp:
                _role_note = "Reliever/closer — lead with SAVE OPPORTUNITY and role security, then stuff."
            elif stat_type == "batting" and player.player_id in two_way_player_ids:
                _role_note = ("BATTING PROFILE ONLY — this is a hitter ranking. "
                              "Do NOT mention pitching, mound duties, or dual eligibility. "
                              "Focus entirely on hitting stats, plate discipline, and power.")

            _outperformer_block = f"\n{outperformer_note}" if outperformer_note else ""
            _ytd_counting = _build_ytd_counting(player.player_id, stat_type)

            # Detect zero-appearance players (injured / not yet played)
            _cnt_check = (raw_stats_map.get(player.player_id) or {}).get("counting", {})
            if stat_type == "pitching":
                _appearances = float(_cnt_check.get("IP") or _cnt_check.get("ip") or 0)
            else:
                _appearances = int(float(_cnt_check.get("PA") or _cnt_check.get("pa") or 0))
            _zero_pa_note = ""
            if _appearances == 0:
                _zero_pa_note = (
                    "⚠ ZERO APPEARANCES THIS SEASON: This player has NOT played a single game "
                    "yet (0 PA / 0 IP). They may be injured or on a wait list. "
                    "Do NOT describe them as 'cooking early', 'off to a hot start', "
                    "'already showing', or ANY language implying current-season performance. "
                    "Write ONLY about their projected profile, underlying skills, and role context. "
                    "If they are injured, you may acknowledge they are yet to appear.\n\n"
                )

            # Build closer role note when save category strength is strong/elite
            _closer_role_note = ""
            if not is_sp and is_rp:
                _sv_tier = contributions.get("SV")
                if _sv_tier is not None and _sv_tier >= 0.3:  # above-average or better
                    _tier_label = _z_to_tier(_sv_tier)
                    if "elite" in _tier_label or "strong" in _tier_label or "above-average" in _tier_label:
                        _closer_role_note = (
                            f"CLOSER ROLE CONFIRMED: Save category signal is '{_tier_label}'. "
                            f"Treat this player's closer role as secured. Do NOT speculate about "
                            f"committee risk, whether they 'have the job yet', or role uncertainty "
                            f"based on training knowledge. The projection data confirms the role.\n\n"
                        )

            if mode == "current":
                # ── Current Season mode: backward-looking, tone-tiered, rank-aware ──
                # Rank movement note
                _rank_mvmt_note = ""
                if prev_rank is not None:
                    _delta = prev_rank - player.overall_rank
                    if _delta >= 15:
                        _rank_mvmt_note = f"RANK MOVEMENT: Surging — up {_delta} spots since last week (from #{prev_rank})\n\n"
                    elif _delta >= 5:
                        _rank_mvmt_note = f"RANK MOVEMENT: Rising — up {_delta} spots since last week (from #{prev_rank})\n\n"
                    elif _delta <= -15:
                        _rank_mvmt_note = f"RANK MOVEMENT: Falling hard — down {abs(_delta)} spots since last week (from #{prev_rank})\n\n"
                    elif _delta <= -5:
                        _rank_mvmt_note = f"RANK MOVEMENT: Slipping — down {abs(_delta)} spots since last week (from #{prev_rank})\n\n"
                    else:
                        _rank_mvmt_note = f"RANK MOVEMENT: Steady — approximately same spot as last week (was #{prev_rank})\n\n"

                # Small sample: treat as notable only if meaningfully below expectation
                # Early season (days 1-14): nearly everyone is small-sample, don't flag
                # After day 14: flag batters < 20 PA or pitchers < 5 IP as notably limited
                _is_small_sample_notable = (
                    _days_into_season > 14 and (
                        (stat_type == "batting" and _appearances < 20) or
                        (stat_type == "pitching" and _appearances < 5)
                    )
                )

                # Tone tier — based on rank, with small-sample and Steamer-vs-actual modifiers
                _has_steamer = player.player_id in proj_map
                if _appearances == 0:
                    _tone_note = (
                        "TONE: Neutral. This player has not appeared yet this season — "
                        "acknowledge their absence, do not speculate on current performance.\n\n"
                    )
                elif _is_small_sample_notable:
                    _tone_note = (
                        "TONE: Neutral. Very limited sample — briefly note the small data set. "
                        "Do not over-interpret early results.\n\n"
                    )
                elif player.overall_rank <= 50:
                    _tone_note = (
                        "TONE: Celebratory. Elite current-season production — earn the praise with "
                        "specifics from their actual stats. No empty superlatives.\n\n"
                    )
                elif player.overall_rank <= 100:
                    _tone_note = (
                        "TONE: Positive. Solid contributor delivering real value. "
                        "Matter-of-fact about what they've actually done.\n\n"
                    )
                elif player.overall_rank <= 150:
                    _tone_note = (
                        "TONE: Measured. Modest production — acknowledge the contribution "
                        "without overselling.\n\n"
                    )
                elif player.overall_rank <= 200:
                    _tone_note = (
                        "TONE: Sleeper-aware. Production is limited but look for an angle: "
                        "are advanced stats better than the rank suggests? Is there a role change, "
                        "a hot streak, or a Steamer projection that implies more upside? "
                        "If there's a real sleeper case, lead with it. "
                        "If there genuinely isn't, be matter-of-fact about the limitations.\n\n"
                    )
                else:
                    # 200+: check for hidden upside before defaulting to critical
                    _tone_note = (
                        "TONE: Check the data before drawing a conclusion. This player ranks outside "
                        "the top 200 — look at whether advanced stats (xwOBA, xERA, xFIP) are "
                        "meaningfully better than their surface results, whether they're trending up, "
                        "or whether a role change creates new value. "
                        "If a real upside signal exists, call it out — don't bury it. "
                        "If the advanced stats"
                        + (
                            " and Steamer projection both confirm the struggles, "
                            "be direct: this player isn't a sleeper and owners should know why. "
                            if _has_steamer else
                            " also reflect limited production, be direct about it. "
                        )
                        + "\n\n"
                    )

                # Steamer comparison block (optional — only include if data exists)
                _steamer_cmp = _build_steamer_comparison(player.player_id, stat_type)
                _steamer_block = (_steamer_cmp + "\n\n") if _steamer_cmp else ""

                # key_stats for current mode: use actual YTD rate/advanced stats
                # (raw_stats_map already has actual data when sample is sufficient)

                prompt = (
                    f"CURRENT SEASON RANKING #{player.overall_rank} — {player.name} "
                    f"({full_team}, {'/'.join(positions)})\n\n"
                    f"PLAYER FACTS (non-negotiable): {player.name} plays for the {full_team}. "
                    f"Do not reference any other team.\n\n"
                    + _rank_mvmt_note
                    + _tone_note
                    + _zero_pa_note
                    + _closer_role_note
                    + (f"ROLE NOTE: {_role_note}\n\n" if _role_note else "")
                    + f"LENGTH: 2–3 sentences. Consistent length regardless of rank. Reactive and direct.\n\n"
                    + f"ACTUAL YTD COUNTING STATS (the only counting numbers you may cite):\n{_ytd_counting}\n\n"
                    + f"YTD RATE & ADVANCED STATS (actual current-season observations — frame as 'is posting', 'has put up', etc.):\n{key_stats}\n\n"
                    + _steamer_block
                    + f"CATEGORY STRENGTH vs. REST OF POOL:\n{cat_summary}\n\n"
                    + f"REQUIREMENTS:\n"
                    + f"- CURRENT SEASON ranking based on 2026 YTD actuals only. Fully backward-looking.\n"
                    + f"- DO NOT end with forward-looking language. No 'watch for', 'keep an eye on', 'should', 'will', 'expected to'.\n"
                    + f"- Reference the player's rank (#{player.overall_rank}) naturally in the blurb.\n"
                    + (f"- Briefly acknowledge whether they're trending up, down, or steady.\n" if prev_rank is not None else "")
                    + f"- NEVER cite a counting stat not listed in ACTUAL YTD COUNTING STATS above.\n"
                    + f"- Rate/advanced stats shown ARE actual current-season observations — frame them as such.\n"
                    + (f"- Steamer projection is optional context — reference only if the comparison adds meaningful insight.\n" if _steamer_cmp else "")
                    + (f"- If sample is notably small, mention it briefly without making it the entire blurb.\n" if _is_small_sample_notable else "")
                    + f"- No percentile language (no 'top X%', 'Xth percentile').\n"
                    + f"- No sigma (σ) notation.\n"
                    + f"- No hedging. Direct and specific."
                )
            else:
                # ── Projected modes (season/month) — talent-focused ─────────────
                # For ranks 151–300: sleeper framing — lead with the best angle
                # (undervalued metrics, role upside, injury recovery, etc.) rather
                # than treating low rank as a verdict on the player's value.
                _sleeper_note = ""
                if player.overall_rank > 150:
                    _sleeper_note = (
                        "BREAKOUT CHECK: This player is ranked outside the top 150. "
                        "Before writing the blurb, check the data for hidden upside: "
                        "Statcast metrics (xwOBA, xFIP, Barrel%) that outpace surface results; "
                        "Steamer projecting meaningfully more than the rank implies; "
                        "role or lineup change creating new opportunity; "
                        "injury recovery that hasn't yet shown in counting stats; "
                        "category scarcity value (rare SB source, saves role). "
                        "If a real upside signal exists in the data, call it out clearly — "
                        "these blurbs are read by owners hunting for waiver pickups. "
                        "If the data shows no meaningful upside, don't invent one — "
                        "be direct about why the rank reflects the player's actual value.\n\n"
                    )

                prompt = (
                f"RANK #{player.overall_rank} — {player.name} "
                f"({full_team}, {'/'.join(positions)})\n\n"
                f"PLAYER FACTS (non-negotiable): {player.name} plays for the {full_team}. "
                f"Do not reference any other team.\n\n"
                + _zero_pa_note
                + _closer_role_note
                + (f"ROLE NOTE: {_role_note}\n\n" if _role_note else "")
                + _sleeper_note
                + f"LENGTH: {length_target}\n\n"
                + _outperformer_block
                + ("\n\n" if outperformer_note else "")
                + f"ACTUAL YTD COUNTING STATS (the only counting numbers you may cite):\n{_ytd_counting}\n\n"
                + f"STEAMER PROJECTIONS + EXPECTED METRICS (projected values — NOT current-season observations):\n{key_stats}\n\n"
                f"PROJECTED CATEGORY STRENGTH (z-scores vs rest-of-pool — NOT actual counts, do not cite these as real numbers):\n{cat_summary}\n\n"
                f"REQUIREMENTS:\n"
                f"- NEVER cite a specific counting stat (HR, RBI, R, SB, K, W, SV, hits, etc.) "
                f"unless the exact number appears in ACTUAL YTD COUNTING STATS above.\n"
                f"- Metrics in STEAMER PROJECTIONS are projected values (Steamer/Statcast models), "
                f"NOT current season observations. Frame them as projections: "
                f"'Steamer projects a 128 wRC+', 'the projection calls for...', 'projects for...' — "
                f"NEVER say 'his 128 wRC+' as if it is an observed current stat.\n"
                f"- NEVER make claims about current-season rankings or league leaders "
                f"(e.g. 'leading the NL in HRs', 'tops in RBIs') — you do not have that data.\n"
                f"- If the sample size is small (< 50 PA / < 15 IP), do NOT discuss early-season "
                f"results as if they are meaningful. Focus on projected profile and underlying metrics.\n"
                f"- End with a forward-looking sentence (what to watch for)\n"
                f"- Call out specific categories this player helps or hurts\n"
                f"- If percentile data is shown, use the provided label (Elite/Above average/etc.) — "
                f"do not invent your own benchmark\n"
                f"- No hedging. Direct and specific.\n"
                f"- Note any meaningful xStat gap (regression warning or buying opportunity)"
            )

        # Current mode: flat 2–3 sentence budget for all players.
        # Projected modes: top-50 get more space for 5–7 sentences.
        if mode == "current":
            _max_tokens = 220
        else:
            _max_tokens = 400 if player.overall_rank <= 50 else (280 if player.overall_rank <= 150 else 200)

        # Shared cached system prompt block — defined once per player loop iteration
        # but the cache is warmed on the first API call and reused for all subsequent
        # players in the same mode run (ephemeral TTL: 5 min; run takes ~30s → safe).
        _cached_system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

        # ── Collect-only mode (used by submit_rankings_blurbs_batch) ─────────
        # When _batch_requests_out is set, skip all API calls and just store
        # the prompt + player metadata for later batch submission.
        if _batch_requests_out is not None:
            _batch_requests_out.append({
                "custom_id": str(player.player_id),
                "prompt": prompt,
                "max_tokens": _max_tokens,
                "player_metadata": {
                    "ranking_type": ranking_type,
                    "period": CURRENT_PERIOD,
                    "overall_rank": player.overall_rank,
                    "score": player.score,
                    "stat_type": stat_type,
                    "category_contributions": contributions,
                    "statcast_score": getattr(player, "statcast_score", None),
                    "steamer_score":  getattr(player, "steamer_score",  None),
                    "accum_score":    getattr(player, "accum_score",    None),
                    "outperformer_flag": getattr(player, "outperformer_flag", None),
                    "percentile_data":   (getattr(player, "percentile_data", None) or None),
                },
            })
            generated += 1
            continue

        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=_max_tokens,
                system=_cached_system,
                messages=[{"role": "user", "content": prompt}],
            )
            blurb_text = response.content[0].text.strip()

            # ── Stat-check verification pass ─────────────────────────────────
            # Ask a second call to verify no counting stats were hallucinated.
            # Only runs for ranks 1–150 in non-week modes — lower-ranked blurbs
            # have simpler prompts with less stat data to hallucinate from.
            if mode != "week" and player.overall_rank <= 150:
                _ytd_counting_check = _build_ytd_counting(player.player_id, stat_type)
                _zero_check = "0" if _appearances == 0 else str(_appearances)
                if mode == "current":
                    # Current mode: rate stats ARE actual observations (not projections).
                    # Check for forward-looking language and percentile language instead.
                    _verify_prompt = (
                        f"ACTUAL YTD COUNTING STATS: {_ytd_counting_check}\n"
                        f"SAMPLE SIZE: {_zero_check} {'IP' if stat_type == 'pitching' else 'PA'}\n\n"
                        f"BLURB TO CHECK:\n{blurb_text}\n\n"
                        f"Check the blurb for these errors:\n"
                        f"1. Specific counting numbers (home runs, RBIs, runs, steals, wins, saves, "
                        f"strikeouts, hits, walks, etc.) that do NOT appear in ACTUAL YTD COUNTING STATS.\n"
                        f"2. Current-season ranking claims with no data support "
                        f"(e.g. 'leading the NL in HRs', 'tops in RBIs', 'league leader in X').\n"
                        f"3. Sigma (σ) notation used directly in the text.\n"
                        f"4. If SAMPLE SIZE is 0: ANY language suggesting current-season performance "
                        f"('cooking early', 'off to a hot start', 'has posted', 'already showing', etc.).\n"
                        f"5. Forward-looking language: 'watch for', 'keep an eye on', 'should', "
                        f"'will be', 'expected to', 'projects', 'going forward', 'rest of season'.\n"
                        f"6. Percentile language: 'top X%', 'Xth percentile', 'percentile'.\n"
                        f"Reply with only 'OK' if clean, or 'ISSUE: <brief description>' if any problem found."
                    )
                else:
                    _verify_prompt = (
                    f"ACTUAL YTD COUNTING STATS: {_ytd_counting_check}\n"
                    f"SAMPLE SIZE: {_zero_check} {'IP' if stat_type == 'pitching' else 'PA'}\n\n"
                    f"BLURB TO CHECK:\n{blurb_text}\n\n"
                    f"Check the blurb for these errors:\n"
                    f"1. Specific counting numbers (home runs, RBIs, runs, steals, wins, saves, "
                    f"strikeouts, hits, walks, etc.) that do NOT appear in ACTUAL YTD COUNTING STATS.\n"
                    f"2. Current-season ranking claims with no data support "
                    f"(e.g. 'leading the NL in HRs', 'tops in RBIs', 'league leader in X').\n"
                    f"3. Sigma (σ) notation used directly in the text.\n"
                    f"4. If SAMPLE SIZE is 0: ANY language suggesting current-season performance "
                    f"('cooking early', 'off to a hot start', 'has posted', 'already showing', etc.).\n"
                    f"5. Rate stats (wRC+, AVG, K/9, BB/9, ERA, WHIP, etc.) cited as current-season "
                    f"observations (e.g. 'his 128 wRC+') rather than as projections.\n"
                    f"Reply with only 'OK' if clean, or 'ISSUE: <brief description>' if any problem found."
                    )
                try:
                    _verify_resp = client.messages.create(
                        model="claude-haiku-4-5",
                        max_tokens=60,
                        messages=[{"role": "user", "content": _verify_prompt}],
                    )
                    _verdict = _verify_resp.content[0].text.strip()
                    if _verdict.upper().startswith("ISSUE"):
                        _log.warning(
                            "blurb_scheduler: stat hallucination detected for %s — %s. Regenerating.",
                            player.name, _verdict,
                        )
                        # Regenerate with an explicit correction instruction
                        _strict_prompt = (
                            prompt
                            + f"\n\nPREVIOUS DRAFT WAS REJECTED because it cited a counting stat "
                            f"not in ACTUAL YTD COUNTING STATS: {_verdict}. "
                            f"Write a new blurb that does NOT mention any specific counts "
                            f"(no home run numbers, hit totals, stolen base counts, etc.) "
                            f"unless they are in ACTUAL YTD COUNTING STATS. "
                            f"Focus on the rate/advanced metrics and projected category strengths instead."
                        )
                        _regen = client.messages.create(
                            model="claude-haiku-4-5",
                            max_tokens=_max_tokens,
                            system=_cached_system,
                            messages=[{"role": "user", "content": _strict_prompt}],
                        )
                        blurb_text = _regen.content[0].text.strip()
                except Exception:
                    _log.warning(
                        "blurb_scheduler: stat-check pass failed for %s, using original blurb",
                        player.name, exc_info=True,
                    )

        except Exception:
            _log.error(
                "blurb_scheduler: API call failed for player_id=%d (%s)",
                player.player_id,
                player.name,
                exc_info=True,
            )
            errors += 1
            time.sleep(0.1)
            continue

        # Upsert Ranking row
        try:
            existing = (
                db.query(Ranking)
                .filter(
                    Ranking.player_id == player.player_id,
                    Ranking.ranking_type == ranking_type,
                    Ranking.period == CURRENT_PERIOD,
                    Ranking.league_id.is_(None),
                )
                .first()
            )

            _statcast_score = getattr(player, "statcast_score", None)
            _steamer_score  = getattr(player, "steamer_score",  None)
            _accum_score    = getattr(player, "accum_score",    None)
            _outperformer   = getattr(player, "outperformer_flag", None)
            _pct_data       = getattr(player, "percentile_data", None) or {}

            if existing:
                existing.blurb = blurb_text
                existing.overall_rank = player.overall_rank
                existing.score = player.score
                existing.category_contributions = contributions
                existing.statcast_score = _statcast_score
                existing.steamer_score  = _steamer_score
                existing.accum_score    = _accum_score
                existing.outperformer_flag = _outperformer
                existing.percentile_data   = _pct_data or None
                # Back-fill share_token on rows that pre-date the migration
                if not existing.share_token:
                    import secrets as _secrets
                    existing.share_token = _secrets.token_urlsafe(32)
            else:
                db.add(
                    Ranking(
                        player_id=player.player_id,
                        ranking_type=ranking_type,
                        period=CURRENT_PERIOD,
                        overall_rank=player.overall_rank,
                        score=player.score,
                        category_contributions=contributions,
                        blurb=blurb_text,
                        league_id=None,
                        statcast_score=_statcast_score,
                        steamer_score=_steamer_score,
                        accum_score=_accum_score,
                        outperformer_flag=_outperformer,
                        percentile_data=_pct_data or None,
                    )
                )

            db.commit()
            generated += 1
        except Exception:
            db.rollback()
            _log.error(
                "blurb_scheduler: DB upsert failed for player_id=%d (%s)",
                player.player_id,
                player.name,
                exc_info=True,
            )
            errors += 1

        time.sleep(0.1)

    if _batch_requests_out is not None:
        _log.info("blurb_scheduler: mode=%s collected %d prompts for batch submission", mode, generated)
        return {"collected": generated, "mode": mode}

    _log.info(
        "blurb_scheduler: mode=%s generated=%d skipped=%d errors=%d",
        mode,
        generated,
        skipped,
        errors,
    )
    return {"generated": generated, "skipped": skipped, "errors": errors, "mode": mode}


# ---------------------------------------------------------------------------
# Batch API — submit + collect (for scheduled runs; ~50% cost reduction)
# ---------------------------------------------------------------------------


def submit_rankings_blurbs_batch(
    db: Session,
    api_key: str | None,
    mode: str = "season",
    top_n: int = 300,
) -> dict:
    """Submit blurb generation for a ranking mode to the Anthropic Batches API.

    Builds all prompts using generate_rankings_blurbs() collect-only mode,
    submits them as a single batch, and stores a BlurbBatch row for later
    collection via collect_rankings_blurb_batches().

    50% cheaper than synchronous generation but async — results available
    within minutes to ~1 hour.  Use collect_rankings_blurb_batches() (or
    POST /rankings/collect-blurb-batches) to write results to the DB.

    Returns:
        Dict with batch_id, player_count, mode, status.
    """
    if not api_key:
        _log.warning("submit_rankings_blurbs_batch: no API key set")
        return {"error": "no API key", "mode": mode}

    try:
        import anthropic as _anthropic
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request as _BatchRequest
    except ImportError:
        _log.warning("submit_rankings_blurbs_batch: anthropic package not installed")
        return {"error": "anthropic not installed", "mode": mode}

    from fantasai.models.ranking import BlurbBatch

    _PERIOD_MAP: dict[str, str] = {
        "season":  "2026-season",
        "week":    "2026-week",
        "month":   "2026-month",
        "current": "2026-current",
    }
    period = _PERIOD_MAP.get(mode, "2026-season")

    # Build all prompts using the existing generate function's prompt-building
    # logic, without making any API calls.
    collected: list[dict] = []
    generate_rankings_blurbs(db, api_key=None, mode=mode, top_n=top_n, _batch_requests_out=collected)

    if not collected:
        _log.warning("submit_rankings_blurbs_batch: no players found for mode=%r", mode)
        return {"error": "no players found", "mode": mode}

    _cached_system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]

    batch_requests = [
        _BatchRequest(
            custom_id=item["custom_id"],
            params=MessageCreateParamsNonStreaming(
                model="claude-haiku-4-5",
                max_tokens=item["max_tokens"],
                system=_cached_system,
                messages=[{"role": "user", "content": item["prompt"]}],
            ),
        )
        for item in collected
    ]

    player_data_map = {item["custom_id"]: item["player_metadata"] for item in collected}

    client = _anthropic.Anthropic(api_key=api_key)
    batch = client.messages.batches.create(requests=batch_requests)

    db.add(BlurbBatch(
        mode=mode,
        period=period,
        batch_id=batch.id,
        player_count=len(batch_requests),
        player_data=player_data_map,
    ))
    db.commit()

    _log.info(
        "submit_rankings_blurbs_batch: submitted %d requests mode=%s batch_id=%s",
        len(batch_requests), mode, batch.id,
    )
    return {
        "batch_id": batch.id,
        "player_count": len(batch_requests),
        "mode": mode,
        "status": "submitted",
    }


def collect_rankings_blurb_batches(db: Session, api_key: str | None) -> dict:
    """Collect results from all pending blurb batches and write to the Ranking table.

    Checks every BlurbBatch with status="pending".  Batches that haven't
    completed yet are silently skipped — call again later.

    Returns:
        Dict with batches_checked, batches_collected, blurbs_written, errors.
    """
    if not api_key:
        return {"error": "no API key"}

    try:
        import anthropic as _anthropic
    except ImportError:
        return {"error": "anthropic not installed"}

    from datetime import datetime, timezone
    from fantasai.models.ranking import BlurbBatch, Ranking

    pending = db.query(BlurbBatch).filter(BlurbBatch.status == "pending").all()
    if not pending:
        return {"batches_checked": 0, "batches_collected": 0, "blurbs_written": 0, "errors": 0}

    client = _anthropic.Anthropic(api_key=api_key)

    batches_collected = 0
    blurbs_written = 0
    errors = 0

    for batch_record in pending:
        try:
            batch_status = client.messages.batches.retrieve(batch_record.batch_id)
            if batch_status.processing_status != "ended":
                _log.info(
                    "collect_rankings_blurb_batches: batch %s not ready yet (%s)",
                    batch_record.batch_id, batch_status.processing_status,
                )
                continue

            player_data_map: dict = batch_record.player_data or {}

            for result in client.messages.batches.results(batch_record.batch_id):
                if result.result.type != "succeeded":
                    _log.warning(
                        "collect_rankings_blurb_batches: request %s failed: %s",
                        result.custom_id, result.result.type,
                    )
                    errors += 1
                    continue

                try:
                    player_id = int(result.custom_id)
                except ValueError:
                    _log.warning("collect_rankings_blurb_batches: bad custom_id %r", result.custom_id)
                    errors += 1
                    continue

                text_blocks = [b for b in result.result.message.content if b.type == "text"]
                if not text_blocks:
                    errors += 1
                    continue
                blurb_text = text_blocks[0].text.strip()

                meta = player_data_map.get(str(player_id), {})
                ranking_type = meta.get("ranking_type", "predictive")
                period       = meta.get("period", batch_record.period)

                try:
                    existing = (
                        db.query(Ranking)
                        .filter(
                            Ranking.player_id == player_id,
                            Ranking.ranking_type == ranking_type,
                            Ranking.period == period,
                            Ranking.league_id.is_(None),
                        )
                        .first()
                    )

                    if existing:
                        existing.blurb             = blurb_text
                        existing.overall_rank       = meta.get("overall_rank", existing.overall_rank)
                        existing.score              = meta.get("score", existing.score)
                        existing.category_contributions = meta.get("category_contributions", existing.category_contributions)
                        existing.statcast_score     = meta.get("statcast_score", existing.statcast_score)
                        existing.steamer_score      = meta.get("steamer_score",  existing.steamer_score)
                        existing.accum_score        = meta.get("accum_score",    existing.accum_score)
                        existing.outperformer_flag  = meta.get("outperformer_flag", existing.outperformer_flag)
                        existing.percentile_data    = meta.get("percentile_data",   existing.percentile_data)
                        if not existing.share_token:
                            import secrets as _secrets
                            existing.share_token = _secrets.token_urlsafe(32)
                    else:
                        import secrets as _secrets
                        db.add(Ranking(
                            player_id=player_id,
                            ranking_type=ranking_type,
                            period=period,
                            overall_rank=meta.get("overall_rank", 9999),
                            score=meta.get("score", 0.0),
                            category_contributions=meta.get("category_contributions", {}),
                            blurb=blurb_text,
                            league_id=None,
                            statcast_score=meta.get("statcast_score"),
                            steamer_score=meta.get("steamer_score"),
                            accum_score=meta.get("accum_score"),
                            outperformer_flag=meta.get("outperformer_flag"),
                            percentile_data=meta.get("percentile_data"),
                        ))

                    db.commit()
                    blurbs_written += 1
                except Exception:
                    db.rollback()
                    _log.error(
                        "collect_rankings_blurb_batches: DB upsert failed player_id=%d", player_id,
                        exc_info=True,
                    )
                    errors += 1

            batch_record.status = "collected"
            batch_record.collected_at = datetime.now(timezone.utc)
            db.commit()
            batches_collected += 1

        except Exception:
            _log.error(
                "collect_rankings_blurb_batches: failed for batch_id=%s", batch_record.batch_id,
                exc_info=True,
            )
            db.rollback()
            errors += 1

    _log.info(
        "collect_rankings_blurb_batches: checked=%d collected=%d blurbs=%d errors=%d",
        len(pending), batches_collected, blurbs_written, errors,
    )
    return {
        "batches_checked": len(pending),
        "batches_collected": batches_collected,
        "blurbs_written": blurbs_written,
        "errors": errors,
    }
