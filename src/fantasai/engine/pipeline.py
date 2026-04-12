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


def backfill_mlbam_ids(db: Session) -> int:
    """Populate Player.mlbam_id for all players using the Chadwick register.

    Reads FanGraphs IDs (player_id column = IDfg) and looks up matching MLBAM
    IDs from the pybaseball Chadwick Bureau register.  Only updates rows where
    mlbam_id is currently NULL to avoid overwriting manually-set values.

    Returns:
        Number of rows updated.
    """
    from fantasai.adapters.mlb import _build_fg_to_mlbam

    players_without_mlbam = db.query(Player).filter(Player.mlbam_id.is_(None)).all()
    if not players_without_mlbam:
        return 0

    fg_ids = [p.player_id for p in players_without_mlbam]
    mapping = _build_fg_to_mlbam(fg_ids)  # {fangraphs_id: mlbam_id}

    updated = 0
    for player in players_without_mlbam:
        mlbam = mapping.get(player.player_id)
        if mlbam:
            player.mlbam_id = mlbam
            updated += 1

    if updated:
        db.commit()
    return updated


def sync_steamer_projections(
    db: Session,
    season: int = 2026,
    batch_size: int = BATCH_SIZE,
) -> int:
    """Fetch forward-looking projections from FanGraphs and persist to DB.

    Uses a per-category consensus blend of the most accurate available
    systems (ATC, ZiPS, The BAT, Steamer) based on whiffs.org 2025 accuracy
    research.  Falls back to single-system Steamer for prospects / MiLB
    players not covered by the consensus systems.

    Stores projections as PlayerStats rows with the given season so that
    keeper-evaluation queries can prefer forward-looking data (season=2027+)
    over current-year actuals (season=2026).

    Creates Player rows for any projection player who isn't already in the DB.
    Existing Player rows are never downgraded (birth_year stays as-is).

    Returns:
        Total number of projection rows successfully upserted.
    """
    from fantasai.adapters.projections import (
        fetch_consensus_batting,
        fetch_consensus_pitching,
    )

    try:
        batters = fetch_consensus_batting(season)
    except Exception:
        logger.error("Consensus batting fetch failed — skipping batting projections")
        batters = []

    try:
        pitchers = fetch_consensus_pitching(season)
    except Exception:
        logger.error("Consensus pitching fetch failed — skipping pitching projections")
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


def sync_current_season_stats(db: Session, season: int = 2026) -> int:
    """Fetch current-season stats from FanGraphs via pybaseball and upsert to DB.

    Fetches all batters (qual=0) and all pitchers (qual=0) for the given
    season, matches each row to an existing Player by FanGraphs IDfg, and
    upserts a PlayerStats row with week=None.

    Missing columns (not all seasons have every advanced metric) are handled
    gracefully with .get() and None defaults.

    Returns:
        Number of PlayerStats rows upserted.
    """
    import math

    try:
        import pybaseball
    except ImportError:
        logger.error("pybaseball not installed — cannot sync current season stats")
        return 0

    # Disable pybaseball's disk cache so we always fetch fresh data from FanGraphs.
    try:
        pybaseball.cache.disable()
    except Exception:
        pass

    def _fval(row: dict, key: str) -> Optional[float]:
        """Return a float value from a row dict, or None if missing/NaN/Inf."""
        v = row.get(key)
        if v is None:
            return None
        try:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return None

    total_upserted = 0

    # ── Batting ──────────────────────────────────────────────────────────────
    try:
        bat_df = pybaseball.batting_stats(season, qual=0)
        logger.info("sync_current_season_stats: fetched %d batter rows", len(bat_df))
    except Exception:
        logger.error("batting_stats(%d) fetch failed", season, exc_info=True)
        bat_df = None

    if bat_df is not None:
        for _, row in bat_df.iterrows():
            row_dict = row.to_dict()
            fg_id = row_dict.get("IDfg")
            if fg_id is None:
                continue
            try:
                player_id = int(float(fg_id))
            except (TypeError, ValueError):
                continue

            # Match to existing player by FanGraphs ID
            player = db.get(Player, player_id)
            if player is None:
                continue

            counting_stats = {
                "PA":  _fval(row_dict, "PA"),
                "AB":  _fval(row_dict, "AB"),
                "H":   _fval(row_dict, "H"),
                "HR":  _fval(row_dict, "HR"),
                "R":   _fval(row_dict, "R"),
                "RBI": _fval(row_dict, "RBI"),
                "SB":  _fval(row_dict, "SB"),
                "BB":  _fval(row_dict, "BB"),
                "SO":  _fval(row_dict, "SO"),
                "2B":  _fval(row_dict, "2B"),
                "3B":  _fval(row_dict, "3B"),
            }
            rate_stats = {
                "AVG": _fval(row_dict, "AVG"),
                "OBP": _fval(row_dict, "OBP"),
                "SLG": _fval(row_dict, "SLG"),
                "OPS": _fval(row_dict, "OPS"),
                "BB%": _fval(row_dict, "BB%"),
                "K%":  _fval(row_dict, "K%"),
            }
            advanced_stats = {
                "xwOBA":    _fval(row_dict, "xwOBA"),
                "xBA":      _fval(row_dict, "xBA"),
                "xSLG":     _fval(row_dict, "xSLG"),
                "Barrel%":  _fval(row_dict, "Barrel%"),
                "HardHit%": _fval(row_dict, "HardHit%"),
                "EV":       _fval(row_dict, "EV") or _fval(row_dict, "AvgEV"),
                "wRC+":     _fval(row_dict, "wRC+"),
                "BABIP":    _fval(row_dict, "BABIP"),
            }

            existing = (
                db.query(PlayerStats)
                .filter(
                    and_(
                        PlayerStats.player_id == player_id,
                        PlayerStats.season == season,
                        PlayerStats.week.is_(None),
                        PlayerStats.stat_type == "batting",
                    )
                )
                .first()
            )
            if existing is None:
                db.add(PlayerStats(
                    player_id=player_id,
                    season=season,
                    week=None,
                    stat_type="batting",
                    data_source="actual",
                    counting_stats=counting_stats,
                    rate_stats=rate_stats,
                    advanced_stats=advanced_stats,
                ))
            else:
                existing.data_source = "actual"
                existing.counting_stats = counting_stats
                existing.rate_stats = rate_stats
                existing.advanced_stats = advanced_stats
            total_upserted += 1

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.error("Failed to commit batting stats batch", exc_info=True)

    # ── Pitching ─────────────────────────────────────────────────────────────
    try:
        pit_df = pybaseball.pitching_stats(season, qual=0)
        logger.info("sync_current_season_stats: fetched %d pitcher rows", len(pit_df))
    except Exception:
        logger.error("pitching_stats(%d) fetch failed", season, exc_info=True)
        pit_df = None

    if pit_df is not None:
        for _, row in pit_df.iterrows():
            row_dict = row.to_dict()
            fg_id = row_dict.get("IDfg")
            if fg_id is None:
                continue
            try:
                player_id = int(float(fg_id))
            except (TypeError, ValueError):
                continue

            player = db.get(Player, player_id)
            if player is None:
                continue

            counting_stats = {
                "IP":  _fval(row_dict, "IP"),
                "W":   _fval(row_dict, "W"),
                "L":   _fval(row_dict, "L"),
                "SV":  _fval(row_dict, "SV"),
                "HLD": _fval(row_dict, "HLD"),
                "SO":  _fval(row_dict, "SO"),
                "K":   _fval(row_dict, "SO"),  # alias
                "BB":  _fval(row_dict, "BB"),
                "G":   _fval(row_dict, "G"),
                "GS":  _fval(row_dict, "GS"),
                "QS":  _fval(row_dict, "QS"),
                "ERA": _fval(row_dict, "ERA"),
            }
            rate_stats = {
                "ERA":  _fval(row_dict, "ERA"),
                "WHIP": _fval(row_dict, "WHIP"),
                "K/9":  _fval(row_dict, "K/9"),
                "BB/9": _fval(row_dict, "BB/9"),
                "K-BB%": _fval(row_dict, "K-BB%"),
            }
            advanced_stats = {
                "xERA":     _fval(row_dict, "xERA"),
                "xFIP":     _fval(row_dict, "xFIP"),
                "SIERA":    _fval(row_dict, "SIERA"),
                "Stuff+":   _fval(row_dict, "Stuff+"),
                "CSW%":     _fval(row_dict, "CSW%"),
                "SwStr%":   _fval(row_dict, "SwStr%"),
                "GB%":      _fval(row_dict, "GB%"),
                "Barrel%":  _fval(row_dict, "Barrel%"),
                "HardHit%": _fval(row_dict, "HardHit%"),
            }

            existing = (
                db.query(PlayerStats)
                .filter(
                    and_(
                        PlayerStats.player_id == player_id,
                        PlayerStats.season == season,
                        PlayerStats.week.is_(None),
                        PlayerStats.stat_type == "pitching",
                    )
                )
                .first()
            )
            if existing is None:
                db.add(PlayerStats(
                    player_id=player_id,
                    season=season,
                    week=None,
                    stat_type="pitching",
                    data_source="actual",
                    counting_stats=counting_stats,
                    rate_stats=rate_stats,
                    advanced_stats=advanced_stats,
                ))
            else:
                existing.data_source = "actual"
                existing.counting_stats = counting_stats
                existing.rate_stats = rate_stats
                existing.advanced_stats = advanced_stats
            total_upserted += 1

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.error("Failed to commit pitching stats batch", exc_info=True)

    logger.info("sync_current_season_stats: upserted %d total rows", total_upserted)
    return total_upserted


def write_ranking_snapshots(
    db: Session,
    rankings: list,
    ranking_type: str,
    horizon: str,
    snapshot_date=None,
) -> int:
    """Upsert ranking snapshots for movement tracking.

    Args:
        rankings: list of PlayerRanking objects
        ranking_type: "current" or "predictive"
        horizon: "week", "month", "season", or "current"
        snapshot_date: date to snapshot (defaults to today)
    Returns:
        count of rows written
    """
    from datetime import date as _date
    from fantasai.models.ranking import RankingSnapshot

    today = snapshot_date or _date.today()
    count = 0

    for r in rankings:
        existing = db.query(RankingSnapshot).filter(
            RankingSnapshot.player_id == r.player_id,
            RankingSnapshot.ranking_type == ranking_type,
            RankingSnapshot.horizon == horizon,
            RankingSnapshot.snapshot_date == today,
        ).first()

        if existing is None:
            existing = RankingSnapshot(
                player_id=r.player_id,
                ranking_type=ranking_type,
                horizon=horizon,
                snapshot_date=today,
            )
            db.add(existing)

        existing.overall_rank = r.overall_rank
        existing.score = r.score
        existing.stat_type = r.stat_type
        count += 1

    db.commit()
    return count


def _upsert_player_stats(
    db: Session,
    data: NormalizedPlayerData,
    season: int,
    week: Optional[int],
    data_source: str = "projection",
) -> None:
    """Insert or update a PlayerStats record."""
    q = db.query(PlayerStats).filter(
        and_(
            PlayerStats.player_id == data.player_id,
            PlayerStats.season == season,
            PlayerStats.week == week if week is not None else PlayerStats.week.is_(None),
            PlayerStats.stat_type == data.stat_type,
            PlayerStats.data_source == data_source,
        )
    )
    existing = q.first()

    if existing is None:
        stats = PlayerStats(
            player_id=data.player_id,
            season=season,
            week=week,
            stat_type=data.stat_type,
            data_source=data_source,
            counting_stats=data.counting_stats,
            rate_stats=data.rate_stats,
            advanced_stats=data.advanced_stats,
        )
        db.add(stats)
    else:
        existing.counting_stats = data.counting_stats
        existing.rate_stats = data.rate_stats
        existing.advanced_stats = data.advanced_stats


def sync_mlb_api_current_season(db: Session, season: int = 2026) -> int:
    """Fetch current-season stats from MLB Stats API and upsert to PlayerStats.

    More real-time than pybaseball/FanGraphs — updates same day, ~2 hours after
    games finish.  Matches players via Player.mlbam_id.

    Returns: number of PlayerStats rows upserted.
    """
    import math

    import httpx

    from fantasai.models.player import Player, PlayerStats

    MLB_BASE = "https://statsapi.mlb.com/api/v1"

    # Build mlbam_id → player_id lookup
    players_with_mlbam = db.query(Player).filter(Player.mlbam_id.isnot(None)).all()
    mlbam_to_player_id: dict[int, int] = {p.mlbam_id: p.player_id for p in players_with_mlbam}

    if not mlbam_to_player_id:
        logger.warning(
            "sync_mlb_api_current_season: no players with mlbam_id — run backfill_mlbam_ids first"
        )
        return 0

    logger.info(
        "sync_mlb_api_current_season: %d players with mlbam_id available", len(mlbam_to_player_id)
    )

    def _fv(stat: dict, key: str) -> Optional[float]:
        v = stat.get(key)
        if v is None:
            return None
        try:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return None

    total_upserted = 0

    # ── Hitting ───────────────────────────────────────────────────────────────
    try:
        resp = httpx.get(
            f"{MLB_BASE}/stats",
            params={
                "stats": "season",
                "group": "hitting",
                "playerPool": "all",
                "season": season,
                "sportId": 1,
                "limit": 2000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        hitting_splits = resp.json().get("stats", [{}])[0].get("splits", [])
        logger.info("sync_mlb_api_current_season: %d hitting splits", len(hitting_splits))
    except Exception:
        logger.error("sync_mlb_api_current_season: hitting fetch failed", exc_info=True)
        hitting_splits = []

    for split in hitting_splits:
        mlbam_id = split.get("player", {}).get("id")
        if not mlbam_id:
            continue
        player_id = mlbam_to_player_id.get(int(mlbam_id))
        if not player_id:
            continue
        stat = split.get("stat", {})
        pa = _fv(stat, "plateAppearances")
        if not pa:
            continue  # skip players with no plate appearances

        counting_stats = {
            "PA":  pa,
            "AB":  _fv(stat, "atBats"),
            "H":   _fv(stat, "hits"),
            "HR":  _fv(stat, "homeRuns"),
            "R":   _fv(stat, "runs"),
            "RBI": _fv(stat, "rbi"),
            "SB":  _fv(stat, "stolenBases"),
            "BB":  _fv(stat, "baseOnBalls"),
            "SO":  _fv(stat, "strikeOuts"),
            "2B":  _fv(stat, "doubles"),
            "3B":  _fv(stat, "triples"),
        }
        rate_stats = {
            "AVG": _fv(stat, "avg"),
            "OBP": _fv(stat, "obp"),
            "SLG": _fv(stat, "slg"),
            "OPS": _fv(stat, "ops"),
        }

        existing = (
            db.query(PlayerStats)
            .filter(
                and_(
                    PlayerStats.player_id == player_id,
                    PlayerStats.season == season,
                    PlayerStats.week.is_(None),
                    PlayerStats.stat_type == "batting",
                    PlayerStats.data_source == "actual",
                )
            )
            .first()
        )
        if existing is None:
            db.add(PlayerStats(
                player_id=player_id,
                season=season,
                week=None,
                stat_type="batting",
                data_source="actual",
                counting_stats=counting_stats,
                rate_stats=rate_stats,
                advanced_stats={},  # FanGraphs sync will populate these later
            ))
        else:
            existing.counting_stats = counting_stats
            existing.rate_stats = rate_stats
            # Do NOT overwrite advanced_stats — MLB Stats API doesn't provide
            # xwOBA, Barrel%, HardHit%, wRC+ etc.  FanGraphs sync owns these.
            # Preserve whatever FanGraphs already wrote.
        total_upserted += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("sync_mlb_api_current_season: hitting commit failed", exc_info=True)

    # ── Pitching ─────────────────────────────────────────────────────────────
    try:
        resp = httpx.get(
            f"{MLB_BASE}/stats",
            params={
                "stats": "season",
                "group": "pitching",
                "playerPool": "all",
                "season": season,
                "sportId": 1,
                "limit": 2000,
            },
            timeout=30,
        )
        resp.raise_for_status()
        pitching_splits = resp.json().get("stats", [{}])[0].get("splits", [])
        logger.info("sync_mlb_api_current_season: %d pitching splits", len(pitching_splits))
    except Exception:
        logger.error("sync_mlb_api_current_season: pitching fetch failed", exc_info=True)
        pitching_splits = []

    for split in pitching_splits:
        mlbam_id = split.get("player", {}).get("id")
        if not mlbam_id:
            continue
        player_id = mlbam_to_player_id.get(int(mlbam_id))
        if not player_id:
            continue
        stat = split.get("stat", {})
        ip_raw = stat.get("inningsPitched")
        if not ip_raw:
            continue

        try:
            ip = float(ip_raw)
        except (TypeError, ValueError):
            continue
        if ip <= 0:
            continue

        # K/9 and BB/9 computed from raw totals
        so = _fv(stat, "strikeOuts") or 0.0
        bb = _fv(stat, "baseOnBalls") or 0.0
        k9 = round(so / ip * 9, 2) if ip > 0 else None
        bb9 = round(bb / ip * 9, 2) if ip > 0 else None
        kbb_pct = round((so - bb) / max(1, _fv(stat, "battersFaced") or 1), 4) if ip > 0 else None

        counting_stats = {
            "IP":  ip,
            "W":   _fv(stat, "wins"),
            "L":   _fv(stat, "losses"),
            "SV":  _fv(stat, "saves"),
            "HLD": _fv(stat, "holds"),
            "SO":  so,
            "K":   so,
            "BB":  bb,
            "G":   _fv(stat, "gamesPlayed"),
            "GS":  _fv(stat, "gamesStarted"),
            "QS":  None,   # not in MLB Stats API
            "ERA": _fv(stat, "era"),
        }
        rate_stats = {
            "ERA":   _fv(stat, "era"),
            "WHIP":  _fv(stat, "whip"),
            "K/9":   k9,
            "BB/9":  bb9,
            "K-BB%": kbb_pct,
        }

        existing = (
            db.query(PlayerStats)
            .filter(
                and_(
                    PlayerStats.player_id == player_id,
                    PlayerStats.season == season,
                    PlayerStats.week.is_(None),
                    PlayerStats.stat_type == "pitching",
                    PlayerStats.data_source == "actual",
                )
            )
            .first()
        )
        if existing is None:
            db.add(PlayerStats(
                player_id=player_id,
                season=season,
                week=None,
                stat_type="pitching",
                data_source="actual",
                counting_stats=counting_stats,
                rate_stats=rate_stats,
                advanced_stats={},  # FanGraphs sync will populate these later
            ))
        else:
            existing.counting_stats = counting_stats
            existing.rate_stats = rate_stats
            # Do NOT overwrite advanced_stats — MLB Stats API doesn't provide
            # xERA, SIERA, Stuff+, CSW%, SwStr% etc.  FanGraphs sync owns these.
            # Preserve whatever FanGraphs already wrote.
        total_upserted += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("sync_mlb_api_current_season: pitching commit failed", exc_info=True)

    logger.info("sync_mlb_api_current_season: upserted %d total rows", total_upserted)
    return total_upserted
