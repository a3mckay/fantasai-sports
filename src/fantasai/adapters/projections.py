"""FanGraphs Steamer projection fetcher.

Fetches 2026 Steamer batting and pitching projections via the FanGraphs
undocumented JSON API and normalizes them into NormalizedPlayerData format
for storage in PlayerStats with season=2026.

Usage::

    from fantasai.adapters.projections import fetch_steamer_batting, fetch_steamer_pitching

    batters = fetch_steamer_batting(season=2026)
    pitchers = fetch_steamer_pitching(season=2026)
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from fantasai.adapters.base import NormalizedPlayerData

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FanGraphs projection API
# ---------------------------------------------------------------------------

_FG_PROJECTIONS_URL = "https://www.fangraphs.com/api/projections"

# Default request timeout (seconds).  FanGraphs is generally fast (<3s) but
# can be slow during peak hours.
_TIMEOUT = 20

# Position normalization: map FanGraphs minpos values to our canonical list.
# FanGraphs uses full names (e.g. "LF", "CF", "RF") for OF sub-positions.
_OF_POSITIONS = {"LF", "CF", "RF"}
_VALID_POSITIONS = {"C", "1B", "2B", "SS", "3B", "OF", "DH", "SP", "RP"}


def _normalise_position(minpos: Any) -> str:
    """Convert FanGraphs minpos to a canonical fantasy position."""
    if not minpos or not isinstance(minpos, str):
        return ""
    pos = minpos.strip().upper()
    if pos in _OF_POSITIONS:
        return "OF"
    if pos in _VALID_POSITIONS:
        return pos
    return ""


def _infer_pitcher_position(gs: float | None, g: float | None) -> str:
    """Infer SP or RP from games started vs total games (same logic as MLBAdapter)."""
    gs = gs or 0.0
    g = g or 0.0
    if g == 0:
        return "RP"
    return "SP" if gs / g >= 0.4 else "RP"


def _safe_float(val: Any) -> float | None:
    """Return float or None — never 0.0 for missing data."""
    if val is None:
        return None
    try:
        f = float(val)
        import math
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _extract(row: dict, keys: list[str]) -> dict[str, float]:
    """Pull named keys from a dict, skipping None / non-numeric values."""
    result: dict[str, float] = {}
    for k in keys:
        v = _safe_float(row.get(k))
        if v is not None:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Batting column mappings
# ---------------------------------------------------------------------------

# Counting stats we care about for fantasy scoring
_BAT_COUNTING = ["PA", "AB", "R", "H", "HR", "RBI", "SB", "CS", "BB", "SO",
                 "1B", "2B", "3B"]

# Rate stats
_BAT_RATE = ["AVG", "OBP", "SLG", "OPS", "BB%", "K%", "ISO", "BABIP"]

# Advanced / context stats
_BAT_ADVANCED = ["wOBA", "wRC+", "Spd", "WAR"]


def fetch_steamer_batting(season: int = 2026) -> list[NormalizedPlayerData]:
    """Fetch Steamer batting projections from FanGraphs API.

    Returns one NormalizedPlayerData per player (deduplicated by playerid).
    Skips records with missing playerid or fewer than 50 projected PA.

    Args:
        season: The target season year (used for logging; projections are
                always "next season" from FanGraphs' perspective).
    """
    logger.info("Fetching Steamer batting projections (season=%d)...", season)
    try:
        resp = requests.get(
            _FG_PROJECTIONS_URL,
            params={
                "type": "steamer",
                "stats": "bat",
                "pos": "all",
                "team": 0,
                "lg": "all",
                "players": 0,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        raw: list[dict] = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch Steamer batting projections: %s", exc)
        raise

    logger.info("  Raw batting records: %d", len(raw))

    seen: set[int] = set()
    players: list[NormalizedPlayerData] = []

    for row in raw:
        pid_raw = row.get("playerid") or row.get("playerids")
        if not pid_raw:
            continue
        try:
            raw_id = str(pid_raw).split(",")[0].strip()
            # MiLB / prospect IDs use a "sa" prefix (e.g. "sa3022895").
            # Strip it and use the numeric part — these numbers are in the
            # 3,000,000+ range and never collide with FanGraphs integer IDs
            # (which top out around 25,000 for current players).
            if raw_id.startswith("sa"):
                player_id = int(raw_id[2:])
            else:
                player_id = int(raw_id)
        except (ValueError, TypeError):
            continue

        if player_id in seen:
            continue

        # Skip players with very few projected PA (noise / non-roster noise)
        pa = _safe_float(row.get("PA"))
        if pa is None or pa < 50:
            continue

        seen.add(player_id)

        name = str(row.get("PlayerName", "")).strip()
        team = str(row.get("Team", "")).strip()

        pos_str = _normalise_position(row.get("minpos"))
        positions = [pos_str] if pos_str else []

        counting = _extract(row, _BAT_COUNTING)
        # Rename SO → K to match our schema (ScoringEngine expects "K" for strikeouts)
        if "SO" in counting:
            counting["K"] = counting.pop("SO")

        rate = _extract(row, _BAT_RATE)
        advanced = _extract(row, _BAT_ADVANCED)

        players.append(
            NormalizedPlayerData(
                player_id=player_id,
                name=name,
                team=team,
                positions=positions,
                stat_type="batting",
                counting_stats=counting,
                rate_stats=rate,
                advanced_stats=advanced,
            )
        )

    logger.info("  Normalized batting records: %d", len(players))
    return players


# ---------------------------------------------------------------------------
# Pitching column mappings
# ---------------------------------------------------------------------------

_PITCH_COUNTING = ["IP", "W", "L", "SV", "HLD", "G", "GS", "BB", "SO", "H",
                   "HR", "ER"]

_PITCH_RATE = ["ERA", "WHIP", "K/9", "BB/9", "K/BB", "HR/9", "K%", "BB%",
               "GB%", "BABIP", "LOB%"]

_PITCH_ADVANCED = ["FIP", "WAR", "QS", "K-BB%"]


def fetch_steamer_pitching(season: int = 2026) -> list[NormalizedPlayerData]:
    """Fetch Steamer pitching projections from FanGraphs API.

    Returns one NormalizedPlayerData per pitcher. Skips records with fewer
    than 10 projected IP (noise / non-roster arms).

    Args:
        season: Target season year (for logging only).
    """
    logger.info("Fetching Steamer pitching projections (season=%d)...", season)
    try:
        resp = requests.get(
            _FG_PROJECTIONS_URL,
            params={
                "type": "steamer",
                "stats": "pit",
                "pos": "all",
                "team": 0,
                "lg": "all",
                "players": 0,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        raw: list[dict] = resp.json()
    except Exception as exc:
        logger.error("Failed to fetch Steamer pitching projections: %s", exc)
        raise

    logger.info("  Raw pitching records: %d", len(raw))

    seen: set[int] = set()
    players: list[NormalizedPlayerData] = []

    for row in raw:
        pid_raw = row.get("playerid") or row.get("playerids")
        if not pid_raw:
            continue
        try:
            raw_id = str(pid_raw).split(",")[0].strip()
            if raw_id.startswith("sa"):
                player_id = int(raw_id[2:])
            else:
                player_id = int(raw_id)
        except (ValueError, TypeError):
            continue

        if player_id in seen:
            continue

        ip = _safe_float(row.get("IP"))
        if ip is None or ip < 10:
            continue

        seen.add(player_id)

        name = str(row.get("PlayerName", "")).strip()
        team = str(row.get("Team", "")).strip()

        # Infer SP vs RP from GS/G ratio
        gs = _safe_float(row.get("GS"))
        g = _safe_float(row.get("G"))
        pos_label = _infer_pitcher_position(gs, g)
        positions = [pos_label]

        counting = _extract(row, _PITCH_COUNTING)
        # Rename SO → K to match our schema
        if "SO" in counting:
            counting["K"] = counting.pop("SO")

        rate = _extract(row, _PITCH_RATE)
        advanced = _extract(row, _PITCH_ADVANCED)

        players.append(
            NormalizedPlayerData(
                player_id=player_id,
                name=name,
                team=team,
                positions=positions,
                stat_type="pitching",
                counting_stats=counting,
                rate_stats=rate,
                advanced_stats=advanced,
            )
        )

    logger.info("  Normalized pitching records: %d", len(players))
    return players
