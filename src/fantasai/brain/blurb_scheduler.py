"""Scheduled blurb generation for player rankings.

Generates 2-3 sentence fantasy baseball analysis notes for the top N players
in each ranking mode, stored in the Ranking table for display on the Rankings page.

Called by the Monday 4am EST APScheduler job in main.py.
Also exposed via POST /rankings/generate-blurbs for on-demand generation.
"""
from __future__ import annotations

import logging
import time
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
    _PITCHER_METRICS = ["SwStr%", "CSW%", "K/9", "K9", "BB/9", "BB9", "BB%", "xERA", "xFIP", "SIERA", "WHIP", "GB%"]
    _BATTER_METRICS  = ["Barrel%", "HardHit%", "xwOBA", "xBA", "xSLG", "OBP", "AVG", "BB%", "K%"]

    def _fmt_metric(key: str, val: float) -> str:
        """Format a raw metric value for a prompt data block."""
        pct_keys = {"SwStr%", "CSW%", "BB%", "K%", "GB%", "HardHit%", "Barrel%"}
        if key in pct_keys:
            return f"{key}: {val:.1f}%"
        if key in {"xwOBA", "xBA", "xSLG", "OBP", "AVG"}:
            return f"{key}: {val:.3f}"
        return f"{key}: {val:.2f}"

    def _build_key_stats(player_id: int, stat_type: str) -> str:
        raw = raw_stats_map.get(player_id)
        if not raw:
            return "(stats not yet available this season)"
        rate = raw.get("rate", {})
        adv  = raw.get("advanced", {})
        keys = _PITCHER_METRICS if stat_type == "pitching" else _BATTER_METRICS
        parts = []
        for k in keys:
            v = rate.get(k) if rate.get(k) is not None else adv.get(k)
            if v is not None:
                try:
                    parts.append(_fmt_metric(k, float(v)))
                except (TypeError, ValueError):
                    pass
        return " | ".join(parts[:6]) if parts else "(stats not yet available this season)"

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

        # Key predictive metrics for the data block
        key_stats = _build_key_stats(player.player_id, stat_type)

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
            prompt = (
                f"Write a 2-sentence fantasy baseball note for {player.name} "
                f"({full_team}, {'/'.join(positions)}, rank #{player.overall_rank}).\n\n"
                f"PLAYER FACTS — 2026 live roster data, non-negotiable: "
                f"{player.name} currently plays for the {full_team}. "
                f"Do not name any other team as his current employer.\n\n"
                f"KEY METRICS:\n{key_stats}\n\n"
                f"CATEGORY SIGNALS: {cat_summary}\n\n"
                f"Focus on fantasy value and category impact. Cite at least one specific metric value. "
                f"Direct and specific. No hedging."
            )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=200,
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

            if existing:
                existing.blurb = blurb_text
                existing.overall_rank = player.overall_rank
                existing.score = player.score
                existing.category_contributions = contributions
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
