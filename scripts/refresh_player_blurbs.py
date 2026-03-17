"""Refresh predictive blurbs for specific players.

Usage:
    PYTHONPATH=src python scripts/refresh_player_blurbs.py Chandler Skubal

Connects to the configured DB (Railway in production), recomputes rankings,
generates fresh blurbs via the Anthropic API, and writes them back to the
Ranking table.  Player names are matched case-insensitively with substring
matching, so "Chandler" matches "Bubba Chandler".
"""
from __future__ import annotations

import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main(names: list[str]) -> None:
    from fantasai.config import settings
    from fantasai.database import engine
    from fantasai.engine.projection import ProjectionHorizon
    from sqlalchemy.orm import Session
    from fantasai.models.player import Player, PlayerStats
    from fantasai.models.ranking import Ranking
    from fantasai.adapters.base import NormalizedPlayerData
    from fantasai.adapters.mlb import MLBAdapter
    from fantasai.engine.scoring import ScoringEngine
    from fantasai.brain.blurb_generator import BlurbGenerator
    from fantasai.api.v1.rankings import RANKINGS_DEFAULT_CATEGORIES, CURRENT_PERIOD

    
    categories = RANKINGS_DEFAULT_CATEGORIES

    with Session(engine) as db:
        # Compute predictive rankings for all players (cached after first call)
        logger.info("Computing predictive rankings...")
        stats_rows = db.query(PlayerStats).filter(
            PlayerStats.season == 2025,
            PlayerStats.stat_type.in_(["batting", "pitching"]),
        ).all()
        if not stats_rows:
            logger.error("No stats found — run the data pipeline first")
            sys.exit(1)

        player_map = {
            p.player_id: p
            for p in db.query(Player).filter(
                Player.player_id.in_([s.player_id for s in stats_rows])
            ).all()
        }

        players = []
        for stats in stats_rows:
            player = player_map.get(stats.player_id)
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
        scoring_engine = ScoringEngine(adapter, categories)
        predictive = scoring_engine.compute_predictive_rankings(
            2025, players=players, horizon=ProjectionHorizon.SEASON
        )

        # Deduplicate (two-way players)
        seen: dict = {}
        for r in predictive:
            if r.player_id not in seen or r.score > seen[r.player_id].score:
                seen[r.player_id] = r
        predictive = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        for i, r in enumerate(predictive):
            r.overall_rank = i + 1

        ranking_map = {r.player_id: r for r in predictive}

        # Find target players
        targets = []
        for name_query in names:
            q = name_query.lower()
            matches = [p for p in player_map.values() if q in p.name.lower()]
            if not matches:
                logger.warning("No player found matching '%s'", name_query)
                continue
            if len(matches) > 1:
                logger.warning("Multiple matches for '%s': %s — using first", name_query, [m.name for m in matches])
            p = matches[0]
            r = ranking_map.get(p.player_id)
            if not r:
                logger.warning("%s has no predictive ranking", p.name)
                continue
            logger.info("Found: %s  rank=#%d  score=%.2f", p.name, r.overall_rank, r.score)
            targets.append(r)

        if not targets:
            logger.error("No valid targets found")
            sys.exit(1)

        # Generate new blurbs
        if not settings.anthropic_api_key:
            logger.error("ANTHROPIC_API_KEY not set — cannot generate blurbs")
            sys.exit(1)

        gen = BlurbGenerator(api_key=settings.anthropic_api_key)
        blurbs = gen.generate_blurbs_single_call(
            targets,
            ranking_type="predictive",
            scoring_categories=categories,
            top_n=0,  # generate for all, regardless of rank
        )
        logger.info("Generated %d blurbs", len(blurbs))

        # Upsert blurbs into Ranking table
        for r in targets:
            blurb = blurbs.get(r.player_id)
            if not blurb:
                logger.warning("No blurb generated for %s", r.name)
                continue

            existing = (
                db.query(Ranking)
                .filter(
                    Ranking.player_id == r.player_id,
                    Ranking.ranking_type == "predictive",
                    Ranking.period == CURRENT_PERIOD,
                    Ranking.league_id.is_(None),
                )
                .first()
            )
            if existing:
                existing.blurb = blurb
                existing.overall_rank = r.overall_rank
                existing.score = r.score
                existing.category_contributions = r.category_contributions
                logger.info("Updated blurb for %s (#%d)", r.name, r.overall_rank)
            else:
                row = Ranking(
                    player_id=r.player_id,
                    ranking_type="predictive",
                    period=CURRENT_PERIOD,
                    overall_rank=r.overall_rank,
                    score=r.score,
                    category_contributions=r.category_contributions,
                    blurb=blurb,
                    league_id=None,
                )
                db.add(row)
                logger.info("Inserted blurb for %s (#%d)", r.name, r.overall_rank)

            logger.info("  Blurb: %s", blurb[:120] + "..." if len(blurb) > 120 else blurb)

        db.commit()
        logger.info("Done.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/refresh_player_blurbs.py <name> [name2 ...]")
        sys.exit(1)
    main(sys.argv[1:])
