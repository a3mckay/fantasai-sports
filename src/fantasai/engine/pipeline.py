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

    Two-pass strategy:
      1. Chadwick FG-ID match (fast, bulk).
      2. Name-based fallback via pybaseball.playerid_lookup for players whose
         FanGraphs ID isn't yet in the Chadwick register (common for in-season
         callups like Mike Burrows whose key_fangraphs=-1 until the register
         is refreshed).  Only auto-assigns when exactly one MLB-active match
         is found to avoid collisions.

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
    still_missing: list[Player] = []
    for player in players_without_mlbam:
        mlbam = mapping.get(player.player_id)
        if mlbam:
            player.mlbam_id = mlbam
            updated += 1
        else:
            still_missing.append(player)

    # Pass 2: name-based fallback for players not matched by FanGraphs ID.
    # The Chadwick register lags new callups by weeks or months; pybaseball's
    # playerid_lookup queries the same register but allows name-only search.
    if still_missing:
        try:
            import pybaseball as pb
            for player in still_missing:
                parts = player.name.strip().split()
                if len(parts) < 2:
                    continue
                first_name, last_name = parts[0], parts[-1]
                try:
                    result = pb.playerid_lookup(last_name, first_name)
                    if result is None or result.empty:
                        continue
                    # Only consider rows with a valid MLBAM ID
                    valid = result[
                        result["key_mlbam"].notna()
                        & (result["key_mlbam"].astype(float) > 0)
                    ]
                    if valid.empty:
                        continue
                    # Prefer MLB-active rows (played recently); single match = safe
                    recent = valid[valid["mlb_played_last"] >= 2022] if "mlb_played_last" in valid.columns else valid
                    candidates = recent if not recent.empty else valid
                    if len(candidates) == 1:
                        player.mlbam_id = int(candidates.iloc[0]["key_mlbam"])
                        updated += 1
                        logger.info(
                            "backfill_mlbam_ids: name-match %s → mlbam_id=%d",
                            player.name, player.mlbam_id,
                        )
                except Exception:
                    pass  # individual lookup failure is non-fatal
        except Exception:
            logger.warning("backfill_mlbam_ids: name-based fallback failed", exc_info=True)

    # Pass 3: bulk MLB Stats API 40-man roster pull for players still unmatched.
    # Pulls every team's 40-man roster (covers all rostered players including those
    # who haven't appeared in a game yet), builds a name → mlbam_id map, and
    # assigns unambiguous matches.  Much more complete than the sports/players
    # endpoint which only returns players who've already appeared in games.
    still_missing_after_p2 = [p for p in still_missing if p.mlbam_id is None]
    if still_missing_after_p2:
        try:
            import requests as _req
            import unicodedata as _ud

            def _norm(s: str) -> str:
                """Lowercase, strip accents, collapse whitespace."""
                s = _ud.normalize("NFD", s)
                s = "".join(c for c in s if _ud.category(c) != "Mn")
                return " ".join(s.lower().split())

            # Fetch all 30 teams
            teams_resp = _req.get(
                "https://statsapi.mlb.com/api/v1/teams",
                params={"sportId": 1, "season": 2026},
                timeout=15.0,
            )
            teams_resp.raise_for_status()
            teams = [t.get("id") for t in teams_resp.json().get("teams", []) if t.get("id")]

            # Build name → list[mlbam_id] from all 40-man rosters
            name_to_mlbam: dict[str, list[int]] = {}
            for team_id in teams:
                try:
                    r = _req.get(
                        f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                        params={"rosterType": "40Man", "season": 2026},
                        timeout=10.0,
                    )
                    r.raise_for_status()
                    for entry in r.json().get("roster", []):
                        person = entry.get("person", {})
                        mlbam = person.get("id")
                        full_name = person.get("fullName", "")
                        if mlbam and full_name:
                            key = _norm(full_name)
                            if mlbam not in name_to_mlbam.get(key, []):
                                name_to_mlbam.setdefault(key, []).append(mlbam)
                except Exception:
                    pass

            mlbam_pass3 = 0
            for player in still_missing_after_p2:
                key = _norm(player.name)
                matches = name_to_mlbam.get(key, [])
                if len(matches) == 1:
                    player.mlbam_id = matches[0]
                    updated += 1
                    mlbam_pass3 += 1
                    logger.info(
                        "backfill_mlbam_ids: mlb-api-match %s → mlbam_id=%d",
                        player.name, player.mlbam_id,
                    )
                # len > 1: genuinely ambiguous name, skip

            logger.info(
                "backfill_mlbam_ids: MLB Stats API (40-man) pass resolved %d / %d remaining",
                mlbam_pass3, len(still_missing_after_p2),
            )
        except Exception:
            logger.warning("backfill_mlbam_ids: MLB Stats API fallback failed", exc_info=True)

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


def _fetch_fangraphs_direct(season: int, stats_type: str) -> Optional[list[dict]]:
    """Fetch FanGraphs leaderboard data from the current JSON API (type=8 dashboard).

    The legacy pybaseball endpoint (leaders-legacy.aspx) returns 403 since early 2026.
    This function calls the new API directly, normalises column names to match what the
    rest of the pipeline expects, and returns a plain list of dicts.

    Args:
        season:     MLB season year (e.g. 2026).
        stats_type: 'bat' or 'pit'.

    Returns:
        List of row dicts with IDfg, Name (plain text), Team, and stat columns,
        or None if the request fails.
    """
    import re
    import json
    import urllib.request

    url = (
        "https://www.fangraphs.com/api/leaders/major-league/data"
        f"?pos=all&stats={stats_type}&lg=all&qual=0&type=8"
        f"&season={season}&season1={season}&ind=0"
        "&startdate=&enddate=&team=0&pageitems=2000&pagenum=1"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as exc:
        logger.error("FanGraphs direct fetch (%s) failed: %s", stats_type, exc)
        return None

    try:
        payload = json.loads(raw)
    except Exception as exc:
        logger.error("FanGraphs direct fetch (%s): JSON parse error: %s", stats_type, exc)
        return None

    rows = payload.get("data", [])
    if not rows:
        logger.warning("FanGraphs direct fetch (%s): empty data", stats_type)
        return None

    _html_re = re.compile(r"<[^>]+>")

    def _clean(row: dict) -> dict:
        """Normalise a raw API row to match legacy pybaseball column names."""
        # Strip HTML tags from Name (new API wraps names in <a> tags)
        raw_name = str(row.get("Name") or "")
        row["Name"] = _html_re.sub("", raw_name).strip()
        # playerid is the FanGraphs ID — expose it as IDfg for pipeline compatibility
        row["IDfg"] = row.get("playerid")
        # C+SwStr% is the new API name for what the pipeline calls CSW%
        if "C+SwStr%" in row and "CSW%" not in row:
            row["CSW%"] = row["C+SwStr%"]
        # xAVG is the new API name for xBA
        if "xAVG" in row and "xBA" not in row:
            row["xBA"] = row["xAVG"]
        return row

    cleaned = [_clean(r) for r in rows]
    logger.info("FanGraphs direct fetch (%s): %d rows", stats_type, len(cleaned))
    return cleaned


def sync_current_season_stats(db: Session, season: int = 2026) -> int:
    """Fetch current-season stats from FanGraphs and upsert to DB.

    Uses a direct call to the FanGraphs JSON API (the legacy pybaseball endpoint
    returns 403 since early 2026).  Fetches all batters and pitchers (qual=0),
    matches each row to an existing Player by FanGraphs IDfg, and upserts a
    PlayerStats row with week=None.

    Missing columns (not all seasons have every advanced metric) are handled
    gracefully with .get() and None defaults.

    Returns:
        Number of PlayerStats rows upserted.
    """
    import math

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
    bat_rows = _fetch_fangraphs_direct(season, "bat")
    if bat_rows is None:
        logger.error("sync_current_season_stats: batting fetch failed — skipping batters")
    else:
        logger.info("sync_current_season_stats: fetched %d batter rows", len(bat_rows))

    if bat_rows is not None:
        for row_dict in bat_rows:
            fg_id = row_dict.get("IDfg")
            if fg_id is None:
                continue
            try:
                player_id = int(float(fg_id))
            except (TypeError, ValueError):
                continue

            # Match to existing player by FanGraphs ID; create if missing
            player = db.get(Player, player_id)
            if player is None:
                name = str(row_dict.get("Name") or "").strip()
                team = str(row_dict.get("Team") or "").strip()
                if not name:
                    continue
                player = Player(player_id=player_id, name=name, team=team, positions=[])
                db.add(player)
                try:
                    db.flush()
                except Exception:
                    db.rollback()
                    logger.warning("Could not create player %d (%s) from FG batting stats", player_id, name)
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
                # Merge: preserve Savant-only fields (SprintSpeed, BatSpeed,
                # FastSwing%, Blast%, EV_FBLD, EV50, etc.) that FanGraphs
                # does not return.  FanGraphs values win for keys it provides.
                merged_adv = dict(existing.advanced_stats or {})
                merged_adv.update({k: v for k, v in advanced_stats.items() if v is not None})
                existing.advanced_stats = merged_adv
            total_upserted += 1

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.error("Failed to commit batting stats batch", exc_info=True)

    # ── Pitching ─────────────────────────────────────────────────────────────
    pit_rows = _fetch_fangraphs_direct(season, "pit")
    if pit_rows is None:
        logger.error("sync_current_season_stats: pitching fetch failed — skipping pitchers")
    else:
        logger.info("sync_current_season_stats: fetched %d pitcher rows", len(pit_rows))

    if pit_rows is not None:
        for row_dict in pit_rows:
            fg_id = row_dict.get("IDfg")
            if fg_id is None:
                continue
            try:
                player_id = int(float(fg_id))
            except (TypeError, ValueError):
                continue

            player = db.get(Player, player_id)
            if player is None:
                name = str(row_dict.get("Name") or "").strip()
                team = str(row_dict.get("Team") or "").strip()
                if not name:
                    continue
                player = Player(player_id=player_id, name=name, team=team, positions=[])
                db.add(player)
                try:
                    db.flush()
                except Exception:
                    db.rollback()
                    logger.warning("Could not create player %d (%s) from FG pitching stats", player_id, name)
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
                # Merge: preserve Savant-only fields (vFA, PitchRV100, SpinRate,
                # Ext, EV, EV_FBLD, etc.) that FanGraphs does not return.
                # FanGraphs values win for keys it provides.
                merged_adv = dict(existing.advanced_stats or {})
                merged_adv.update({k: v for k, v in advanced_stats.items() if v is not None})
                existing.advanced_stats = merged_adv
            total_upserted += 1

        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.error("Failed to commit pitching stats batch", exc_info=True)

    logger.info("sync_current_season_stats: upserted %d total rows", total_upserted)
    return total_upserted


def sync_statcast_advanced_stats(db: Session, season: int = 2026) -> int:
    """Fetch advanced stats from Baseball Savant (Statcast) and upsert to PlayerStats.

    Uses pybaseball's Statcast endpoints which pull from baseballsavant.mlb.com —
    a different URL than FanGraphs, so it works even when FanGraphs is unavailable.

    Populates advanced_stats for each player who has an MLBAM ID and an existing
    PlayerStats "actual" row. Does NOT touch counting_stats or rate_stats.

    Batting advanced stats populated:
      xwOBA (est_woba), xBA (est_ba), xSLG (est_slg),
      Barrel% (brl_percent), HardHit% (ev95percent), EV (avg_hit_speed),
      EV_FBLD (fbld — exit velo on fly balls/line drives, better power predictor),
      MaxEV (max_hit_speed), EV50 (ev50 — avg of top 50% batted balls),
      BatSpeed (avg_bat_speed), FastSwing% (hard_swing_rate), Blast% (blast_per_swing),
      SprintSpeed (sprint_speed)

    Pitching advanced stats populated:
      xERA (xera), Barrel% (brl_percent), HardHit% (ev95percent), EV (avg_hit_speed)

    Returns:
        Number of PlayerStats rows with advanced_stats updated.
    """
    import math

    try:
        import pybaseball
    except ImportError:
        logger.error("pybaseball not installed — cannot sync Statcast advanced stats")
        return 0

    try:
        pybaseball.cache.disable()
    except Exception:
        pass

    def _fval_sc(row: dict, key: str) -> Optional[float]:
        v = row.get(key)
        if v is None:
            return None
        try:
            f = float(v)
            return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
        except (TypeError, ValueError):
            return None

    # Build MLBAM → [player_id, ...] reverse map (only players with mlbam_id set).
    # One mlbam_id can map to multiple player rows (e.g. a FanGraphs row and a
    # Yahoo-synced row for the same player) — keep all so every row gets updated.
    rows = db.query(Player.player_id, Player.mlbam_id).filter(Player.mlbam_id.isnot(None)).all()
    mlbam_to_player_ids: dict[int, list[int]] = {}
    for row in rows:
        mlbam_to_player_ids.setdefault(row.mlbam_id, []).append(row.player_id)
    if not mlbam_to_player_ids:
        logger.warning("sync_statcast_advanced_stats: no players with mlbam_id — skipping")
        return 0

    logger.info("sync_statcast_advanced_stats: %d players with mlbam_id", len(mlbam_to_player_ids))
    total_updated = 0

    # ── Batting ──────────────────────────────────────────────────────────────
    batter_adv: dict[int, dict] = {}  # mlbam_id → partial advanced_stats

    try:
        exp_df = pybaseball.statcast_batter_expected_stats(season)
        logger.info("Statcast batter expected stats: %d rows", len(exp_df))
        for _, row in exp_df.iterrows():
            mlbam = row.get("player_id")
            if mlbam is None:
                continue
            try:
                mlbam = int(float(mlbam))
            except (TypeError, ValueError):
                continue
            batter_adv.setdefault(mlbam, {}).update({
                "xwOBA": _fval_sc(row.to_dict(), "est_woba"),
                "xBA":   _fval_sc(row.to_dict(), "est_ba"),
                "xSLG":  _fval_sc(row.to_dict(), "est_slg"),
            })
    except Exception:
        logger.warning("Statcast batter expected stats fetch failed", exc_info=True)

    try:
        ev_df = pybaseball.statcast_batter_exitvelo_barrels(season)
        logger.info("Statcast batter exit velo: %d rows", len(ev_df))
        for _, row in ev_df.iterrows():
            mlbam = row.get("player_id")
            if mlbam is None:
                continue
            try:
                mlbam = int(float(mlbam))
            except (TypeError, ValueError):
                continue
            d = row.to_dict()
            # Baseball Savant returns brl_percent / ev95percent already as
            # percentage numbers (e.g. 11.0 for 11%) — store as-is so the UI
            # can format with one decimal place.
            def _pct(v): return round(v, 2) if v is not None else None
            batter_adv.setdefault(mlbam, {}).update({
                "Barrel%":  _pct(_fval_sc(d, "brl_percent")),
                "HardHit%": _pct(_fval_sc(d, "ev95percent")),
                "EV":       _fval_sc(d, "avg_hit_speed"),
                # More predictive power metrics (per advanced stats framework)
                "EV_FBLD":  _fval_sc(d, "fbld"),        # EV on fly balls / line drives
                "MaxEV":    _fval_sc(d, "max_hit_speed"),
                "EV50":     _fval_sc(d, "ev50"),         # avg of top 50% batted balls
            })
    except Exception:
        logger.warning("Statcast batter exit velo fetch failed", exc_info=True)

    # Bat tracking: bat speed, fast swing rate, blast rate (Hawkeye, 2023+)
    try:
        import csv
        import io
        import urllib.request
        _bt_url = (
            "https://baseballsavant.mlb.com/leaderboard/bat-tracking"
            "?attackZone=&batSide=&contactType=&count=&dateStart="
            f"{season}-03-20&dateEnd={season}-12-01&gameType=&isHardHit="
            f"&minSwings=25&minGroupSwings=1&pitchType=&seasonEnd={season}"
            f"&seasonStart={season}&team=&type=batter&csv=true"
        )
        req = urllib.request.Request(_bt_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(raw))
        bt_count = 0
        for row in reader:
            mlbam_raw = row.get("id") or row.get("player_id")
            if not mlbam_raw:
                continue
            try:
                mlbam = int(float(str(mlbam_raw).strip()))
            except (TypeError, ValueError):
                continue

            def _fv_bt(key: str) -> Optional[float]:
                v = row.get(key, "").strip()
                if not v:
                    return None
                try:
                    import math
                    f = float(v)
                    return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
                except (TypeError, ValueError):
                    return None

            batter_adv.setdefault(mlbam, {}).update({
                "BatSpeed":   _fv_bt("avg_bat_speed"),
                "FastSwing%": _fv_bt("hard_swing_rate"),
                "Blast%":     _fv_bt("blast_per_swing"),
                "SquaredUp%": _fv_bt("squared_up_per_swing"),
            })
            bt_count += 1
        logger.info("Statcast bat tracking: %d rows", bt_count)
    except Exception:
        logger.warning("Statcast bat tracking fetch failed (non-fatal)", exc_info=True)

    # Sprint speed
    try:
        ss_df = pybaseball.statcast_sprint_speed(season)
        logger.info("Statcast sprint speed: %d rows", len(ss_df))
        for _, row in ss_df.iterrows():
            mlbam = row.get("player_id")
            if mlbam is None:
                continue
            try:
                mlbam = int(float(mlbam))
            except (TypeError, ValueError):
                continue
            d = row.to_dict()
            batter_adv.setdefault(mlbam, {}).update({
                "SprintSpeed": _fval_sc(d, "sprint_speed"),
            })
    except Exception:
        logger.warning("Statcast sprint speed fetch failed (non-fatal)", exc_info=True)

    for mlbam, adv in batter_adv.items():
        player_ids = mlbam_to_player_ids.get(mlbam)
        if not player_ids:
            continue
        for player_id in player_ids:
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
                continue  # No base stats row yet — MLB API sync must run first
            # Merge: preserve any existing keys not in this update (e.g. wRC+ if FanGraphs ran)
            merged = dict(existing.advanced_stats or {})
            merged.update({k: v for k, v in adv.items() if v is not None})
            existing.advanced_stats = merged
            total_updated += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("sync_statcast_advanced_stats: batting commit failed", exc_info=True)

    # ── Pitching ─────────────────────────────────────────────────────────────
    pitcher_adv: dict[int, dict] = {}

    try:
        exp_df = pybaseball.statcast_pitcher_expected_stats(season)
        logger.info("Statcast pitcher expected stats: %d rows", len(exp_df))
        for _, row in exp_df.iterrows():
            mlbam = row.get("player_id")
            if mlbam is None:
                continue
            try:
                mlbam = int(float(mlbam))
            except (TypeError, ValueError):
                continue
            pitcher_adv.setdefault(mlbam, {}).update({
                "xERA": _fval_sc(row.to_dict(), "xera"),
            })
    except Exception:
        logger.warning("Statcast pitcher expected stats fetch failed", exc_info=True)

    try:
        ev_df = pybaseball.statcast_pitcher_exitvelo_barrels(season)
        logger.info("Statcast pitcher exit velo: %d rows", len(ev_df))
        for _, row in ev_df.iterrows():
            mlbam = row.get("player_id")
            if mlbam is None:
                continue
            try:
                mlbam = int(float(mlbam))
            except (TypeError, ValueError):
                continue
            d = row.to_dict()
            def _pct(v): return round(v, 2) if v is not None else None
            pitcher_adv.setdefault(mlbam, {}).update({
                "Barrel%":  _pct(_fval_sc(d, "brl_percent")),
                "HardHit%": _pct(_fval_sc(d, "ev95percent")),
                "EV":       _fval_sc(d, "avg_hit_speed"),
            })
    except Exception:
        logger.warning("Statcast pitcher exit velo fetch failed", exc_info=True)

    for mlbam, adv in pitcher_adv.items():
        player_ids = mlbam_to_player_ids.get(mlbam)
        if not player_ids:
            continue
        for player_id in player_ids:
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
                continue
            merged = dict(existing.advanced_stats or {})
            merged.update({k: v for k, v in adv.items() if v is not None})
            existing.advanced_stats = merged
            total_updated += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("sync_statcast_advanced_stats: pitching commit failed", exc_info=True)

    logger.info("sync_statcast_advanced_stats: updated %d stat rows for season %s", total_updated, season)
    return total_updated


def sync_savant_pitch_arsenal(db: Session, season: int = 2026) -> int:
    """Fetch pitch quality metrics from Baseball Savant and populate advanced_stats.

    Two Savant endpoints (no FanGraphs dependency):
      1. pitch-movement leaderboard (FF + SI): primary fastball avg velocity → vFA
      2. pitch-arsenal-stats leaderboard: pitch-count-weighted run value / 100 → PitchRV100

    PitchRV100 sign: positive = pitcher creates value (good), negative = batter creates value (bad).
    Stored in advanced_stats for existing actual pitching rows only.

    Returns: number of PlayerStats rows updated.
    """
    import csv
    import io
    import math
    import urllib.request

    rows_with_mlbam = db.query(Player.player_id, Player.mlbam_id).filter(Player.mlbam_id.isnot(None)).all()
    mlbam_to_player_ids: dict[int, list[int]] = {}
    for r in rows_with_mlbam:
        mlbam_to_player_ids.setdefault(r.mlbam_id, []).append(r.player_id)
    if not mlbam_to_player_ids:
        logger.warning("sync_savant_pitch_arsenal: no players with mlbam_id — skipping")
        return 0

    def _fetch_csv(url: str) -> list[dict]:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8-sig")
            return list(csv.DictReader(io.StringIO(raw)))
        except Exception as e:
            logger.warning("sync_savant_pitch_arsenal: fetch failed for %s: %s", url, e)
            return []

    def _fv(val: str) -> Optional[float]:
        try:
            f = float(val)
            return None if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return None

    # ── Step 1: Primary fastball velocity from pitch-movement ─────────────────
    # For each pitcher, take the fastball type (FF or SI) with the higher pitch count
    # as their primary. This handles both 4-seam and sinker-primary pitchers.
    fastball_velo: dict[int, float] = {}  # mlbam_id → avg velocity

    for pitch_type in ("FF", "SI"):
        url = (
            f"https://baseballsavant.mlb.com/leaderboard/pitch-movement"
            f"?year={season}&team=&min=10&pitch_type={pitch_type}&type=pitcher&csv=true"
        )
        for row in _fetch_csv(url):
            try:
                mlbam = int(float(row.get("pitcher_id") or 0))
            except (TypeError, ValueError):
                continue
            if mlbam == 0 or mlbam not in mlbam_to_player_ids:
                continue
            speed = _fv(row.get("avg_speed"))
            pitches = int(float(row.get("pitches_thrown") or 0))
            if speed is None or pitches == 0:
                continue
            # Keep the fastball type with more pitches (pitcher's primary)
            if mlbam not in fastball_velo or pitches > fastball_velo.get(mlbam, (0, 0))[1]:  # type: ignore[index]
                fastball_velo[mlbam] = (speed, pitches)  # type: ignore[assignment]

    # Unwrap to just velocity
    fastball_velo_final: dict[int, float] = {
        mlbam: v[0] for mlbam, v in fastball_velo.items()  # type: ignore[index]
    }
    logger.info("sync_savant_pitch_arsenal: got fastball velocity for %d pitchers", len(fastball_velo_final))

    # ── Step 2: Weighted-average run value / 100 from pitch-arsenal-stats ─────
    # Aggregate all pitch types: sum(rv100_i × pitches_i) / sum(pitches_i)
    # Positive RV100 = pitch creates positive value for pitcher (good).
    pitch_totals: dict[int, tuple[float, float]] = {}  # mlbam → (weighted_rv_sum, total_pitches)

    url = (
        f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats"
        f"?type=pitcher&min=10&team=&season={season}&range=year&csv=true"
    )
    for row in _fetch_csv(url):
        try:
            mlbam = int(float(row.get("player_id") or 0))
        except (TypeError, ValueError):
            continue
        if mlbam == 0 or mlbam not in mlbam_to_player_ids:
            continue
        rv100 = _fv(row.get("run_value_per_100"))
        pitches = _fv(row.get("pitches"))
        if rv100 is None or pitches is None or pitches <= 0:
            continue
        prev_rv, prev_p = pitch_totals.get(mlbam, (0.0, 0.0))
        pitch_totals[mlbam] = (prev_rv + rv100 * pitches, prev_p + pitches)

    rv100_by_mlbam: dict[int, float] = {
        mlbam: round(rv_sum / total_p, 3)
        for mlbam, (rv_sum, total_p) in pitch_totals.items()
        if total_p > 0
    }
    logger.info("sync_savant_pitch_arsenal: got PitchRV100 for %d pitchers", len(rv100_by_mlbam))

    # ── Step 3: Merge into existing actual pitching rows ──────────────────────
    total_updated = 0
    all_mlbam = set(fastball_velo_final) | set(rv100_by_mlbam)

    for mlbam in all_mlbam:
        player_ids = mlbam_to_player_ids.get(mlbam)
        if not player_ids:
            continue
        for player_id in player_ids:
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
                continue
            merged = dict(existing.advanced_stats or {})
            if mlbam in fastball_velo_final:
                merged["vFA"] = fastball_velo_final[mlbam]
            if mlbam in rv100_by_mlbam:
                merged["PitchRV100"] = rv100_by_mlbam[mlbam]
            existing.advanced_stats = merged
            total_updated += 1

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("sync_savant_pitch_arsenal: commit failed", exc_info=True)

    logger.info("sync_savant_pitch_arsenal: updated %d pitching rows", total_updated)
    return total_updated


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

        bat_so = _fv(stat, "strikeOuts") or 0.0
        bat_bb = _fv(stat, "baseOnBalls") or 0.0
        counting_stats = {
            "PA":  pa,
            "AB":  _fv(stat, "atBats"),
            "H":   _fv(stat, "hits"),
            "HR":  _fv(stat, "homeRuns"),
            "R":   _fv(stat, "runs"),
            "RBI": _fv(stat, "rbi"),
            "SB":  _fv(stat, "stolenBases"),
            "BB":  bat_bb,
            "SO":  bat_so,
            "2B":  _fv(stat, "doubles"),
            "3B":  _fv(stat, "triples"),
        }
        rate_stats = {
            "AVG": _fv(stat, "avg"),
            "OBP": _fv(stat, "obp"),
            "SLG": _fv(stat, "slg"),
            "OPS": _fv(stat, "ops"),
            # Derived from counting stats — K% and BB% needed for Statcast composite
            "K%":  round(bat_so / pa, 4) if pa and pa > 0 else None,
            "BB%": round(bat_bb / pa, 4) if pa and pa > 0 else None,
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

        # K/9, BB/9, K%, BB%, K-BB% all derived from raw totals
        so = _fv(stat, "strikeOuts") or 0.0
        bb = _fv(stat, "baseOnBalls") or 0.0
        bf = _fv(stat, "battersFaced") or None
        k9 = round(so / ip * 9, 2) if ip > 0 else None
        bb9 = round(bb / ip * 9, 2) if ip > 0 else None
        kbb_pct = round((so - bb) / max(1, bf or 1), 4) if ip > 0 else None
        k_pct  = round(so / bf, 4) if bf and bf > 0 else None
        bb_pct = round(bb / bf, 4) if bf and bf > 0 else None

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
            # K% and BB% derived from battersFaced — not used in current pitcher
            # composites but stored for completeness and future use
            "K%":    k_pct,
            "BB%":   bb_pct,
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


def sync_derived_rate_stats(db: Session, season: int = 2026) -> int:
    """Backfill K% and BB% into rate_stats for actual rows that are missing them.

    Batters: K% = SO / PA  (exact)
             BB% = BB / PA  (exact)
    Pitchers: K% = SO / BF  (exact, BF = battersFaced stored in K-BB% derivation)
              BB% = BB / BF  (exact)
              BF is approximated as IP*(3+WHIP) when not available.

    This is needed because FanGraphs is blocked (403) and the MLB Stats API
    sync only added K%/BB% to rate_stats after June 2026.  Existing rows were
    stored without them.  This function patches the JSON in-place.

    Idempotent — rows that already have K% and BB% set are skipped.

    Returns:
        Number of rows patched.
    """
    import math
    from sqlalchemy.orm.attributes import flag_modified

    def _safe_float(v: object) -> Optional[float]:
        if v is None:
            return None
        try:
            f = float(v)  # type: ignore[arg-type]
            return None if (math.isnan(f) or math.isinf(f)) else f
        except (TypeError, ValueError):
            return None

    patched = 0

    # ── Batters ───────────────────────────────────────────────────────────────
    bat_rows = (
        db.query(PlayerStats)
        .filter(
            and_(
                PlayerStats.stat_type == "batting",
                PlayerStats.data_source == "actual",
                PlayerStats.season == season,
                PlayerStats.week.is_(None),
            )
        )
        .all()
    )
    for row in bat_rows:
        cs = row.counting_stats or {}
        rs = row.rate_stats or {}
        # Only patch if K% or BB% is missing
        if rs.get("K%") is not None and rs.get("BB%") is not None:
            continue
        pa = _safe_float(cs.get("PA"))
        so = _safe_float(cs.get("SO"))
        bb = _safe_float(cs.get("BB"))
        if not pa or pa <= 0:
            continue
        new_rs = dict(rs)
        if new_rs.get("K%") is None and so is not None:
            new_rs["K%"] = round(so / pa, 4)
        if new_rs.get("BB%") is None and bb is not None:
            new_rs["BB%"] = round(bb / pa, 4)
        row.rate_stats = new_rs
        flag_modified(row, "rate_stats")
        patched += 1

    # ── Pitchers ──────────────────────────────────────────────────────────────
    pit_rows = (
        db.query(PlayerStats)
        .filter(
            and_(
                PlayerStats.stat_type == "pitching",
                PlayerStats.data_source == "actual",
                PlayerStats.season == season,
                PlayerStats.week.is_(None),
            )
        )
        .all()
    )
    for row in pit_rows:
        cs = row.counting_stats or {}
        rs = row.rate_stats or {}
        if rs.get("K%") is not None and rs.get("BB%") is not None:
            continue
        so = _safe_float(cs.get("SO")) or _safe_float(cs.get("K"))
        bb = _safe_float(cs.get("BB"))
        ip = _safe_float(cs.get("IP"))
        whip = _safe_float(rs.get("WHIP"))
        if not ip or ip <= 0 or so is None or bb is None:
            continue
        # Approximate BF = IP*(3 + WHIP).  WHIP = (H+BB)/IP → H = WHIP*IP - BB
        # BF = outs + H + BB = IP*3 + H + BB = IP*(3 + WHIP)
        if whip is not None and whip > 0:
            bf = ip * (3.0 + whip)
        else:
            # Fallback: use K/9 and BB/9 to estimate BF ≈ IP*4 (rough)
            bf = ip * 4.0
        if bf <= 0:
            continue
        new_rs = dict(rs)
        if new_rs.get("K%") is None:
            new_rs["K%"] = round(so / bf, 4)
        if new_rs.get("BB%") is None:
            new_rs["BB%"] = round(bb / bf, 4)
        row.rate_stats = new_rs
        flag_modified(row, "rate_stats")
        patched += 1

    try:
        db.commit()
        logger.info("sync_derived_rate_stats: patched %d rows for season %d", patched, season)
    except Exception:
        db.rollback()
        logger.error("sync_derived_rate_stats: commit failed", exc_info=True)
        return 0

    return patched
