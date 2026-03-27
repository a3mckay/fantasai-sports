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

    from fantasai.api.v1.rankings import CURRENT_PERIOD, RANKINGS_DEFAULT_CATEGORIES
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

    # For week mode, pre-fetch the schedule so we can enrich blurb prompts
    # with notable context (starts, park factors, Vegas odds, weather).
    week_player_schedules: dict = {}
    if mode == "week":
        try:
            week_start, week_end = get_current_week_bounds()
            week_player_schedules = fetch_weekly_schedule(week_start, week_end, db)
        except Exception:
            _log.warning("blurb_scheduler: could not fetch weekly schedule for blurb context", exc_info=True)

    client = _anthropic.Anthropic(api_key=api_key)

    generated = 0
    skipped = 0
    errors = 0

    for player in top_players:
        # Skip MiLB prospects — they have PAV blurbs
        if getattr(player, "is_prospect", False):
            skipped += 1
            continue

        positions = getattr(player, "positions", []) or []
        contributions: dict = getattr(player, "category_contributions", {}) or {}

        # Build prompt with top 3 category contributions by absolute z-score
        top_cats = sorted(contributions.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
        cat_summary = ", ".join(
            f"{c} ({'+' if v > 0 else ''}{v:.1f})" for c, v in top_cats
        )

        stat_type = getattr(player, "stat_type", "batting") or "batting"

        # Build week context note for notable schedule factors (SP starts, park, Vegas, weather)
        week_context_note: str | None = None
        if mode == "week" and week_player_schedules:
            ps = week_player_schedules.get(player.player_id)
            if ps is not None:
                week_context_note = build_player_week_context(
                    player.player_id, ps, stat_type, positions
                )

        full_team = _MLB_TEAM_NAMES.get(player.team, player.team)
        prompt = (
            f"Write a 2-sentence fantasy baseball note for {player.name} "
            f"({full_team}, {'/'.join(positions)}, rank #{player.overall_rank}).\n\n"
            f"PLAYER FACTS — 2026 live roster data, non-negotiable: "
            f"{player.name} currently plays for the {full_team}. "
            f"Do not name any other team as his current employer.\n\n"
            f"Category strengths: {cat_summary}\n\n"
            f"Focus on fantasy value and category impact. Direct and specific. No hedging."
        )

        if week_context_note:
            prompt += (
                f"\n\nThis week's schedule context: {week_context_note}\n\n"
                "If any of these factors are particularly notable, weave them into the note naturally."
            )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=150,
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
