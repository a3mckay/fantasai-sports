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
) -> dict:
    """Generate AI blurbs for the top N players in the given ranking mode.

    Args:
        db: SQLAlchemy session.
        api_key: Anthropic API key. If None or empty, returns immediately.
        mode: One of "season", "week", "month", "current".
        top_n: Number of top players to generate blurbs for.

    Returns:
        Dict with keys: generated, skipped, errors, mode.
    """
    if not api_key:
        _log.warning("blurb_scheduler: no API key set, skipping blurb generation")
        return {"generated": 0, "skipped": 0, "errors": 0, "mode": mode}

    try:
        import anthropic as _anthropic
    except ImportError:
        _log.warning("blurb_scheduler: anthropic package not installed, skipping")
        return {"generated": 0, "skipped": 0, "errors": 0, "mode": mode}

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
        # Build per player: prefer actual over projection
        for row in all_stat_rows:
            pid = row.player_id
            existing = raw_stats_map.get(pid)
            if existing is None or row.data_source == "actual":
                raw_stats_map[pid] = {
                    "counting": row.counting_stats or {},
                    "rate": row.rate_stats or {},
                    "advanced": row.advanced_stats or {},
                    "stat_type": row.stat_type or "batting",
                }
    except Exception:
        _log.warning("blurb_scheduler: could not bulk-fetch raw stats", exc_info=True)

    client = _anthropic.Anthropic(api_key=api_key)

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

    for player in top_players:
        # Skip MiLB prospects — they have PAV blurbs
        if getattr(player, "is_prospect", False):
            skipped += 1
            continue

        positions = getattr(player, "positions", []) or []
        contributions: dict = getattr(player, "category_contributions", {}) or {}

        # Top 4 category contributions by absolute z-score
        top_cats = sorted(contributions.items(), key=lambda x: abs(x[1]), reverse=True)[:4]
        cat_summary = ", ".join(
            f"{c} ({'+' if v > 0 else ''}{v:.1f}σ)" for c, v in top_cats
        )

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
        prev_rank     = None  # TODO: populate from RankingSnapshot history

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

            _outperformer_block = f"\n{outperformer_note}" if outperformer_note else ""

            prompt = (
                f"RANK #{player.overall_rank} — {player.name} "
                f"({full_team}, {'/'.join(positions)})\n\n"
                f"PLAYER FACTS (non-negotiable): {player.name} plays for the {full_team}. "
                f"Do not reference any other team.\n\n"
                + (f"ROLE NOTE: {_role_note}\n\n" if _role_note else "")
                + f"LENGTH: {length_target}\n\n"
                + _outperformer_block
                + ("\n\n" if outperformer_note else "")
                + f"KEY METRICS (with percentile context where shown):\n{key_stats}\n\n"
                f"CATEGORY SIGNALS: {cat_summary}\n\n"
                f"REQUIREMENTS:\n"
                f"- End with a forward-looking sentence (what to watch for)\n"
                f"- Call out specific categories this player helps or hurts\n"
                f"- If percentile data is shown, use the provided label (Elite/Above average/etc.) — "
                f"do not invent your own benchmark\n"
                f"- No hedging. Direct and specific.\n"
                f"- Note any meaningful xStat gap (regression warning or buying opportunity)"
            )

        # Top-50 players get 5–7 sentences — allow more tokens
        _max_tokens = 400 if player.overall_rank <= 50 else (280 if player.overall_rank <= 150 else 200)

        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=_max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            blurb_text = response.content[0].text.strip()
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

    _log.info(
        "blurb_scheduler: mode=%s generated=%d skipped=%d errors=%d",
        mode,
        generated,
        skipped,
        errors,
    )
    return {"generated": generated, "skipped": skipped, "errors": errors, "mode": mode}
