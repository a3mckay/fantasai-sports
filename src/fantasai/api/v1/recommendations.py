"""Waiver recommendation API endpoints.

Fetches team/league data from DB, builds WaiverContext, calls the
Recommender, and returns results. The Recommender itself is pure
(no DB dependency).
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.brain.blurb_generator import get_blurb_generator
from fantasai.brain.recommender import BuildPreferences, Recommender, WaiverContext
from fantasai.brain.strategy import suggest_strategy
from fantasai.config import settings
from fantasai.engine.projection import ProjectionHorizon
from fantasai.engine.scoring import (
    ScoringEngine,
)
from fantasai.models.league import Team
from fantasai.models.player import PlayerRollingStats, PlayerStats
from fantasai.schemas.recommendation import (
    RecommendationRead,
    StrategySuggestionRead,
    WaiverRecommendationRead,
)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

# ---------------------------------------------------------------------------
# Rankings cache — avoid re-querying 935 PlayerStats on every request
# ---------------------------------------------------------------------------

_RANKINGS_CACHE: dict[str, tuple[float, tuple]] = {}
_RANKINGS_TTL = 1800  # 30 minutes — rankings change at most once per pipeline run


def _rankings_cache_key(categories: list[str], horizon: ProjectionHorizon) -> str:
    return f"{','.join(sorted(categories))}|{horizon.value}"


def _get_cached_rankings(
    categories: list[str], horizon: ProjectionHorizon
) -> tuple | None:
    key = _rankings_cache_key(categories, horizon)
    entry = _RANKINGS_CACHE.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _RANKINGS_TTL:
        del _RANKINGS_CACHE[key]
        return None
    return value


def _set_cached_rankings(
    categories: list[str], horizon: ProjectionHorizon, value: tuple
) -> None:
    _RANKINGS_CACHE[_rankings_cache_key(categories, horizon)] = (time.monotonic(), value)


# ---------------------------------------------------------------------------
# Helpers — shared DB → rankings pipeline
# ---------------------------------------------------------------------------


def _fetch_team_and_league(team_id: int, db: Session) -> tuple:
    """Fetch Team + League from DB or raise 404."""
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

    league = team.league
    if not league:
        raise HTTPException(status_code=404, detail="Team has no associated league")

    return team, league


def _compute_rankings(
    db: Session,
    categories: list[str],
    horizon: ProjectionHorizon = ProjectionHorizon.SEASON,
) -> tuple:
    """Compute lookback + predictive rankings from stored PlayerStats.

    Results are cached in-process for 5 minutes, keyed by both category set
    and horizon so that different horizon requests are cached independently.

    Returns (lookback, predictive) or ([], []) if no data.
    """
    cached = _get_cached_rankings(categories, horizon)
    if cached is not None:
        return cached

    from fantasai.adapters.base import NormalizedPlayerData
    from fantasai.adapters.mlb import MLBAdapter
    from fantasai.models.player import Player

    stats_rows = db.query(PlayerStats).filter(
        PlayerStats.season == 2025,
        PlayerStats.stat_type.in_(["batting", "pitching"]),
    ).all()

    if not stats_rows:
        return [], []

    # Batch-load all players in one query (avoids N+1 round trips)
    stat_player_ids = [s.player_id for s in stats_rows]
    player_map = {
        p.player_id: p
        for p in db.query(Player).filter(Player.player_id.in_(stat_player_ids)).all()
    }

    players = []
    for stats in stats_rows:
        player = player_map.get(stats.player_id)
        if not player:
            continue
        players.append(
            NormalizedPlayerData(
                player_id=stats.player_id,
                name=player.name,
                team=player.team,
                positions=player.positions or [],
                stat_type=stats.stat_type,
                counting_stats=stats.counting_stats or {},
                rate_stats=stats.rate_stats or {},
                advanced_stats=stats.advanced_stats or {},
            )
        )

    if not players:
        return [], []

    adapter = MLBAdapter()
    engine = ScoringEngine(adapter, categories)
    lookback = engine.compute_lookback_rankings(2025, players=players)
    predictive = engine.compute_predictive_rankings(2025, players=players, horizon=horizon)

    # Deduplicate: two-way players (e.g. Ohtani) have both batting and pitching
    # rows, producing two ranking entries with the same player_id. Keep the
    # higher-scoring entry and re-assign overall ranks.
    def _dedup(rnks: list) -> list:
        seen: dict = {}
        for r in rnks:
            if r.player_id not in seen or r.score > seen[r.player_id].score:
                seen[r.player_id] = r
        deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        for i, r in enumerate(deduped):
            r.overall_rank = i + 1
        return deduped

    result = (_dedup(lookback), _dedup(predictive))
    _set_cached_rankings(categories, horizon, result)
    return result


def _compute_projection_rankings(
    db: Session,
    categories: list[str],
    projection_season: int = 2026,
) -> list:
    """Compute lookback-style rankings from Steamer projection rows (season=2026).

    Used by keeper evaluation to produce forward-looking scores. Runs the
    standard lookback scorer on Steamer stat rows, which already represent
    full-season projections — no z-score blending needed.

    Falls back to an empty list if no projection rows are found for the given
    season (caller should then fall back to _compute_rankings predictive).

    Results are cached for 30 minutes under a separate key.
    """
    cache_key = f"proj|{','.join(sorted(categories))}|{projection_season}"
    entry = _RANKINGS_CACHE.get(cache_key)
    if entry is not None:
        ts, value = entry
        if time.monotonic() - ts <= _RANKINGS_TTL:
            return value

    from fantasai.adapters.base import NormalizedPlayerData
    from fantasai.adapters.mlb import MLBAdapter
    from fantasai.models.player import Player

    stats_rows = db.query(PlayerStats).filter(
        PlayerStats.season == projection_season,
        PlayerStats.stat_type.in_(["batting", "pitching"]),
        PlayerStats.week.is_(None),
    ).all()

    if not stats_rows:
        return []

    stat_player_ids = [s.player_id for s in stats_rows]
    player_map = {
        p.player_id: p
        for p in db.query(Player).filter(Player.player_id.in_(stat_player_ids)).all()
    }

    players = []
    for stats in stats_rows:
        player = player_map.get(stats.player_id)
        if not player:
            continue
        players.append(
            NormalizedPlayerData(
                player_id=stats.player_id,
                name=player.name,
                team=player.team,
                positions=player.positions or [],
                stat_type=stats.stat_type,
                counting_stats=stats.counting_stats or {},
                rate_stats=stats.rate_stats or {},
                advanced_stats=stats.advanced_stats or {},
            )
        )

    if not players:
        return []

    adapter = MLBAdapter()
    engine = ScoringEngine(adapter, categories)
    # Steamer rows are full-season projections — use the lookback scorer
    # (z-score on counting/rate stats) so ranking units match the actuals.
    rankings = engine.compute_lookback_rankings(projection_season, players=players)

    seen: dict = {}
    for r in rankings:
        if r.player_id not in seen or r.score > seen[r.player_id].score:
            seen[r.player_id] = r
    deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)
    for i, r in enumerate(deduped):
        r.overall_rank = i + 1

    _RANKINGS_CACHE[cache_key] = (time.monotonic(), deduped)
    return deduped


def _fetch_rolling_windows_map(
    db: Session,
    player_ids: list[int],
    season: int = 2025,
    windows: list[int] | None = None,
) -> dict[int, dict[str, dict[str, float]]]:
    """Build the rolling_windows_map for a set of player IDs.

    Returns {player_id: {"Last 14 days": {"HR": 4, "AVG": .301, ...}, ...}}
    Only includes windows where data exists for the player.
    """
    if windows is None:
        windows = [14, 30]  # most useful for blurb context; 7-day is noisy

    window_label = {14: "Last 14 days", 30: "Last 30 days", 60: "Last 60 days"}

    rows = (
        db.query(PlayerRollingStats)
        .filter(
            PlayerRollingStats.player_id.in_(player_ids),
            PlayerRollingStats.season == season,
            PlayerRollingStats.window_days.in_(windows),
        )
        .all()
    )

    result: dict[int, dict[str, dict[str, float]]] = {}
    for row in rows:
        label = window_label.get(row.window_days, f"Last {row.window_days} days")
        stats = {**row.counting_stats, **row.rate_stats}
        if row.window_rank:
            stats["rank"] = float(row.window_rank)
        result.setdefault(row.player_id, {})[label] = stats

    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{team_id}", response_model=list[RecommendationRead])
def get_recommendations(team_id: int) -> list:
    """Legacy endpoint — returns persisted recommendations."""
    return []


@router.get(
    "/{team_id}/waivers",
    response_model=list[WaiverRecommendationRead],
    summary="Get waiver wire recommendations for a team",
)
def get_waiver_recommendations(
    team_id: int,
    limit: int = Query(default=15, ge=1, le=50),
    pitcher_strategy: str = Query(default="balanced", pattern="^(balanced|rp_heavy|sp_heavy)$"),
    punt_positions: Optional[str] = Query(default=None, description="Comma-separated positions to punt, e.g. 'C,2B'"),
    punt_categories: Optional[str] = Query(default=None, description="Comma-separated categories to punt, e.g. 'SB,AVG'"),
    priority_targets: Optional[str] = Query(default=None, description="Comma-separated priority categories, e.g. 'SV,K'"),
    db: Session = Depends(get_db),
) -> list[WaiverRecommendationRead]:
    """Generate real-time waiver recommendations for a team.

    Fetches the team's roster and league config from the DB,
    computes fresh rankings, and runs the recommender algorithm.
    Optionally accepts build preferences via query parameters.
    """
    team, league = _fetch_team_and_league(team_id, db)

    # Gather all rostered player IDs across the league
    all_rostered: set[int] = set()
    for t in league.teams:
        all_rostered.update(t.roster or [])

    categories = league.scoring_categories or []

    # Build rankings from stored PlayerStats
    # TODO: cache rankings or compute on data refresh instead of per-request
    lookback, predictive = _compute_rankings(db, categories)
    if not lookback:
        return []

    # Parse build preferences from query params
    preferences = BuildPreferences(
        pitcher_strategy=pitcher_strategy,
        punt_positions=[p.strip() for p in punt_positions.split(",")] if punt_positions else [],
        punt_categories=[c.strip() for c in punt_categories.split(",")] if punt_categories else [],
        priority_targets=[t.strip() for t in priority_targets.split(",")] if priority_targets else [],
    )

    # Build pitcher IP map from the 14-day rolling window (most relevant for
    # weekly IP floor checks).  Falls back to an empty dict if no rolling data.
    roster_ids = team.roster or []
    team_pitcher_ip: dict[int, float] = {}
    if roster_ids:
        rolling_map = _fetch_rolling_windows_map(db, roster_ids, windows=[14])
        for pid, windows in rolling_map.items():
            ip = windows.get("Last 14 days", {}).get("IP")
            if ip is not None:
                team_pitcher_ip[pid] = float(ip)

    # Build context
    max_acq = (league.settings or {}).get("max_acquisitions_per_week", 4)
    context = WaiverContext(
        team_id=team_id,
        roster_player_ids=roster_ids,
        league_type=league.league_type,
        scoring_categories=categories,
        roster_positions=league.roster_positions or [],
        max_acquisitions_remaining=max_acq,
        all_rankings=lookback,
        predictive_rankings=predictive,
        all_rostered_ids=all_rostered,
        build_preferences=preferences,
        team_pitcher_ip=team_pitcher_ip,
    )

    # Run recommender
    recommender = Recommender(categories, league_type=league.league_type)
    recommendations = recommender.get_waiver_recommendations(context, limit=limit)

    # Generate LLM blurbs for each recommendation if an API key is configured.
    # Uses parallel requests so all blurbs complete in ~1–2 seconds total.
    blurbs: dict[int, str] = {}
    if settings.anthropic_api_key:
        try:
            # Build synthetic PlayerRanking objects for blurb generation
            # from the predictive rankings (forward-looking is more useful here).
            pred_by_id = {r.player_id: r for r in predictive}
            rec_rankings = [
                pred_by_id[r.player_id]
                for r in recommendations
                if r.player_id in pred_by_id
            ]
            if rec_rankings:
                gen = get_blurb_generator(api_key=settings.anthropic_api_key)
                # Fetch rolling window data for richer blurb context
                rec_ids = [r.player_id for r in rec_rankings]
                rolling_map = _fetch_rolling_windows_map(db, rec_ids)
                # Single-call: all blurbs in one request so the model
                # can vary language across the set (no repeated phrases).
                blurbs = gen.generate_blurbs_single_call(
                    rec_rankings,
                    ranking_type="predictive",
                    scoring_categories=categories,
                    rolling_windows_map=rolling_map or None,
                    top_n=0,  # 0 = generate for all provided
                )
        except Exception as exc:  # never block the response on blurb failure
            import logging
            logging.getLogger(__name__).warning("Blurb generation failed: %s", exc)

    # Convert to response schema
    return [
        WaiverRecommendationRead(
            player_id=r.player_id,
            player_name=r.player_name,
            team=r.team,
            positions=r.positions,
            priority_score=r.priority_score,
            category_impact=r.category_impact,
            fills_positions=r.fills_positions,
            weak_categories_addressed=r.weak_categories_addressed,
            drop_candidates=[
                {
                    "player_id": d.player_id,
                    "player_name": d.player_name,
                    "positions": d.positions,
                    "current_score": d.current_score,
                    "category_contributions": d.category_contributions,
                    "net_impact": d.net_impact,
                    "ip_warning": d.ip_warning,
                }
                for d in r.drop_candidates
            ],
            action=r.action,
            # LLM blurb takes priority; fall back to algorithmic rationale if unavailable.
            rationale_blurb=blurbs.get(r.player_id) or r.rationale_blurb,
        )
        for r in recommendations
    ]


@router.get(
    "/{team_id}/strategy",
    response_model=StrategySuggestionRead,
    summary="Get auto-detected build strategy suggestion for a team",
)
def get_strategy_suggestion(
    team_id: int,
    db: Session = Depends(get_db),
) -> StrategySuggestionRead:
    """Analyze a team's roster and suggest an optimal build strategy.

    Examines roster composition and category strengths to infer
    what build the manager appears to be running, then suggests
    preferences that align with and optimize that build.
    """
    team, league = _fetch_team_and_league(team_id, db)
    categories = league.scoring_categories or []

    lookback, _ = _compute_rankings(db, categories)
    if not lookback:
        raise HTTPException(
            status_code=404,
            detail="No player stats available to analyze strategy",
        )

    # Filter to just this team's players
    roster_ids = set(team.roster or [])
    roster_rankings = [r for r in lookback if r.player_id in roster_ids]

    if not roster_rankings:
        raise HTTPException(
            status_code=404,
            detail="No ranked players found on this team's roster",
        )

    # Run strategy suggester
    suggestion = suggest_strategy(
        roster_rankings=roster_rankings,
        scoring_categories=categories,
        roster_positions=league.roster_positions or [],
        league_type=league.league_type,
    )

    return StrategySuggestionRead(
        pitcher_strategy=suggestion.preferences.pitcher_strategy,
        punt_positions=suggestion.preferences.punt_positions,
        punt_categories=suggestion.preferences.punt_categories,
        priority_targets=suggestion.preferences.priority_targets,
        reasoning=suggestion.reasoning,
        confidence=suggestion.confidence,
    )
