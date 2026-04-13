from __future__ import annotations

import logging
import unicodedata
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.brain.injury_classifier import maybe_apply_classification
from fantasai.config import settings
from fantasai.engine.projection import ProjectionHorizon
from fantasai.schemas.ranking import PlayerRankingRead, RankingsResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rankings", tags=["rankings"])

# Default 6x6 roto categories — used when no league context is available.
# Kept here (not just in recommendations.py) so the rankings page always uses
# a stable, predictable category set regardless of any per-league overrides.
RANKINGS_DEFAULT_CATEGORIES = [
    "R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP",
]

# Period label used when storing / looking up pre-generated blurbs.
CURRENT_PERIOD = "2026-season"


@router.get("", response_model=RankingsResponse)
def list_rankings(
    ranking_type: Optional[str] = Query(default="lookback", pattern="^(lookback|predictive|current)$"),
    season: int = Query(default=2026),
    horizon: str = Query(
        default="season",
        pattern="^(week|month|season)$",
        description="Projection horizon for predictive rankings: week, month, or season. Ignored for lookback and current.",
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
    - ``week``  — projects ~26 PA / 6 IP; 100% talent signal (pure projections)
    - ``month`` — projects ~100 PA / 28 IP; 80% talent signal
    - ``season`` — projects Rest of Season volume; 50% talent signal (default)

    The ``current`` ranking_type returns YTD stats-based rankings (season=2026 actuals only).
    """
    from fantasai.models.ranking import Ranking

    proj_horizon = ProjectionHorizon(horizon)

    # Re-use the shared cache for the current season (fast path).
    # For historical seasons the cache is keyed on 2026, so fall through to a
    # fresh uncached query — this path is only hit in tests / edge cases.
    _CACHED_SEASON = 2026
    if ranking_type == "current" and season == _CACHED_SEASON:
        from fantasai.api.v1.recommendations import _compute_rankings
        current_rankings, _ = _compute_rankings(
            db, RANKINGS_DEFAULT_CATEGORIES, horizon=proj_horizon, ranking_type="current"
        )
        rankings = current_rankings

        if not rankings:
            return RankingsResponse(rankings=[])

        # Apply filters
        if stat_type:
            rankings = [r for r in rankings if r.stat_type == stat_type]
        if position:
            pos = position.upper()
            rankings = [r for r in rankings if pos in r.positions]

        rankings = rankings[offset: offset + limit]

        # Fetch pre-generated blurbs for current mode
        _current_blurb_map: dict[int, str] = {}
        _current_share_map: dict[int, str] = {}
        _current_blurbs_generated_at: Optional[str] = None
        try:
            _current_pids = [r.player_id for r in rankings]
            _current_blurb_rows = (
                db.query(Ranking)
                .filter(
                    Ranking.player_id.in_(_current_pids),
                    Ranking.ranking_type == "current",
                    Ranking.period == "2026-current",
                    Ranking.league_id.is_(None),
                )
                .all()
            )
            for _row in _current_blurb_rows:
                if _row.blurb:
                    _current_blurb_map[_row.player_id] = _row.blurb
                    _current_share_map[_row.player_id] = _row.share_token
            # Timestamp of the most recently generated current blurb.
            from sqlalchemy import func as _sqlfunc
            _current_ts = (
                db.query(_sqlfunc.max(Ranking.updated_at))
                .filter(
                    Ranking.ranking_type == "current",
                    Ranking.period == "2026-current",
                    Ranking.league_id.is_(None),
                )
                .scalar()
            )
            if _current_ts:
                _current_blurbs_generated_at = _current_ts.isoformat()
        except Exception:
            logger.warning("list_rankings: current blurb fetch failed (non-fatal)", exc_info=True)
            db.rollback()

        return RankingsResponse(
            rankings=[
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
                    blurb=_current_blurb_map.get(r.player_id),
                    injury_status=r.injury_status,
                    risk_flag=r.risk_flag,
                    risk_note=r.risk_note,
                    is_prospect=getattr(r, "is_prospect", False),
                    pav_score=getattr(r, "pav_score", None),
                )
                for r in rankings
            ],
            blurbs_generated_at=_current_blurbs_generated_at,
        )

    if season == _CACHED_SEASON:
        from fantasai.api.v1.recommendations import _compute_rankings, _get_cached_raw_rankings
        _compute_rankings(db, RANKINGS_DEFAULT_CATEGORIES, horizon=proj_horizon)
        raw = _get_cached_raw_rankings(RANKINGS_DEFAULT_CATEGORIES, proj_horizon)
        if raw is not None:
            lookback, predictive = raw
        else:
            from fantasai.api.v1.recommendations import _compute_rankings as _cr
            lookback, predictive = _cr(db, RANKINGS_DEFAULT_CATEGORIES, horizon=proj_horizon)
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
            return RankingsResponse(rankings=[])
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
            return RankingsResponse(rankings=[])
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
        return RankingsResponse(rankings=[])

    # Inject MiLB prospects at their PAV-equivalent rank.
    # Only inject when no position / stat_type filter has been applied yet
    # (we filter after injection so the position filter still works on prospects).
    if season == _CACHED_SEASON:
        from fantasai.api.v1.recommendations import _inject_prospect_rankings
        rankings = list(_inject_prospect_rankings(rankings, db))

    # Pull pre-generated blurbs from the Ranking table.
    # Keyed on (player_id, ranking_type, period); league_id=None = global blurbs.
    # Each mode (season/week/month/current) stores blurbs under its own period
    # string so they never overwrite each other.
    _BLURB_PERIOD_MAP: dict[str, str] = {
        "season":  "2026-season",
        "week":    "2026-week",
        "month":   "2026-month",
        "current": "2026-current",
    }
    if ranking_type == "predictive":
        blurb_period = _BLURB_PERIOD_MAP.get(horizon, CURRENT_PERIOD)
    else:
        blurb_period = _BLURB_PERIOD_MAP.get("current", CURRENT_PERIOD)

    player_ids = [r.player_id for r in rankings]
    blurb_map: dict[int, str] = {}
    share_token_map: dict[int, str] = {}
    try:
        blurb_rows = (
            db.query(Ranking)
            .filter(
                Ranking.player_id.in_(player_ids),
                Ranking.ranking_type.in_([ranking_type, "pav"]),
                Ranking.period == blurb_period,
                Ranking.league_id.is_(None),
            )
            .all()
        )
        # PAV blurbs take priority for prospects; ranking-type blurbs for MLB players.
        # share_token travels with the blurb — always update both from the same row.
        for row in blurb_rows:
            if row.blurb:
                if row.ranking_type == "pav" or row.player_id not in blurb_map:
                    blurb_map[row.player_id] = row.blurb
                    share_token_map[row.player_id] = row.share_token
    except Exception:
        # Blurb/share_token fetch is non-critical — rankings still work without it.
        # This can happen if the share_token migration hasn't run yet.
        logger.warning("list_rankings: blurb fetch failed (non-fatal)", exc_info=True)
        db.rollback()

    # Timestamp of the most recently generated blurb for this mode/period.
    blurbs_generated_at: Optional[str] = None
    try:
        from sqlalchemy import func as _sqlfunc
        _ts = (
            db.query(_sqlfunc.max(Ranking.updated_at))
            .filter(
                Ranking.ranking_type.in_([ranking_type, "pav"]),
                Ranking.period == blurb_period,
                Ranking.league_id.is_(None),
            )
            .scalar()
        )
        if _ts:
            blurbs_generated_at = _ts.isoformat()
    except Exception:
        logger.debug("blurbs_generated_at query failed (non-fatal)", exc_info=True)

    # Apply filters
    if stat_type:
        rankings = [r for r in rankings if r.stat_type == stat_type]

    if position:
        pos = position.upper()
        rankings = [r for r in rankings if pos in r.positions]

    # Look up previous ranking snapshots to compute rank_delta.
    # Projected modes: compare against 7 days ago; current: 1 day ago.
    rank_delta_map: dict[int, Optional[int]] = {}
    try:
        from datetime import date as _date, timedelta as _timedelta
        from fantasai.models.ranking import RankingSnapshot

        snap_horizon = horizon if ranking_type == "predictive" else "current"
        snap_type = ranking_type if ranking_type != "lookback" else "predictive"
        lookback_days = 1 if ranking_type == "current" else 7
        compare_date = _date.today() - _timedelta(days=lookback_days)

        paginated_ids = [r.player_id for r in rankings[offset: offset + limit]]
        prev_snaps = (
            db.query(RankingSnapshot)
            .filter(
                RankingSnapshot.player_id.in_(paginated_ids),
                RankingSnapshot.ranking_type == snap_type,
                RankingSnapshot.horizon == snap_horizon,
                RankingSnapshot.snapshot_date == compare_date,
            )
            .all()
        )
        prev_rank_map = {s.player_id: s.overall_rank for s in prev_snaps}
        for r in rankings[offset: offset + limit]:
            prev = prev_rank_map.get(r.player_id)
            if prev is not None:
                # Positive delta = moved up (lower rank number = better)
                rank_delta_map[r.player_id] = prev - r.overall_rank
    except Exception:
        logger.debug("rank_delta computation failed (non-fatal)", exc_info=True)

    # Paginate
    rankings = rankings[offset: offset + limit]

    return RankingsResponse(
        rankings=[
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
                injury_status=r.injury_status,
                risk_flag=r.risk_flag,
                risk_note=r.risk_note,
                is_prospect=getattr(r, "is_prospect", False),
                pav_score=getattr(r, "pav_score", None),
                rank_delta=rank_delta_map.get(r.player_id),
                share_token=share_token_map.get(r.player_id),
            )
            for r in rankings
        ],
        blurbs_generated_at=blurbs_generated_at,
    )


# ---------------------------------------------------------------------------
# Week-mode state (set by scheduler in main.py)
# ---------------------------------------------------------------------------

# Module-level flag — True means show next week's rankings as "This Week".
# Flipped by the Thursday midnight scheduler job; reset Monday 4am EST.
SHOW_NEXT_WEEK: bool = False


@router.get("/week-mode", tags=["rankings"])
def get_week_mode() -> dict:
    """Return the current This Week display mode.

    Returns:
        show_next_week: bool — True from Thursday midnight to Monday 4am EST.
        current_week_start: ISO date string of the start of the current week (Monday).
        next_week_start: ISO date string of the start of next week.
    """
    from datetime import date, timedelta

    today = date.today()
    # Find last Monday (weekday 0)
    days_since_monday = today.weekday()
    current_week_start = today - timedelta(days=days_since_monday)
    next_week_start = current_week_start + timedelta(weeks=1)

    return {
        "show_next_week": SHOW_NEXT_WEEK,
        "current_week_start": current_week_start.isoformat(),
        "next_week_start": next_week_start.isoformat(),
    }


# ---------------------------------------------------------------------------
# Admin: cache management
# ---------------------------------------------------------------------------


@router.post("/clear-cache", tags=["admin"])
def clear_rankings_cache() -> dict:
    """Clear the in-process rankings cache, forcing a fresh compute on next request."""
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE, _RANKINGS_RAW_CACHE
    n = len(_RANKINGS_CACHE)
    _RANKINGS_CACHE.clear()
    _RANKINGS_RAW_CACHE.clear()
    return {"cleared": n, "status": "ok"}


@router.post("/sync-statcast-now", tags=["admin"])
def sync_statcast_now(
    season: int = Query(default=2026, ge=2020, le=2030),
    sample_player_id: int = Query(default=15640, description="Player ID to spot-check after sync (default: Aaron Judge)"),
    db: Session = Depends(get_db),
) -> dict:
    """Run Statcast advanced stats sync synchronously and return detailed diagnostics.

    Unlike /sync-fangraphs (background), this runs inline so errors surface immediately.
    Returns a report showing how many rows were updated and a before/after spot-check
    for the given sample_player_id.

    Default sample: Aaron Judge (player_id=15640, mlbam=592450).
    """
    import traceback
    from fantasai.models.player import Player, PlayerStats

    result: dict = {"season": season, "status": "ok", "errors": []}

    # Before snapshot
    before_stats: dict = {}
    try:
        ps_before = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == sample_player_id,
                PlayerStats.season == season,
                PlayerStats.stat_type == "batting",
                PlayerStats.data_source == "actual",
            )
            .first()
        )
        if ps_before:
            before_stats = dict(ps_before.advanced_stats or {})
    except Exception as e:
        result["errors"].append(f"Before snapshot error: {e}")

    # Check mlbam_id mapping
    mlbam_count = db.query(Player).filter(Player.mlbam_id.isnot(None)).count()
    sample_player = db.get(Player, sample_player_id)
    result["players_with_mlbam_id"] = mlbam_count
    result["sample_player"] = {
        "player_id": sample_player_id,
        "name": sample_player.name if sample_player else "NOT FOUND",
        "mlbam_id": sample_player.mlbam_id if sample_player else None,
    }

    # Run sync
    rows_updated = 0
    try:
        from fantasai.engine.pipeline import sync_statcast_advanced_stats
        rows_updated = sync_statcast_advanced_stats(db, season=season)
    except Exception as e:
        result["errors"].append(f"Sync error: {traceback.format_exc()}")
        result["status"] = "error"

    result["rows_updated"] = rows_updated

    # After snapshot — re-query fresh
    db.expire_all()
    after_stats: dict = {}
    try:
        ps_after = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == sample_player_id,
                PlayerStats.season == season,
                PlayerStats.stat_type == "batting",
                PlayerStats.data_source == "actual",
            )
            .first()
        )
        if ps_after:
            after_stats = dict(ps_after.advanced_stats or {})
    except Exception as e:
        result["errors"].append(f"After snapshot error: {e}")

    result["advanced_stats_before"] = before_stats
    result["advanced_stats_after"] = after_stats
    result["advanced_stats_changed"] = before_stats != after_stats

    # Clear cache
    try:
        from fantasai.api.v1.recommendations import _RANKINGS_CACHE, _RANKINGS_RAW_CACHE
        _RANKINGS_CACHE.clear()
        _RANKINGS_RAW_CACHE.clear()
    except Exception:
        pass

    return result


@router.post("/sync-current-stats", tags=["admin"])
def sync_current_stats(
    season: int = Query(default=2026, ge=2020, le=2030),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch current-season YTD stats from MLB Stats API and upsert into player_stats.

    Runs the same logic as the nightly APScheduler job.  Call this once after
    deployment to backfill the current 2026 season, or any time you need fresh
    data immediately without waiting for the scheduled run.

    NOTE: This endpoint only updates counting/rate stats from the MLB Stats API.
    It does NOT populate advanced stats (xwOBA, Barrel%, xERA, Stuff+ etc.) — use
    /sync-fangraphs for that.

    Clears the rankings cache so the next request reflects the new stats.
    Returns the number of rows upserted.
    """
    from fantasai.engine.pipeline import sync_mlb_api_current_season

    rows = sync_mlb_api_current_season(db, season=season)

    # Bust cache so the updated data surfaces immediately
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE, _RANKINGS_RAW_CACHE
    _RANKINGS_CACHE.clear()
    _RANKINGS_RAW_CACHE.clear()

    return {"season": season, "rows_upserted": rows, "status": "ok"}


@router.post("/sync-fangraphs", tags=["admin"])
def sync_fangraphs(
    background_tasks: BackgroundTasks,
    season: int = Query(default=2026, ge=2020, le=2030),
) -> dict:
    """Fetch advanced stats from Statcast (Baseball Savant) + FanGraphs and upsert.

    Primary source: Baseball Savant (Statcast) — always accessible, provides
    xwOBA, xBA, xSLG, Barrel%, HardHit%, EV (batters) and xERA, Barrel%,
    HardHit%, EV (pitchers).

    Secondary source: FanGraphs via pybaseball — adds wRC+, SIERA, Stuff+,
    CSW%, SwStr% if accessible (may return 403 if FanGraphs is blocking).

    Use this to repair or refresh advanced stats without a full three-step refresh.
    Runs in the background. Returns 202 immediately — check logs for progress.
    """
    from fantasai.database import SessionLocal
    from fantasai.engine.pipeline import sync_current_season_stats, sync_statcast_advanced_stats

    def _run() -> None:
        db = SessionLocal()
        try:
            # Primary: Statcast (always works)
            sc_count = sync_statcast_advanced_stats(db, season=season)
            logger.info("sync-fangraphs: Statcast updated %d rows for season %s", sc_count, season)

            # Secondary: FanGraphs (adds wRC+, SIERA, Stuff+ if accessible)
            try:
                fg_count = sync_current_season_stats(db, season=season)
                logger.info("sync-fangraphs: FanGraphs upserted %d rows for season %s", fg_count, season)
            except Exception:
                logger.warning("sync-fangraphs: FanGraphs unavailable (likely 403), Statcast stats retained", exc_info=True)

            from fantasai.api.v1.recommendations import _RANKINGS_CACHE, _RANKINGS_RAW_CACHE
            _RANKINGS_CACHE.clear()
            _RANKINGS_RAW_CACHE.clear()
            logger.info("sync-fangraphs: rankings cache cleared")
        except Exception:
            logger.error("sync-fangraphs: failed", exc_info=True)
            db.rollback()
        finally:
            db.close()

    background_tasks.add_task(_run)
    return {
        "status": "accepted",
        "season": season,
        "message": "Advanced stats sync running in background (Statcast primary, FanGraphs optional). "
                   "Check logs for progress. xwOBA, Barrel%, xERA etc. will be refreshed.",
    }


@router.post("/force-full-refresh", status_code=202, tags=["admin"])
def force_full_refresh(
    background_tasks: BackgroundTasks,
    season: int = Query(default=2026, ge=2020, le=2030),
) -> dict:
    """Run the full nightly stats refresh immediately in the background.

    Runs all four syncs in order:
      1. Steamer/consensus projections (picks up new callups)
      2. MLB Stats API actuals (counting/rate stats)
      3. Statcast / Baseball Savant advanced stats (xwOBA, Barrel%, xERA, etc.)
      4. FanGraphs actuals (adds wRC+, SIERA, Stuff+ if accessible)

    Clears rankings cache after completion. Returns 202 immediately — check
    server logs for progress. Typically takes 2–5 minutes.
    """
    from fantasai.database import SessionLocal
    from fantasai.engine.pipeline import (
        sync_current_season_stats,
        sync_mlb_api_current_season,
        sync_statcast_advanced_stats,
        sync_steamer_projections,
    )

    def _run() -> None:
        db = SessionLocal()
        try:
            try:
                proj_count = sync_steamer_projections(db, season=season)
                logger.info("force-full-refresh: Steamer upserted %d rows", proj_count)
            except Exception:
                logger.warning("force-full-refresh: Steamer sync failed", exc_info=True)

            mlb_count = sync_mlb_api_current_season(db, season=season)
            logger.info("force-full-refresh: MLB API upserted %d rows", mlb_count)

            try:
                sc_count = sync_statcast_advanced_stats(db, season=season)
                logger.info("force-full-refresh: Statcast advanced stats updated %d rows", sc_count)
            except Exception:
                logger.warning("force-full-refresh: Statcast sync failed", exc_info=True)

            try:
                fg_count = sync_current_season_stats(db, season=season)
                logger.info("force-full-refresh: FanGraphs upserted %d rows", fg_count)
            except Exception:
                logger.warning("force-full-refresh: FanGraphs sync failed (likely 403), Statcast stats retained", exc_info=True)

            from fantasai.api.v1.recommendations import _RANKINGS_CACHE, _RANKINGS_RAW_CACHE
            _RANKINGS_CACHE.clear()
            _RANKINGS_RAW_CACHE.clear()
            logger.info("force-full-refresh: rankings cache cleared")
        except Exception:
            logger.error("force-full-refresh failed", exc_info=True)
            db.rollback()
        finally:
            db.close()

    background_tasks.add_task(_run)
    return {"status": "accepted", "season": season, "message": "Full refresh running in background — check logs for progress"}


@router.post("/generate-blurbs", status_code=202, tags=["admin"])
def generate_blurbs(
    background_tasks: BackgroundTasks,
    mode: str = Query(default="season", pattern="^(week|month|season|current)$"),
    top_n: int = Query(default=300, ge=10, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """Kick off blurb generation in the background and return 202 immediately.

    Generation runs asynchronously — check server logs for progress.
    Runs the same logic as the Monday 4am scheduled job.
    mode: "season" (ROS), "week", "month", "current" (YTD)
    """
    from fantasai.brain.blurb_scheduler import generate_rankings_blurbs
    from fantasai.database import SessionLocal

    api_key = settings.anthropic_api_key

    def _run() -> None:
        bg_db = SessionLocal()
        try:
            result = generate_rankings_blurbs(bg_db, api_key, mode=mode, top_n=top_n)
            logger.info("generate-blurbs background task complete: %s", result)
        finally:
            bg_db.close()

    background_tasks.add_task(_run)
    return {"status": "accepted", "mode": mode, "top_n": top_n}


@router.post("/submit-blurb-batch", status_code=202, tags=["admin"])
def submit_blurb_batch(
    background_tasks: BackgroundTasks,
    mode: str = Query(default="season", pattern="^(week|month|season|current)$"),
    top_n: int = Query(default=300, ge=10, le=500),
    db: Session = Depends(get_db),
) -> dict:
    """Submit blurb generation for a ranking mode to the Anthropic Batches API.

    ~50% cheaper than synchronous generation but async — results available
    within minutes to ~1 hour.  Call POST /rankings/collect-blurb-batches
    to write results to the DB once the batch completes.
    """
    from fantasai.brain.blurb_scheduler import submit_rankings_blurbs_batch
    from fantasai.database import SessionLocal

    api_key = settings.anthropic_api_key

    def _run() -> None:
        bg_db = SessionLocal()
        try:
            result = submit_rankings_blurbs_batch(bg_db, api_key, mode=mode, top_n=top_n)
            logger.info("submit-blurb-batch background task complete: %s", result)
        finally:
            bg_db.close()

    background_tasks.add_task(_run)
    return {"status": "accepted", "mode": mode, "top_n": top_n}


@router.post("/collect-blurb-batches", tags=["admin"])
def collect_blurb_batches(db: Session = Depends(get_db)) -> dict:
    """Check all pending blurb batches and write any completed results to the DB.

    Safe to call repeatedly — batches not yet complete are silently skipped.
    Returns counts of batches checked, collected, blurbs written, and errors.
    """
    from fantasai.brain.blurb_scheduler import collect_rankings_blurb_batches

    result = collect_rankings_blurb_batches(db, settings.anthropic_api_key)

    # Bust cache so updated blurbs appear immediately
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    _RANKINGS_CACHE.clear()

    return result


# ---------------------------------------------------------------------------
# Admin: prospect sync
# ---------------------------------------------------------------------------


@router.post("/sync-prospects", tags=["admin"])
def sync_prospects(
    season: int = Query(default=2026),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch MiLB stats from the MLB Stats API, compute PAV scores, and
    upsert ProspectProfile rows for all tracked minor-league prospects.

    Also generates AI blurbs via the Anthropic API (requires ANTHROPIC_API_KEY).

    Runs in two passes so the implied pipeline_rank (based on PAV order)
    feeds back into the prospect grade calculation for a better final score.
    """
    from fantasai.engine.prospect_pipeline import sync_prospect_data
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE

    result = sync_prospect_data(db, season=season, api_key=settings.anthropic_api_key)
    # Bust the rankings cache so injected prospects appear on the next request
    _RANKINGS_CACHE.clear()
    return result


# ---------------------------------------------------------------------------
# Admin: injury management
# ---------------------------------------------------------------------------


@router.post("/sync-mlbam-ids", tags=["admin"])
def sync_mlbam_ids(db: Session = Depends(get_db)) -> dict:
    """Backfill Player.mlbam_id for all players using the Chadwick Bureau register.

    Required before sync-injuries will work: the injury sync cross-references
    MLB Stats API players by MLBAM ID. Run this once after ingesting stats,
    then re-run sync-injuries to pick up IL data.

    Safe to call repeatedly — only updates rows where mlbam_id is currently NULL.
    """
    from fantasai.engine.pipeline import backfill_mlbam_ids
    updated = backfill_mlbam_ids(db)
    return {"updated": updated, "status": "ok"}


@router.post("/sync-injuries", tags=["admin"])
def sync_injuries(db: Session = Depends(get_db)) -> dict:
    """Fetch current MLB IL data from the MLB Stats API and upsert into injury_records.

    Iterates all 30 MLB teams, fetches each team's injured-list roster, and
    cross-references players by mlbam_id.  Auto-runs the MLBAM ID backfill
    first if any players are missing their mlbam_id.
    """
    import httpx
    from datetime import datetime, timezone
    from fantasai.models.player import InjuryRecord, Player

    # Auto-backfill missing MLBAM IDs so the sync can match players
    from fantasai.engine.pipeline import backfill_mlbam_ids
    backfilled = backfill_mlbam_ids(db)
    if backfilled:
        logger.info("sync-injuries: backfilled %d MLBAM IDs before sync", backfilled)

    synced = 0
    not_found = 0
    errors: list[str] = []

    try:
        teams_resp = httpx.get(
            "https://statsapi.mlb.com/api/v1/teams",
            params={"sportId": 1, "season": 2026},
            timeout=15.0,
        )
        teams_resp.raise_for_status()
        teams = teams_resp.json().get("teams", [])
    except Exception as exc:
        return {"error": f"Failed to fetch teams: {exc}", "synced": 0}

    # Build mlbam_id → player_id lookup (batch)
    all_players = db.query(Player).filter(Player.mlbam_id.isnot(None)).all()
    mlbam_map: dict[int, int] = {p.mlbam_id: p.player_id for p in all_players}  # type: ignore[index]

    now_utc = datetime.now(timezone.utc)

    for team in teams:
        team_id = team.get("id")
        if not team_id:
            continue
        try:
            # Use the 40Man roster (not "injuredList" which returns Active players too).
            # Filter server-side by status codes: D10=10-day IL, D15=15-day, D60=60-day,
            # DTD=day-to-day.  Active players have status code "A" and are skipped.
            il_resp = httpx.get(
                f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                params={"rosterType": "40Man", "season": 2026},
                timeout=10.0,
            )
            il_resp.raise_for_status()
            roster = il_resp.json().get("roster", [])
        except Exception as exc:
            errors.append(f"team {team_id}: {exc}")
            continue

        # Status codes that represent an actual IL placement or day-to-day status.
        _IL_CODES = {"D10", "D15", "D60", "DTD", "DL10", "DL15", "DL60"}

        for entry in roster:
            mlbam_id = entry.get("person", {}).get("id")
            if not mlbam_id:
                continue

            status_code = entry.get("status", {}).get("code", "")

            # Skip active players (code "A") and any non-IL status.
            if status_code not in _IL_CODES:
                continue

            player_id = mlbam_map.get(mlbam_id)
            if not player_id:
                not_found += 1
                continue

            if "60" in status_code:
                status = "il_60"
            elif status_code == "DTD":
                status = "day_to_day"
            else:
                status = "il_10"

            # Only store meaningful notes — don't fall back to a bare status string.
            injury_note = (
                entry.get("note")
                or entry.get("injuryDescription")
                or None
            )

            existing = db.query(InjuryRecord).filter(
                InjuryRecord.player_id == player_id
            ).first()
            if existing:
                existing.status = status
                existing.injury_description = injury_note
                existing.fetched_at = now_utc
            else:
                db.add(InjuryRecord(
                    player_id=player_id,
                    status=status,
                    injury_description=injury_note,
                    fetched_at=now_utc,
                ))

            # Auto-classify severity and set risk_flag on the Player row.
            # Only call when there's an actual note — avoids 900+ Claude API
            # calls on spring training rosters with no injury descriptions.
            # "fragile" is never overwritten (manual-only flag).
            if injury_note:
                player_obj = db.get(Player, player_id)
                if player_obj:
                    maybe_apply_classification(
                        player=player_obj,
                        description=injury_note,
                        il_status=status,
                        api_key=settings.anthropic_api_key,
                    )

            synced += 1

    db.commit()

    # Bust the rankings cache so changes take effect immediately
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    _RANKINGS_CACHE.clear()

    return {
        "synced": synced,
        "not_found_in_db": not_found,
        "team_errors": errors,
        "status": "ok",
    }


class InjuryOverrideBody(BaseModel):
    """Request body for manually setting a player's current injury status.

    Provide either ``player_id`` (FanGraphs IDfg) or ``player_name`` (partial
    match, case-insensitive).  ``player_id`` takes precedence when both are given.
    """
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    status: str  # "il_10" | "il_60" | "day_to_day" | "out_for_season" | "active"
    return_date: Optional[str] = None   # ISO date string: "2026-07-01"
    injury_description: Optional[str] = None


class RiskFlagBody(BaseModel):
    """Request body for setting a player's chronic risk flag.

    Provide either ``player_id`` (FanGraphs IDfg) or ``player_name`` (partial
    match, case-insensitive).  ``player_id`` takes precedence when both are given.
    """
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    risk_flag: Optional[str] = None   # "fragile" | "recent_surgery" | null to clear
    risk_note: Optional[str] = None


def _fold_name(text: str) -> str:
    """Strip diacritics and lowercase for accent-insensitive name matching.

    Converts "José Ramírez" → "jose ramirez" so that typing without accents
    still finds the correct player.
    """
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()


def _resolve_player(db: Session, player_id: Optional[int], player_name: Optional[str]):
    """Return a Player ORM object from either player_id or a name search.

    Name search is accent- and case-insensitive substring match — returns the
    best single match or raises 404/422 if the name is ambiguous or not found.
    """
    from sqlalchemy import func as sqlfunc

    from fantasai.models.player import Player

    if player_id is not None:
        player = db.get(Player, player_id)
        if not player:
            raise HTTPException(status_code=404, detail=f"Player {player_id} not found")
        return player

    if player_name:
        norm = _fold_name(player_name.strip())

        # Try DB-level unaccent() (PostgreSQL); fall back to Python filtering.
        rows: list[Player] = []
        try:
            rows = (
                db.query(Player)
                .filter(sqlfunc.unaccent(sqlfunc.lower(Player.name)).contains(norm))
                .limit(10)
                .all()
            )
        except Exception:
            # unaccent() not available (SQLite/extension missing) — Python fallback.
            candidates = db.query(Player).limit(5000).all()
            rows = [r for r in candidates if norm in _fold_name(r.name)][:10]

        if not rows:
            raise HTTPException(status_code=404, detail=f"No player found matching '{player_name}'")
        if len(rows) == 1:
            return rows[0]
        # Prefer an exact accent-folded match when multiple partial hits exist.
        exact = [r for r in rows if _fold_name(r.name) == norm]
        if len(exact) == 1:
            return exact[0]
        names = ", ".join(r.name for r in rows[:5])
        raise HTTPException(
            status_code=422,
            detail=f"Ambiguous name '{player_name}' — matched: {names}. Use player_id instead.",
        )

    raise HTTPException(status_code=422, detail="Provide either player_id or player_name.")


@router.post("/set-injury", tags=["admin"])
def set_injury(body: InjuryOverrideBody, db: Session = Depends(get_db)) -> dict:
    """Manually set or clear a player's current injury status.

    Use this for spring-training injuries (not yet in the MLB Stats API IL),
    or to set precise return dates that the API doesn't provide.
    Setting status="active" removes the injury record entirely.

    Pass either ``player_id`` (FanGraphs IDfg) or ``player_name`` (e.g. "Tyler Glasnow").
    """
    from datetime import date as _date, datetime, timezone
    from fantasai.models.player import InjuryRecord

    player = _resolve_player(db, body.player_id, body.player_name)

    pid = player.player_id

    if body.status == "active":
        # Clear any existing injury record and reset auto-classified risk flag.
        db.query(InjuryRecord).filter(InjuryRecord.player_id == pid).delete()
        # Only clear auto-classified flags — preserve manual "fragile" flag.
        if player.risk_flag != "fragile":
            player.risk_flag = None
            player.risk_note = None
    else:
        return_date = None
        if body.return_date:
            try:
                return_date = _date.fromisoformat(body.return_date)
            except ValueError:
                raise HTTPException(status_code=422, detail="return_date must be ISO format: YYYY-MM-DD")

        existing = db.query(InjuryRecord).filter(InjuryRecord.player_id == pid).first()
        if existing:
            existing.status = body.status
            existing.return_date = return_date
            existing.injury_description = body.injury_description
            existing.fetched_at = datetime.now(timezone.utc)
        else:
            db.add(InjuryRecord(
                player_id=pid,
                status=body.status,
                return_date=return_date,
                injury_description=body.injury_description,
                fetched_at=datetime.now(timezone.utc),
            ))

        # Auto-classify severity when a description is provided.
        # Always re-runs so manual overrides with updated descriptions work correctly.
        # "fragile" is never overwritten (manual-only flag).
        if body.injury_description:
            maybe_apply_classification(
                player=player,
                description=body.injury_description,
                il_status=body.status,
                api_key=settings.anthropic_api_key,
            )

    db.commit()

    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    _RANKINGS_CACHE.clear()

    return {
        "player_id": pid,
        "name": player.name,
        "status": body.status,
        "risk_flag": player.risk_flag,
        "risk_note": player.risk_note,
        "ok": True,
    }


@router.post("/set-risk-flag", tags=["admin"])
def set_risk_flag(body: RiskFlagBody, db: Session = Depends(get_db)) -> dict:
    """Set or clear a player's chronic injury risk flag.

    risk_flag values:
      "fragile"        — chronically injury-prone: 0.70× PA/IP (Glasnow, Seager)
      "recent_surgery" — post-surgery risk: 0.80× PA/IP (Wheeler)
      null / ""        — clear the flag (player is healthy profile)

    Pass either ``player_id`` (FanGraphs IDfg) or ``player_name`` (e.g. "Tyler Glasnow").
    """
    player = _resolve_player(db, body.player_id, body.player_name)

    player.risk_flag = body.risk_flag or None
    player.risk_note = body.risk_note or None
    db.commit()

    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    _RANKINGS_CACHE.clear()

    return {
        "player_id": player.player_id,
        "name": player.name,
        "risk_flag": player.risk_flag,
        "risk_note": player.risk_note,
        "ok": True,
    }


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


# ---------------------------------------------------------------------------
# Public share endpoint — no auth required
# ---------------------------------------------------------------------------


@router.get("/share/{share_token}", include_in_schema=False)
def share_blurb_card(share_token: str, db: Session = Depends(get_db)):
    """Public endpoint — returns the blurb card PNG for the given share token.

    No authentication required so the image can be embedded in social media
    previews and shared directly between users.
    """
    import os
    from fastapi.responses import FileResponse
    from fantasai.models.ranking import Ranking
    from fantasai.models.player import Player
    from fantasai.brain.grade_card import render_blurb_card

    row = (
        db.query(Ranking)
        .filter(Ranking.share_token == share_token)
        .first()
    )
    if not row or not row.blurb:
        raise HTTPException(status_code=404, detail="Not found")

    player = db.get(Player, row.player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    # render_blurb_card caches in /tmp — serve from cache if already rendered
    card_path = f"/tmp/grade_cards/blurb_{row.player_id}_{share_token[:8]}.png"
    if not os.path.exists(card_path):
        card_path = render_blurb_card(
            player_id=row.player_id,
            player_name=player.name,
            team=player.team or "",
            positions=player.positions or [],
            overall_rank=row.overall_rank,
            score=row.score,
            blurb=row.blurb,
            share_token=share_token,
            mlbam_id=getattr(player, "mlbam_id", None),
        )

    if not card_path or not os.path.exists(card_path):
        raise HTTPException(status_code=500, detail="Card generation failed")

    safe_name = player.name.replace(" ", "_").replace("/", "_")
    return FileResponse(
        card_path,
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{safe_name}_blurb.png"',
            "Cache-Control": "public, max-age=3600",
        },
    )


# ---------------------------------------------------------------------------
# Admin: sync player handedness
# ---------------------------------------------------------------------------

def _run_handedness_sync(db: Session) -> None:
    """Background task: populate Player.throws / Player.bats from MLB Stats API."""
    import time as _time

    import httpx

    from fantasai.models.player import Player as PlayerModel

    players = db.query(PlayerModel).filter(PlayerModel.mlbam_id.isnot(None)).all()
    mlbam_ids = [p.mlbam_id for p in players if p.mlbam_id]
    if not mlbam_ids:
        logger.info("sync-handedness: no players with mlbam_id found")
        return

    mlbam_to_player: dict[int, PlayerModel] = {
        p.mlbam_id: p for p in players if p.mlbam_id
    }

    batch_size = 100
    updated = 0
    failed  = 0

    for i in range(0, len(mlbam_ids), batch_size):
        batch   = mlbam_ids[i : i + batch_size]
        ids_str = ",".join(str(m) for m in batch)
        url     = (
            f"https://statsapi.mlb.com/api/v1/people"
            f"?personIds={ids_str}"
            f"&fields=people,id,pitchHand,batSide"
        )
        try:
            resp = httpx.get(url, timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("sync-handedness: MLB Stats API failed (batch %d): %s", i, exc)
            failed += len(batch)
            continue

        for person in data.get("people") or []:
            mlbam_id  = person.get("id")
            player    = mlbam_to_player.get(mlbam_id)
            if player is None:
                continue

            pitch_hand = (person.get("pitchHand") or {}).get("code")
            bat_side   = (person.get("batSide")   or {}).get("code")

            changed = False
            if pitch_hand and getattr(player, "throws", None) != pitch_hand:
                player.throws = pitch_hand
                changed = True
            if bat_side and getattr(player, "bats", None) != bat_side:
                player.bats = bat_side
                changed = True
            if changed:
                updated += 1

        _time.sleep(0.1)   # polite rate-limiting

    try:
        db.commit()
        logger.info("sync-handedness: committed — %d updated, %d failed", updated, failed)
    except Exception as exc:
        db.rollback()
        logger.error("sync-handedness: commit failed: %s", exc)


@router.post("/sync-handedness", status_code=202, tags=["admin"])
def sync_handedness(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Populate Player.throws and Player.bats from the MLB Stats API.

    Queries the /people endpoint in batches of 100 using each player's mlbam_id
    and stores pitchHand.code → throws, batSide.code → bats.

    Admin endpoint — no authentication required (consistent with other admin
    endpoints in this file).
    """
    background_tasks.add_task(_run_handedness_sync, db)
    return {
        "status": "accepted",
        "message": "Syncing player handedness (throws/bats) in background via MLB Stats API",
    }

