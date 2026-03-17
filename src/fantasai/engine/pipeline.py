"""Data pipeline: fetch player data from adapter and persist to database.

Handles:
- Batch-size commits to avoid losing all progress on failure
- Transaction safety with rollback on errors
- Retry logic for transient adapter failures
"""
from __future__ import annotations

import logging
import time
import unicodedata
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import and_
from sqlalchemy.orm import Session

from fantasai.adapters.base import NormalizedPlayerData, SportAdapter
from fantasai.adapters.mlb import MLBAdapter
from fantasai.models.player import Player, PlayerRollingStats, PlayerStats

logger = logging.getLogger(__name__)

# How many players to commit per batch
BATCH_SIZE = 100

# Retry config for adapter fetch calls
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


class PipelineError(Exception):
    """Raised when the pipeline encounters an unrecoverable error."""


def sync_players(
    db: Session,
    adapter: SportAdapter,
    season: int,
    week: Optional[int] = None,
    batch_size: int = BATCH_SIZE,
) -> list[NormalizedPlayerData]:
    """Fetch player data from adapter and upsert into database.

    Uses batched commits so that partial progress is preserved if a late
    record fails. Returns the full list of NormalizedPlayerData for
    downstream use (scoring).

    Raises:
        PipelineError: If the adapter fails after retries.
    """
    players = _fetch_with_retry(adapter, season, week)
    logger.info("Fetched %d player records from adapter", len(players))

    if not players:
        logger.warning("Adapter returned no players for season=%d week=%s", season, week)
        return []

    succeeded = 0
    failed = 0

    for i in range(0, len(players), batch_size):
        batch = players[i : i + batch_size]
        try:
            for p in batch:
                _upsert_player(db, p)
                _upsert_player_stats(db, p, season, week)
            db.commit()
            succeeded += len(batch)
        except Exception:
            db.rollback()
            failed += len(batch)
            logger.error(
                "Batch %d-%d failed, rolled back. Continuing with next batch.",
                i,
                i + len(batch),
                exc_info=True,
            )

    logger.info(
        "Pipeline complete: %d succeeded, %d failed out of %d total",
        succeeded,
        failed,
        len(players),
    )

    if failed == len(players):
        raise PipelineError(
            f"All {len(players)} player records failed to persist"
        )

    return players


# Rolling windows to sync: (window_days, label)
ROLLING_WINDOWS = [7, 14, 30, 60]


def sync_rolling_windows(
    db: Session,
    adapter: MLBAdapter,
    season: int,
    as_of_date: Optional[date] = None,
    windows: Optional[list[int]] = None,
    batch_size: int = BATCH_SIZE,
) -> dict[int, int]:
    """Fetch and persist rolling-window stats for all tracked players.

    Fetches Baseball Reference date-range stats for each window length,
    matches records to our player table by name + team, and upserts into
    player_rolling_stats.

    Name matching is best-effort (BRef uses slightly different spellings).
    Unmatched records are logged as warnings and skipped.

    Args:
        db: SQLAlchemy session.
        adapter: MLBAdapter instance.
        season: Current season year.
        as_of_date: End date for all windows (defaults to today).
        windows: Window lengths in days to sync. Defaults to ROLLING_WINDOWS.
        batch_size: DB commit batch size.

    Returns:
        Dict mapping window_days → number of records successfully upserted.
    """
    if as_of_date is None:
        as_of_date = date.today()
    if windows is None:
        windows = ROLLING_WINDOWS

    # Build player lookup: (normalised_name, normalised_team) -> player_id
    # and fallback: normalised_name -> [player_ids]
    all_players = db.query(Player).all()
    name_team_index: dict[tuple[str, str], int] = {}
    name_index: dict[str, list[int]] = {}
    for p in all_players:
        norm_name = _normalise_name(p.name)
        norm_team = p.team.upper() if p.team else ""
        name_team_index[(norm_name, norm_team)] = p.player_id
        name_index.setdefault(norm_name, []).append(p.player_id)

    results: dict[int, int] = {}

    for window_days in windows:
        start_dt = as_of_date - timedelta(days=window_days)
        start_str = start_dt.isoformat()
        end_str = as_of_date.isoformat()

        records: list[dict] = []
        try:
            records.extend(
                adapter.fetch_rolling_batting_stats(start_str, end_str, window_days)
            )
            records.extend(
                adapter.fetch_rolling_pitching_stats(start_str, end_str, window_days)
            )
        except Exception as e:
            logger.error(
                "Failed to fetch rolling stats for window=%d: %s", window_days, e, exc_info=True
            )
            results[window_days] = 0
            continue

        succeeded = 0
        unmatched = 0

        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            try:
                for rec in batch:
                    player_id = _resolve_player_id(
                        rec["name"], rec["team"], name_team_index, name_index
                    )
                    if player_id is None:
                        unmatched += 1
                        continue
                    _upsert_rolling_stats(
                        db, player_id, season, window_days,
                        start_dt, as_of_date, rec,
                    )
                    succeeded += 1
                db.commit()
            except Exception:
                db.rollback()
                logger.error(
                    "Rolling stats batch %d-%d (window=%d) failed, rolled back.",
                    i, i + len(batch), window_days, exc_info=True,
                )

        if unmatched:
            logger.warning(
                "Window=%d: %d records could not be matched to a player", window_days, unmatched
            )
        logger.info(
            "Window=%d: upserted %d rolling stat records (%d unmatched)",
            window_days, succeeded, unmatched,
        )
        results[window_days] = succeeded

    return results


def _normalise_name(name: str) -> str:
    """Normalise a player name for fuzzy matching.

    Strips diacritics, lowercases, and collapses whitespace so that
    e.g. "Javier Báez" matches "Javier Baez".
    """
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(ascii_name.lower().split())


def _resolve_player_id(
    name: str,
    team: str,
    name_team_index: dict[tuple[str, str], int],
    name_index: dict[str, list[int]],
) -> Optional[int]:
    """Resolve a BRef name+team to a player_id in our DB.

    Priority:
    1. Exact (normalised name, normalised team) match
    2. Name-only match when exactly one player has that name
    Returns None if no match is found.
    """
    norm_name = _normalise_name(name)
    norm_team = team.upper() if team else ""

    # Try exact name+team
    pid = name_team_index.get((norm_name, norm_team))
    if pid is not None:
        return pid

    # Fallback: name only (unambiguous)
    candidates = name_index.get(norm_name, [])
    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        logger.debug(
            "Ambiguous name '%s' (team=%s) matches %d players — skipping",
            name, team, len(candidates),
        )

    return None


def _upsert_rolling_stats(
    db: Session,
    player_id: int,
    season: int,
    window_days: int,
    start_date: date,
    end_date: date,
    rec: dict,
) -> None:
    """Insert or update a PlayerRollingStats record."""
    existing = (
        db.query(PlayerRollingStats)
        .filter(
            and_(
                PlayerRollingStats.player_id == player_id,
                PlayerRollingStats.season == season,
                PlayerRollingStats.window_days == window_days,
                PlayerRollingStats.stat_type == rec["stat_type"],
            )
        )
        .first()
    )

    if existing is None:
        row = PlayerRollingStats(
            player_id=player_id,
            season=season,
            window_days=window_days,
            start_date=start_date,
            end_date=end_date,
            stat_type=rec["stat_type"],
            counting_stats=rec["counting_stats"],
            rate_stats=rec["rate_stats"],
        )
        db.add(row)
    else:
        existing.start_date = start_date
        existing.end_date = end_date
        existing.counting_stats = rec["counting_stats"]
        existing.rate_stats = rec["rate_stats"]


def _fetch_with_retry(
    adapter: SportAdapter,
    season: int,
    week: Optional[int],
    max_retries: int = MAX_RETRIES,
    backoff: float = RETRY_BACKOFF_SECONDS,
) -> list[NormalizedPlayerData]:
    """Fetch player data with exponential backoff on transient errors."""
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            return adapter.fetch_player_data(season, week)
        except (ConnectionError, TimeoutError, OSError) as e:
            last_error = e
            wait = backoff * (2 ** (attempt - 1))
            logger.warning(
                "Adapter fetch attempt %d/%d failed: %s. Retrying in %.1fs...",
                attempt,
                max_retries,
                e,
                wait,
            )
            time.sleep(wait)
        except Exception as e:
            # Non-transient errors (ValueError, KeyError, etc.) — don't retry
            logger.error("Adapter fetch failed with non-retryable error: %s", e)
            raise PipelineError(f"Adapter fetch failed: {e}") from e

    raise PipelineError(
        f"Adapter fetch failed after {max_retries} retries: {last_error}"
    )


def _upsert_player(db: Session, data: NormalizedPlayerData) -> None:
    """Insert or update a Player record."""
    player = db.get(Player, data.player_id)
    if player is None:
        player = Player(
            player_id=data.player_id,
            name=data.name,
            team=data.team,
            positions=data.positions,
            birth_year=data.birth_year,
        )
        db.add(player)
    else:
        player.name = data.name
        player.team = data.team
        player.positions = data.positions
        # Update birth_year if we have it — never overwrite a known value with None
        if data.birth_year is not None:
            player.birth_year = data.birth_year


def sync_steamer_projections(
    db: Session,
    season: int = 2026,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Fetch 2026 Steamer projections from FanGraphs and persist to DB.

    Stores projections as PlayerStats rows with the given season so that
    keeper-evaluation queries can prefer forward-looking data (season=2026)
    over current-year actuals (season=2025).

    Creates Player rows for any projection player who isn't already in the DB,
    using whatever name/team/position data Steamer provides.  Existing Player
    rows are never downgraded (birth_year stays as-is).

    Returns:
        Total number of projection rows successfully upserted.
    """
    from fantasai.adapters.projections import fetch_steamer_batting, fetch_steamer_pitching

    try:
        batters = fetch_steamer_batting(season)
    except Exception:
        logger.error("Steamer batting fetch failed — skipping batting projections")
        batters = []

    try:
        pitchers = fetch_steamer_pitching(season)
    except Exception:
        logger.error("Steamer pitching fetch failed — skipping pitching projections")
        pitchers = []

    all_players = batters + pitchers
    logger.info(
        "sync_steamer_projections: %d batters + %d pitchers = %d total",
        len(batters), len(pitchers), len(all_players),
    )

    if not all_players:
        return 0

    succeeded = 0
    failed = 0

    for i in range(0, len(all_players), batch_size):
        batch = all_players[i : i + batch_size]
        try:
            for p in batch:
                _upsert_player(db, p)
                _upsert_player_stats(db, p, season, week=None)
            db.commit()
            succeeded += len(batch)
        except Exception:
            db.rollback()
            failed += len(batch)
            logger.error(
                "Projection batch %d-%d failed, rolled back.",
                i, i + len(batch),
                exc_info=True,
            )

    logger.info(
        "Steamer projections: %d upserted, %d failed",
        succeeded, failed,
    )
    return succeeded


def _upsert_player_stats(
    db: Session,
    data: NormalizedPlayerData,
    season: int,
    week: Optional[int],
) -> None:
    """Insert or update a PlayerStats record."""
    existing = (
        db.query(PlayerStats)
        .filter(
            and_(
                PlayerStats.player_id == data.player_id,
                PlayerStats.season == season,
                PlayerStats.week == week
                if week is not None
                else PlayerStats.week.is_(None),
                PlayerStats.stat_type == data.stat_type,
            )
        )
        .first()
    )

    if existing is None:
        stats = PlayerStats(
            player_id=data.player_id,
            season=season,
            week=week,
            stat_type=data.stat_type,
            counting_stats=data.counting_stats,
            rate_stats=data.rate_stats,
            advanced_stats=data.advanced_stats,
        )
        db.add(stats)
    else:
        existing.counting_stats = data.counting_stats
        existing.rate_stats = data.rate_stats
        existing.advanced_stats = data.advanced_stats
