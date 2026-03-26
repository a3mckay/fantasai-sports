"""FastAPI application factory."""
from __future__ import annotations

import logging
import sys
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from fantasai.config import settings


def _configure_logging() -> None:
    """Set up logging with format appropriate for the environment."""
    if settings.is_production:
        fmt = (
            '{"time":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","message":"%(message)s"}'
        )
    else:
        fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

    logging.basicConfig(
        level=settings.log_level,
        format=fmt,
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )


_configure_logging()

from fantasai.api.errors import register_error_handlers  # noqa: E402
from fantasai.api.health import router as health_router  # noqa: E402
from fantasai.api.v1.leagues import router as leagues_router  # noqa: E402
from fantasai.api.v1.players import router as players_router  # noqa: E402
from fantasai.api.v1.rankings import router as rankings_router  # noqa: E402
from fantasai.api.v1.recommendations import router as recommendations_router, _compute_rankings  # noqa: E402
from fantasai.api.v1.rankings import RANKINGS_DEFAULT_CATEGORIES  # noqa: E402
import fantasai.api.v1.rankings as _rankings_module  # noqa: E402
from fantasai.api.v1.analysis import router as analysis_router  # noqa: E402
from fantasai.api.v1.auth import router as auth_router  # noqa: E402
from fantasai.api.v1.users import router as users_router  # noqa: E402
from fantasai.api.v1.settings import router as settings_router  # noqa: E402

_log = logging.getLogger(__name__)


def _nightly_stats_refresh() -> None:
    """Nightly 4am EST: fetch current-season stats, recompute rankings, write snapshots."""
    from fantasai.database import SessionLocal
    from fantasai.engine.pipeline import sync_current_season_stats, write_ranking_snapshots

    _log.info("Nightly stats refresh starting")

    db = SessionLocal()
    try:
        count = sync_current_season_stats(db, season=2025)
        _log.info("Nightly refresh: upserted %d stat rows", count)

        # Clear rankings cache so next request recomputes with fresh data
        try:
            from fantasai.api.v1.recommendations import _RANKINGS_CACHE, _RANKINGS_RAW_CACHE
            _RANKINGS_CACHE.clear()
            _RANKINGS_RAW_CACHE.clear()
            _log.info("Rankings cache cleared after nightly refresh")
        except Exception:
            _log.debug("Could not clear rankings cache", exc_info=True)

    except Exception:
        _log.error("Nightly stats refresh failed", exc_info=True)
        db.rollback()
    finally:
        db.close()


def _thursday_week_flip() -> None:
    """Thursday midnight EST: flip This Week to show next week's rankings."""
    _rankings_module.SHOW_NEXT_WEEK = True
    _log.info("This Week flipped to show next week's data")


def _monday_week_reset() -> None:
    """Monday 4am EST: reset This Week back to current week."""
    _rankings_module.SHOW_NEXT_WEEK = False
    _log.info("This Week reset to current week")


def _warm_rankings_cache() -> None:
    """Pre-compute SEASON rankings on startup so the first user request is fast.

    Only warms the SEASON horizon (most commonly used).  Other horizons are
    computed on-demand and cached for 30 minutes on first hit.
    """
    try:
        from fantasai.api.deps import get_db
        from fantasai.engine.projection import ProjectionHorizon

        db = next(get_db())
        try:
            _compute_rankings(db, RANKINGS_DEFAULT_CATEGORIES, horizon=ProjectionHorizon.SEASON)
            _log.info("Rankings cache warmed: horizon=season")
        finally:
            db.close()
    except Exception:
        _log.warning("Rankings cache warm-up failed (non-fatal)", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the rankings cache in a background thread so startup is non-blocking
    t = threading.Thread(target=_warm_rankings_cache, daemon=True, name="cache-warmer")
    t.start()

    # Start the Yahoo league sync scheduler.
    # Syncs all connected users every 2 hours so roster data stays fresh
    # without manual intervention.
    # NOTE: This runs in-process.  If Railway ever scales to multiple instances,
    # replace with a dedicated job queue or Railway's native cron feature.
    from apscheduler.schedulers.background import BackgroundScheduler
    from fantasai.services.yahoo_sync import sync_all_yahoo_users

    scheduler = BackgroundScheduler(timezone="UTC", daemon=True)
    scheduler.add_job(
        sync_all_yahoo_users,
        trigger="interval",
        hours=2,
        id="yahoo-sync",
        max_instances=1,          # prevent overlap if a sync takes > 2 hours
        misfire_grace_time=300,   # allow 5-minute late starts
    )

    # Nightly 4am EST stats refresh + rankings snapshot
    scheduler.add_job(
        _nightly_stats_refresh,
        trigger="cron",
        hour=9,    # 9 UTC = 4am EST (UTC-5) / 5am EDT (UTC-4); use 9 to cover both
        minute=0,
        id="nightly-stats-refresh",
        max_instances=1,
        misfire_grace_time=600,
    )

    # Thursday midnight EST — flip This Week window (week_flip_flag in app state)
    scheduler.add_job(
        _thursday_week_flip,
        trigger="cron",
        day_of_week="thu",
        hour=5,    # 5 UTC = midnight EST
        minute=0,
        id="thursday-week-flip",
        max_instances=1,
        misfire_grace_time=300,
    )

    # Monday 4am EST — reset This Week back to current week
    scheduler.add_job(
        _monday_week_reset,
        trigger="cron",
        day_of_week="mon",
        hour=9,    # Monday 4am EST
        minute=0,
        id="monday-week-reset",
        max_instances=1,
    )

    scheduler.start()
    _log.info("Yahoo sync scheduler started (interval=2h)")

    yield

    scheduler.shutdown(wait=False)
    _log.info("Yahoo sync scheduler stopped")


app = FastAPI(
    title="FantasAI Sports",
    version="0.1.0",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url=None,
    lifespan=lifespan,
)

register_error_handlers(app)

# CORS — allow the frontend origin(s) to call the API.
# Set CORS_ORIGINS in the environment (comma-separated) to restrict in production.
_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(leagues_router, prefix="/api/v1")
app.include_router(players_router, prefix="/api/v1")
app.include_router(rankings_router, prefix="/api/v1")
app.include_router(recommendations_router, prefix="/api/v1")
app.include_router(analysis_router, prefix="/api/v1")
app.include_router(auth_router, prefix="/api/v1")
app.include_router(users_router, prefix="/api/v1")
app.include_router(settings_router, prefix="/api/v1")
