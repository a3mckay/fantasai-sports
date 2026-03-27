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
from fantasai.api.v1.transactions import router as transactions_router
from fantasai.api.v1.matchups import router as matchups_router  # noqa: E402

_log = logging.getLogger(__name__)


def _nightly_stats_refresh() -> None:
    """Nightly 4am EST: fetch current-season stats via MLB Stats API, recompute rankings."""
    from fantasai.database import SessionLocal
    from fantasai.engine.pipeline import sync_mlb_api_current_season

    _log.info("Nightly stats refresh starting")

    db = SessionLocal()
    try:
        count = sync_mlb_api_current_season(db, season=2026)
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


def _monday_blurb_generation() -> None:
    """Monday 4am EST: generate fresh AI blurbs for top 300 players in all ranking modes."""
    from fantasai.database import SessionLocal
    from fantasai.brain.blurb_scheduler import generate_rankings_blurbs

    _log.info("Monday blurb generation starting")
    db = SessionLocal()
    try:
        for mode in ["season", "current", "week", "month"]:
            result = generate_rankings_blurbs(db, settings.anthropic_api_key, mode=mode, top_n=300)
            _log.info("Blurb generation: %s", result)
    except Exception:
        _log.error("Monday blurb generation failed", exc_info=True)
    finally:
        db.close()


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

    # Monday 4am EST — generate fresh AI blurbs for top 300 players in all ranking modes
    scheduler.add_job(
        _monday_blurb_generation,
        trigger="cron",
        day_of_week="mon",
        hour=9,
        minute=30,
        id="monday-blurbs",
        max_instances=1,
        misfire_grace_time=600,
    )

    # Daytime MLB Stats API refresh every 3 hours (noon–9pm EST) during the season
    # so current rankings stay up-to-date between the nightly run and live games
    scheduler.add_job(
        _nightly_stats_refresh,
        trigger="cron",
        hour="15,18,21",  # 10am / 1pm / 4pm EST (UTC-5)
        minute=0,
        id="daytime-stats-refresh",
        max_instances=1,
        misfire_grace_time=300,
    )

    # Transaction polling every 20 min — grade new adds/drops/trades from Yahoo
    from fantasai.services.yahoo_transactions import poll_all_leagues
    scheduler.add_job(
        poll_all_leagues,
        trigger="interval",
        minutes=20,
        id="transaction-poll",
        max_instances=1,
        misfire_grace_time=120,
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
app.include_router(transactions_router, prefix="/api/v1")
app.include_router(matchups_router, prefix="/api/v1")
