from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.engine.projection import ProjectionHorizon
from fantasai.schemas.ranking import PlayerRankingRead

router = APIRouter(prefix="/rankings", tags=["rankings"])

# Default 6x6 roto categories — used when no league context is available.
# Kept here (not just in recommendations.py) so the rankings page always uses
# a stable, predictable category set regardless of any per-league overrides.
RANKINGS_DEFAULT_CATEGORIES = [
    "R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP",
]

# Period label used when storing / looking up pre-generated blurbs.
CURRENT_PERIOD = "2025-season"


@router.get("", response_model=list[PlayerRankingRead])
def list_rankings(
    ranking_type: Optional[str] = Query(default="lookback", pattern="^(lookback|predictive)$"),
    season: int = Query(default=2025),
    horizon: str = Query(
        default="season",
        pattern="^(week|month|season)$",
        description="Projection horizon for predictive rankings: week, month, or season. Ignored for lookback.",
    ),
    position: Optional[str] = Query(default=None, description="Filter by position, e.g. 'OF'"),
    stat_type: Optional[str] = Query(default=None, pattern="^(batting|pitching)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list:
    """Compute and return player rankings from stored season stats.

    Rankings are computed on-demand from the most recent PlayerStats in the DB,
    using a 5-minute in-process cache shared with the analysis endpoints.
    Returns pre-generated blurbs from the Ranking table when available.

    The ``horizon`` parameter only affects predictive rankings:
    - ``week``  — projects ~26 PA / 6 IP; 35% talent signal
    - ``month`` — projects ~100 PA / 28 IP; 65% talent signal
    - ``season`` — projects full-season volume; 85% talent signal (default)
    """
    from fantasai.models.ranking import Ranking

    proj_horizon = ProjectionHorizon(horizon)

    # Re-use the shared cache for the current season (fast path).
    # For historical seasons the cache is keyed on 2025, so fall through to a
    # fresh uncached query — this path is only hit in tests / edge cases.
    _CACHED_SEASON = 2025
    if season == _CACHED_SEASON:
        from fantasai.api.v1.recommendations import _compute_rankings
        lookback, predictive = _compute_rankings(
            db, RANKINGS_DEFAULT_CATEGORIES, horizon=proj_horizon
        )
    else:
        from fantasai.adapters.base import NormalizedPlayerData
        from fantasai.adapters.mlb import MLBAdapter
        from fantasai.engine.scoring import ScoringEngine
        from fantasai.models.player import Player, PlayerStats

        stats_rows = db.query(PlayerStats).filter(
            PlayerStats.season == season,
            PlayerStats.stat_type.in_(["batting", "pitching"]),
        ).all()
        if not stats_rows:
            return []
        players = []
        for stats in stats_rows:
            player = db.get(Player, stats.player_id)
            if not player:
                continue
            players.append(NormalizedPlayerData(
                player_id=stats.player_id,
                name=player.name,
                team=player.team,
                positions=player.positions or [],
                stat_type=stats.stat_type,
                counting_stats=stats.counting_stats or {},
                rate_stats=stats.rate_stats or {},
                advanced_stats=stats.advanced_stats or {},
            ))
        if not players:
            return []
        adapter = MLBAdapter()
        eng = ScoringEngine(adapter, RANKINGS_DEFAULT_CATEGORIES)
        lookback_raw = eng.compute_lookback_rankings(season, players=players)
        predictive_raw = eng.compute_predictive_rankings(
            season, players=players, horizon=proj_horizon
        )

        def _dedup(rnks: list) -> list:
            seen: dict = {}
            for r in rnks:
                if r.player_id not in seen or r.score > seen[r.player_id].score:
                    seen[r.player_id] = r
            deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)
            for i, r in enumerate(deduped):
                r.overall_rank = i + 1
            return deduped

        lookback, predictive = _dedup(lookback_raw), _dedup(predictive_raw)

    rankings = predictive if ranking_type == "predictive" else lookback

    if not rankings:
        return []

    # Pull pre-generated blurbs from the Ranking table.
    # Keyed on (player_id, ranking_type, period); league_id=None = global blurbs.
    player_ids = [r.player_id for r in rankings]
    blurb_rows = (
        db.query(Ranking)
        .filter(
            Ranking.player_id.in_(player_ids),
            Ranking.ranking_type == ranking_type,
            Ranking.period == CURRENT_PERIOD,
            Ranking.league_id.is_(None),
        )
        .all()
    )
    blurb_map: dict[int, str] = {row.player_id: row.blurb for row in blurb_rows if row.blurb}

    # Apply filters
    if stat_type:
        rankings = [r for r in rankings if r.stat_type == stat_type]

    if position:
        pos = position.upper()
        rankings = [r for r in rankings if pos in r.positions]

    # Paginate
    rankings = rankings[offset: offset + limit]

    return [
        PlayerRankingRead(
            player_id=r.player_id,
            name=r.name,
            team=r.team,
            positions=r.positions,
            stat_type=r.stat_type,
            overall_rank=r.overall_rank,
            score=r.score,
            raw_score=r.raw_score,
            category_contributions=r.category_contributions,
            blurb=blurb_map.get(r.player_id),
        )
        for r in rankings
    ]


# ---------------------------------------------------------------------------
# Admin: projection sync
# ---------------------------------------------------------------------------


@router.post(
    "/sync-projections",
    tags=["admin"],
    summary="Ingest Steamer 2026 projections from FanGraphs",
)
def sync_projections(
    season: int = Query(default=2026, ge=2025, le=2030),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch Steamer projections for the given season and store them as
    PlayerStats rows (season=2026, stat_type=batting|pitching).

    Safe to re-run: upserts existing rows rather than duplicating them.
    Keeper evaluation will automatically prefer these projection rows over
    YTD actuals the next time the keeper-eval endpoint is called.

    Returns the number of rows upserted.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    from fantasai.engine.pipeline import sync_steamer_projections

    # Invalidate the projection rankings cache so the next keeper-eval call
    # sees the fresh data immediately.
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    stale_keys = [k for k in list(_RANKINGS_CACHE.keys()) if k.startswith("proj|")]
    for k in stale_keys:
        del _RANKINGS_CACHE[k]
    _logger.info("Invalidated %d stale projection cache entries", len(stale_keys))

    upserted = sync_steamer_projections(db, season=season)
    return {"season": season, "rows_upserted": upserted, "status": "ok"}


@router.post("/clear-cache")
def clear_rankings_cache() -> dict:
    """Bust the in-process rankings cache immediately.

    The next request to any endpoint that calls _compute_rankings will
    recompute from the DB (takes a few seconds).  Useful after a code deploy
    or data sync when you don't want to wait out the 30-minute TTL.
    """
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    n = len(_RANKINGS_CACHE)
    _RANKINGS_CACHE.clear()
    return {"cleared": n, "status": "ok"}
