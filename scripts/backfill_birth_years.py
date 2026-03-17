"""Backfill birth_year on all existing Player records from pybaseball.

Run this ONCE after the add_player_birth_year migration has been applied:

    PYTHONPATH=src python scripts/backfill_birth_years.py

Queries FanGraphs batting_stats(2025) and pitching_stats(2025), extracts the
Age column, computes birth_year = 2025 - age, and updates the players table.
"""
from __future__ import annotations

import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SEASON = 2025


def main() -> None:
    import pandas as pd
    from pybaseball import batting_stats, pitching_stats
    from fantasai.database import engine
    from sqlalchemy.orm import Session
    from fantasai.models.player import Player

    logger.info("Fetching %d batting stats from pybaseball...", SEASON)
    batting_df = batting_stats(SEASON, qual=50)  # low qual to include more players
    logger.info("Fetching %d pitching stats from pybaseball...", SEASON)
    pitching_df = pitching_stats(SEASON, qual=5)

    # Build {IDfg: birth_year} from each DataFrame
    age_map: dict[int, int] = {}
    for df, label in [(batting_df, "batting"), (pitching_df, "pitching")]:
        if "Age" not in df.columns or "IDfg" not in df.columns:
            logger.warning("Missing Age or IDfg column in %s df — skipping", label)
            continue
        for _, row in df.iterrows():
            try:
                player_id = int(row["IDfg"])
                age = int(float(row["Age"]))
                if player_id and 15 <= age <= 50:
                    birth_year = SEASON - age
                    # If already set, keep the batting value (more reliable)
                    if player_id not in age_map:
                        age_map[player_id] = birth_year
            except (TypeError, ValueError):
                continue

    logger.info("Built age_map with %d entries", len(age_map))

    updated = 0
    skipped = 0

    with Session(engine) as db:
        players = db.query(Player).all()
        for player in players:
            birth_year = age_map.get(player.player_id)
            if birth_year is None:
                skipped += 1
                continue
            if player.birth_year != birth_year:
                player.birth_year = birth_year
                updated += 1
        db.commit()

    logger.info("Done: updated=%d, skipped (no data)=%d", updated, skipped)


if __name__ == "__main__":
    main()
