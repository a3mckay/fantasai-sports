#!/usr/bin/env python
"""Generate and store player ranking blurbs for the rankings page.

Blurbs are generated once (not per-user) and stored in the `rankings` table.
All users who visit the Rankings page see the same pre-generated blurbs.

Strategy
--------
* Uses the Anthropic Batches API (50% cost reduction vs. real-time calls).
* Generates blurbs for the top N players in both ranking types
  (predictive + lookback) — default: top 300 each.
* Upserts into the `rankings` table keyed on
  (player_id, ranking_type, period, league_id=None).
* Safe to re-run — existing blurbs are overwritten with fresh ones.

Usage
-----
    # Run locally or via Railway cron:
    python scripts/refresh_blurbs.py

    # Override number of players:
    python scripts/refresh_blurbs.py --top-n 200

    # Only regenerate one ranking type:
    python scripts/refresh_blurbs.py --type predictive

Environment
-----------
Requires DATABASE_URL and ANTHROPIC_API_KEY in the environment (or .env file).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Make sure the src package is importable when run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger("refresh_blurbs")


def _load_dotenv() -> None:
    """Load .env file if present (development convenience)."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
            logger.info("Loaded .env from %s", env_path)
        except ImportError:
            pass  # dotenv not installed; rely on real env vars


def main() -> None:
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Refresh player ranking blurbs")
    parser.add_argument(
        "--top-n", type=int, default=300,
        help="Number of top players to generate blurbs for (default: 300)",
    )
    parser.add_argument(
        "--type", choices=["predictive", "lookback", "both"], default="both",
        dest="ranking_type",
        help="Which ranking type(s) to refresh (default: both)",
    )
    parser.add_argument(
        "--season", type=int, default=2025,
        help="Season year (default: 2025)",
    )
    parser.add_argument(
        "--period", default="2025-season",
        help="Period label stored in the rankings table (default: 2025-season)",
    )
    parser.add_argument(
        "--no-wait", action="store_true",
        help="Submit Anthropic batch and exit without waiting for results",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.error("ANTHROPIC_API_KEY not set — cannot generate blurbs")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    # ---------------------------------------------------------------------------
    # DB + scoring setup
    # ---------------------------------------------------------------------------
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(db_url)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()

    from fantasai.adapters.base import NormalizedPlayerData
    from fantasai.adapters.mlb import MLBAdapter
    from fantasai.engine.scoring import ScoringEngine
    from fantasai.models.player import Player, PlayerStats
    from fantasai.models.ranking import Ranking

    CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"]

    logger.info("Fetching PlayerStats from DB (season=%d)…", args.season)
    stats_rows = db.query(PlayerStats).filter(
        PlayerStats.season == args.season,
        PlayerStats.stat_type.in_(["batting", "pitching"]),
    ).all()

    if not stats_rows:
        logger.error("No PlayerStats found for season %d — run refresh_data.py first", args.season)
        sys.exit(1)

    logger.info("Building player list from %d stat rows…", len(stats_rows))
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

    adapter = MLBAdapter()
    scoring_engine = ScoringEngine(adapter, CATEGORIES)

    logger.info("Computing rankings…")
    lookback_all = scoring_engine.compute_lookback_rankings(args.season, players=players)
    predictive_all = scoring_engine.compute_predictive_rankings(args.season, players=players)

    # Deduplicate (two-way players)
    def _dedup(rnks: list) -> list:
        seen: dict = {}
        for r in rnks:
            if r.player_id not in seen or r.score > seen[r.player_id].score:
                seen[r.player_id] = r
        deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        for i, r in enumerate(deduped):
            r.overall_rank = i + 1
        return deduped

    lookback_all = _dedup(lookback_all)
    predictive_all = _dedup(predictive_all)
    logger.info(
        "Rankings ready: %d lookback, %d predictive",
        len(lookback_all), len(predictive_all),
    )

    # ---------------------------------------------------------------------------
    # Blurb generation via Anthropic Batches API
    # ---------------------------------------------------------------------------
    from fantasai.brain.blurb_generator import BlurbGenerator

    gen = BlurbGenerator(api_key=api_key)

    types_to_run = (
        [("predictive", predictive_all), ("lookback", lookback_all)]
        if args.ranking_type == "both"
        else [(args.ranking_type, predictive_all if args.ranking_type == "predictive" else lookback_all)]
    )

    all_blurbs: dict[str, dict[int, str]] = {}  # ranking_type → {player_id: blurb}

    for rtype, rankings in types_to_run:
        top = rankings[: args.top_n]
        logger.info("Submitting Batches API job for %s (%d players)…", rtype, len(top))
        batch_id = gen.submit_blurb_batch(top, rtype, CATEGORIES, top_n=0)
        logger.info("Batch submitted: %s", batch_id)

        if args.no_wait:
            logger.info("--no-wait set; exiting. Re-run with --collect %s to store results.", batch_id)
            continue

        # Poll until the batch completes (typically < 15 min for 300 players)
        logger.info("Waiting for batch %s to complete…", batch_id)
        while True:
            status = gen.get_batch_status(batch_id)
            logger.info("  Batch status: %s", status)
            if status == "ended":
                break
            time.sleep(30)

        blurbs = gen.collect_batch_results(batch_id)
        logger.info("Collected %d blurbs for %s", len(blurbs), rtype)
        all_blurbs[rtype] = blurbs

    if args.no_wait:
        return

    # ---------------------------------------------------------------------------
    # Upsert blurbs into the Ranking table
    # ---------------------------------------------------------------------------
    logger.info("Upserting blurbs into rankings table (period=%s)…", args.period)
    upserted = 0

    for rtype, blurbs in all_blurbs.items():
        # Pre-fetch existing rows for this (ranking_type, period, league_id=None)
        existing: dict[int, Ranking] = {
            row.player_id: row
            for row in db.query(Ranking).filter(
                Ranking.ranking_type == rtype,
                Ranking.period == args.period,
                Ranking.league_id.is_(None),
            ).all()
        }

        rankings_map = {
            r.player_id: r
            for r in (predictive_all if rtype == "predictive" else lookback_all)
        }

        for player_id, blurb in blurbs.items():
            ranking = rankings_map.get(player_id)
            if ranking is None:
                continue

            if player_id in existing:
                row = existing[player_id]
                row.blurb = blurb
                row.overall_rank = ranking.overall_rank
                row.score = ranking.score
                row.category_contributions = ranking.category_contributions
            else:
                row = Ranking(
                    player_id=player_id,
                    ranking_type=rtype,
                    period=args.period,
                    overall_rank=ranking.overall_rank,
                    score=ranking.score,
                    category_contributions=ranking.category_contributions or {},
                    blurb=blurb,
                    league_id=None,
                )
                db.add(row)

            upserted += 1

    db.commit()
    logger.info("Done — upserted %d blurb rows.", upserted)
    db.close()


if __name__ == "__main__":
    main()
