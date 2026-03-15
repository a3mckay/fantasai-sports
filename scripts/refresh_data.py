"""CLI script to refresh player data, persist to DB, and compute rankings.

Usage examples:
  # Full refresh (season stats + all rolling windows)
  python scripts/refresh_data.py --season 2025

  # Season stats only, skip rolling windows
  python scripts/refresh_data.py --season 2025 --skip-rolling

  # Custom rolling windows
  python scripts/refresh_data.py --season 2025 --windows 14 30

  # Dry-run: fetch + print, no DB writes
  python scripts/refresh_data.py --season 2025 --no-db

  # Display rankings in terminal
  python scripts/refresh_data.py --season 2025 --top 25
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime

from fantasai.adapters.mlb import MLBAdapter
from fantasai.config import settings
from fantasai.database import SessionLocal
from fantasai.engine.pipeline import PipelineError, sync_players, sync_rolling_windows
from fantasai.engine.scoring import ScoringEngine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Default 6x6 categories from the reference league
DEFAULT_CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh MLB player data and compute rankings")
    parser.add_argument("--season", type=int, default=datetime.now().year)
    parser.add_argument("--week", type=int, default=None)
    parser.add_argument("--top", type=int, default=25, help="Number of top players to display")
    parser.add_argument(
        "--mode", choices=["lookback", "predictive", "both", "none"], default="both",
        help="Which rankings to display (none = skip display)",
    )
    parser.add_argument(
        "--no-db", action="store_true",
        help="Skip DB persistence — fetch + display only",
    )
    parser.add_argument(
        "--skip-rolling", action="store_true",
        help="Skip rolling window sync (faster, season stats only)",
    )
    parser.add_argument(
        "--windows", type=int, nargs="+", default=[7, 14, 30, 60],
        metavar="DAYS",
        help="Rolling window lengths in days (default: 7 14 30 60)",
    )
    parser.add_argument(
        "--as-of", type=str, default=None,
        metavar="YYYY-MM-DD",
        help="End date for rolling windows (default: today)",
    )
    args = parser.parse_args()

    as_of_date: date | None = None
    if args.as_of:
        try:
            as_of_date = date.fromisoformat(args.as_of)
        except ValueError:
            logger.error("Invalid --as-of date '%s'. Expected YYYY-MM-DD.", args.as_of)
            sys.exit(1)

    adapter = MLBAdapter()
    engine = ScoringEngine(adapter, DEFAULT_CATEGORIES)

    # ------------------------------------------------------------------ #
    # Phase 1: Fetch season stats + persist to DB
    # ------------------------------------------------------------------ #
    if args.no_db:
        logger.info("--no-db: fetching stats (no DB writes)")
        logger.info("Fetching %d player data from FanGraphs...", args.season)
        players = adapter.fetch_player_data(season=args.season, week=args.week)
        logger.info("Fetched %d player records", len(players))
    else:
        logger.info("Fetching %d season stats from FanGraphs and persisting to DB...", args.season)
        db = SessionLocal()
        try:
            players = sync_players(db, adapter, season=args.season, week=args.week)
            logger.info("Season stats sync complete — %d players persisted", len(players))
        except PipelineError as e:
            logger.error("Pipeline error: %s", e)
            db.close()
            sys.exit(1)
        except Exception as e:
            logger.error("Unexpected error during season sync: %s", e, exc_info=True)
            db.close()
            sys.exit(1)

    # ------------------------------------------------------------------ #
    # Phase 2: Rolling windows
    # ------------------------------------------------------------------ #
    if not args.skip_rolling:
        if args.no_db:
            logger.info("--no-db: skipping rolling window DB sync")
        else:
            logger.info(
                "Syncing rolling windows %s (as-of: %s)...",
                args.windows,
                (as_of_date or date.today()).isoformat(),
            )
            try:
                window_counts = sync_rolling_windows(
                    db,
                    adapter,
                    season=args.season,
                    as_of_date=as_of_date,
                    windows=args.windows,
                )
                for w, count in window_counts.items():
                    logger.info("  Window %d days: %d records upserted", w, count)
            except Exception as e:
                logger.error("Rolling window sync failed: %s", e, exc_info=True)
                # Non-fatal — season stats are already persisted

        if not args.no_db:
            db.close()

    elif not args.no_db:
        db.close()

    # ------------------------------------------------------------------ #
    # Phase 3: Display rankings in terminal
    # ------------------------------------------------------------------ #
    if args.mode == "none":
        logger.info("Done.")
        return

    batters = [p for p in players if p.stat_type == "batting"]
    pitchers = [p for p in players if p.stat_type == "pitching"]
    logger.info("Loaded %d batters and %d pitchers for ranking display", len(batters), len(pitchers))

    if args.mode in ("lookback", "both"):
        logger.info("\n=== LOOKBACK RANKINGS (Season-to-Date) ===")
        lookback = engine.compute_lookback_rankings(args.season, players=players)
        _print_rankings(lookback, args.top, "Lookback")

    if args.mode in ("predictive", "both"):
        logger.info("\n=== PREDICTIVE RANKINGS (Forward-Looking) ===")
        predictive = engine.compute_predictive_rankings(args.season, players=players)
        _print_rankings(predictive, args.top, "Predictive")

    logger.info("Done.")


def _print_rankings(rankings: list, top_n: int, label: str) -> None:
    """Pretty-print the top N rankings."""
    hitters = [r for r in rankings if r.stat_type == "batting"]
    pitchers = [r for r in rankings if r.stat_type == "pitching"]

    print(f"\n{'='*70}")
    print(f"  TOP {top_n} HITTERS ({label})")
    print(f"{'='*70}")
    print(f"  {'Rank':<6}{'Name':<25}{'Team':<6}{'Pos':<12}{'Score':<10}{'Top Categories'}")
    print(f"  {'-'*6}{'-'*25}{'-'*6}{'-'*12}{'-'*10}{'-'*30}")
    for r in hitters[:top_n]:
        pos = "/".join(r.positions[:3])
        top_cats = sorted(r.category_contributions.items(), key=lambda x: x[1], reverse=True)[:3]
        cat_str = ", ".join(f"{c}:{v:+.1f}" for c, v in top_cats)
        print(f"  {r.overall_rank:<6}{r.name:<25}{r.team:<6}{pos:<12}{r.score:<10.2f}{cat_str}")

    print(f"\n{'='*70}")
    print(f"  TOP {top_n} PITCHERS ({label})")
    print(f"{'='*70}")
    print(f"  {'Rank':<6}{'Name':<25}{'Team':<6}{'Pos':<12}{'Score':<10}{'Top Categories'}")
    print(f"  {'-'*6}{'-'*25}{'-'*6}{'-'*12}{'-'*10}{'-'*30}")
    for r in pitchers[:top_n]:
        pos = "/".join(r.positions[:3])
        top_cats = sorted(r.category_contributions.items(), key=lambda x: x[1], reverse=True)[:3]
        cat_str = ", ".join(f"{c}:{v:+.1f}" for c, v in top_cats)
        print(f"  {r.overall_rank:<6}{r.name:<25}{r.team:<6}{pos:<12}{r.score:<10.2f}{cat_str}")


if __name__ == "__main__":
    main()
