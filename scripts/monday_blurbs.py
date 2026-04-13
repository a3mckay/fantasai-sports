#!/usr/bin/env python3
"""Monday blurb generation — all 4 ranking modes.

Designed to run as a Railway Cron job (independent of the web server process),
so a deploy or restart cannot cause the Monday blurb window to be missed.

Railway Cron setup (one-time, via Railway dashboard):
  - Service type: Cron
  - Schedule: 30 9 * * 1   (9:30 UTC every Monday)
  - Command: python scripts/monday_blurbs.py
  - Environment: same DATABASE_URL + ANTHROPIC_API_KEY as the web service

Usage (local / manual):
    DATABASE_URL=... ANTHROPIC_API_KEY=... python scripts/monday_blurbs.py
    python scripts/monday_blurbs.py --mode season   # single mode
    python scripts/monday_blurbs.py --top-n 150     # smaller run
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("monday_blurbs")

_ALL_MODES = ["season", "current", "week", "month"]


def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
            logger.info("Loaded .env from %s", env_path)
        except ImportError:
            pass


def main() -> None:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Generate Monday AI blurbs for all ranking modes")
    parser.add_argument(
        "--mode",
        choices=_ALL_MODES,
        default=None,
        help="Run a single mode only (default: all 4 modes)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=300,
        help="Number of top players per mode (default: 300)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set — cannot generate blurbs")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(db_url)
    SessionLocal = sessionmaker(bind=engine)

    from fantasai.brain.blurb_scheduler import generate_rankings_blurbs

    modes = [args.mode] if args.mode else _ALL_MODES
    total_generated = 0
    total_errors = 0

    db = SessionLocal()
    try:
        for mode in modes:
            logger.info("Starting blurb generation: mode=%s top_n=%d", mode, args.top_n)
            result = generate_rankings_blurbs(db, api_key, mode=mode, top_n=args.top_n)
            logger.info("mode=%s result=%s", mode, result)
            total_generated += result.get("generated", 0)
            total_errors += result.get("errors", 0)
    except Exception:
        logger.error("Blurb generation failed", exc_info=True)
        sys.exit(1)
    finally:
        db.close()

    logger.info(
        "All modes complete — generated=%d errors=%d",
        total_generated,
        total_errors,
    )
    if total_errors > 0:
        sys.exit(1)  # non-zero exit so Railway Cron marks the run as failed


if __name__ == "__main__":
    main()
