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
from fantasai.api.v1.analysis import router as analysis_router  # noqa: E402
from fantasai.api.v1.auth import router as auth_router  # noqa: E402
from fantasai.api.v1.users import router as users_router  # noqa: E402
from fantasai.api.v1.settings import router as settings_router  # noqa: E402

_log = logging.getLogger(__name__)


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
