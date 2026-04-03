"""Multi-system consensus projection fetcher.

Fetches 2026 projections from multiple FanGraphs systems and merges them
on a per-category basis, choosing the historically most accurate source
for each stat.  Research from whiffs.org (2025 season analysis):

  - R, RBI, K, SV, IP  → ATC  (wins each category outright)
  - SB                  → ZiPS ("best every year")
  - AVG, OBP, SLG, OPS → The BAT (best batting-average projection)
  - PA (playing time)   → Steamer (best PA projection + widest coverage)
  - W                   → Steamer
  - ERA, WHIP           → ATC (OOPSY wins but isn't publicly available)
  - Prospects / MiLB    → Steamer only (sa* IDs; only system with coverage)

Usage::

    from fantasai.adapters.projections import fetch_consensus_batting, fetch_consensus_pitching

    batters  = fetch_consensus_batting(season=2026)
    pitchers = fetch_consensus_pitching(season=2026)

The returned NormalizedPlayerData objects are identical in shape to the
single-system fetchers they replace — the rest of the scoring pipeline
does not need to change.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import requests

from fantasai.adapters.base import NormalizedPlayerData

logger = logging.getLogger(__name__)

_FG_PROJECTIONS_URL = "https://www.fangraphs.com/api/projections"
_TIMEOUT = 25

# ---------------------------------------------------------------------------
# Position helpers
# ---------------------------------------------------------------------------

_OF_POSITIONS = {"LF", "CF", "RF"}
_VALID_POSITIONS = {"C", "1B", "2B", "SS", "3B", "OF", "DH", "SP", "RP"}


def _normalise_position(minpos: Any) -> str:
    if not minpos or not isinstance(minpos, str):
        return ""
    pos = minpos.strip().upper()
    if pos in _OF_POSITIONS:
        return "OF"
    return pos if pos in _VALID_POSITIONS else ""


def _infer_pitcher_position(gs: float | None, g: float | None) -> str:
    gs = gs or 0.0
    g = g or 0.0
    if g == 0:
        return "RP"
    return "SP" if gs / g >= 0.4 else "RP"


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError):
        return None


def _parse_player_id(pid_raw: Any) -> int | None:
    """Parse FanGraphs playerid to int.

    FanGraphs uses integer IDs for MLB players and "sa{number}" prefix IDs
    for minor-leaguers / prospects (e.g. "sa3022895").  The numeric part of
    sa* IDs is in the 3,000,000+ range and never collides with MLB IDs.
    """
    if not pid_raw:
        return None
    try:
        raw_id = str(pid_raw).split(",")[0].strip()
        if raw_id.startswith("sa"):
            return int(raw_id[2:])
        return int(raw_id)
    except (ValueError, TypeError):
        return None


def _extract(row: dict, keys: list[str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for k in keys:
        v = _safe_float(row.get(k))
        if v is not None:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Raw system fetchers  (return dicts keyed by player_id)
# ---------------------------------------------------------------------------

_FG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fangraphs.com/projections.aspx",
    "Origin": "https://www.fangraphs.com",
}


def _fetch_raw(system: str, stats: str) -> dict[int, dict]:
    """Fetch one system/stats combo and return {player_id: row} dict."""
    try:
        resp = requests.get(
            _FG_PROJECTIONS_URL,
            params={"type": system, "stats": stats, "pos": "all",
                    "team": 0, "lg": "all", "players": 0},
            headers=_FG_HEADERS,
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        raw: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch %s/%s: %s", system, stats, exc)
        return {}

    result: dict[int, dict] = {}
    for row in raw:
        pid = _parse_player_id(row.get("playerid") or row.get("playerids"))
        if pid is not None and pid not in result:
            result[pid] = row
    logger.info("  %s/%s: %d rows", system, stats, len(result))
    return result


# ---------------------------------------------------------------------------
# Batting consensus
# ---------------------------------------------------------------------------

# Per-stat best system (based on whiffs.org 2025 accuracy analysis).
# Key: stat name in the FanGraphs API response.
# Value: which system to prefer for that stat.
_BAT_STAT_SYSTEM: dict[str, str] = {
    # Playing time — Steamer is the best PA projector
    "PA": "steamer",
    "AB": "steamer",
    # Rate stats — The BAT is most accurate for AVG/OBP/SLG
    "AVG": "thebat",
    "OBP": "thebat",
    "SLG": "thebat",
    "OPS": "thebat",
    "ISO": "thebat",
    "BABIP": "thebat",
    # Stolen bases — ZiPS wins every year
    "SB": "zips",
    "CS": "zips",
    # Everything else → ATC (wins R, RBI, K/SO)
}
_BAT_STAT_DEFAULT = "atc"

_BAT_COUNTING = ["PA", "AB", "R", "H", "HR", "RBI", "SB", "CS", "BB", "SO",
                 "1B", "2B", "3B"]
_BAT_RATE     = ["AVG", "OBP", "SLG", "OPS", "BB%", "K%", "ISO", "BABIP"]
_BAT_ADVANCED = ["wOBA", "wRC+", "Spd", "WAR"]


def fetch_consensus_batting(season: int = 2026) -> list[NormalizedPlayerData]:
    """Return one NormalizedPlayerData per batter, using per-stat best system.

    Falls back gracefully when a system is unavailable or doesn't include
    a given player (e.g. prospects only in Steamer).
    """
    logger.info("Fetching consensus batting projections (season=%d)...", season)

    steamer = _fetch_raw("steamer", "bat")
    atc     = _fetch_raw("atc",     "bat")
    zips    = _fetch_raw("zips",    "bat")
    thebat  = _fetch_raw("thebat",  "bat")

    system_map = {"steamer": steamer, "atc": atc, "zips": zips, "thebat": thebat}

    # All player IDs across all systems — Steamer has the broadest coverage
    all_ids = set(steamer) | set(atc) | set(zips) | set(thebat)
    logger.info("  Total unique batting players: %d", len(all_ids))

    players: list[NormalizedPlayerData] = []

    for pid in all_ids:
        # Require at least 50 PA from whichever system knows this player best
        # (prefer ATC → Steamer → ZiPS → TheBat for PA threshold check)
        ref_row = (atc.get(pid) or steamer.get(pid)
                   or zips.get(pid) or thebat.get(pid))
        if ref_row is None:
            continue
        pa_check = _safe_float(ref_row.get("PA"))
        if pa_check is None or pa_check < 50:
            continue

        # Identity: prefer ATC → Steamer → others (most curated first)
        id_row = atc.get(pid) or steamer.get(pid) or zips.get(pid) or thebat.get(pid)
        name = str(id_row.get("PlayerName", "")).strip()
        team = str(id_row.get("Team", "")).strip()
        pos_str = _normalise_position(id_row.get("minpos") or id_row.get("Pos"))
        positions = [pos_str] if pos_str else []

        # Build merged counting stats: pick best system per key
        counting: dict[str, float] = {}
        for key in _BAT_COUNTING:
            preferred = _BAT_STAT_SYSTEM.get(key, _BAT_STAT_DEFAULT)
            for sys_name in [preferred, "atc", "steamer", "zips", "thebat"]:
                row = system_map[sys_name].get(pid)
                if row is not None:
                    v = _safe_float(row.get(key))
                    if v is not None:
                        counting[key] = v
                        break

        # SO → K alias
        if "SO" in counting:
            counting["K"] = counting.pop("SO")

        # Build merged rate stats
        rate: dict[str, float] = {}
        for key in _BAT_RATE:
            preferred = _BAT_STAT_SYSTEM.get(key, _BAT_STAT_DEFAULT)
            for sys_name in [preferred, "thebat", "atc", "steamer", "zips"]:
                row = system_map[sys_name].get(pid)
                if row is not None:
                    v = _safe_float(row.get(key))
                    if v is not None:
                        rate[key] = v
                        break

        # Advanced stats — ATC → Steamer (ATC has wRC+, Spd)
        advanced: dict[str, float] = {}
        for key in _BAT_ADVANCED:
            for sys_name in ["atc", "steamer", "zips", "thebat"]:
                row = system_map[sys_name].get(pid)
                if row is not None:
                    v = _safe_float(row.get(key))
                    if v is not None:
                        advanced[key] = v
                        break

        players.append(NormalizedPlayerData(
            player_id=pid,
            name=name,
            team=team,
            positions=positions,
            stat_type="batting",
            counting_stats=counting,
            rate_stats=rate,
            advanced_stats=advanced,
        ))

    logger.info("  Consensus batting records: %d", len(players))
    return players


# ---------------------------------------------------------------------------
# Pitching consensus
# ---------------------------------------------------------------------------

_PITCH_STAT_SYSTEM: dict[str, str] = {
    # ERA, WHIP, K, SV, IP → ATC (OOPSY wins ERA/WHIP but isn't publicly available)
    "ERA":  "atc",
    "WHIP": "atc",
    "SO":   "atc",
    "K":    "atc",
    "SV":   "atc",
    "HLD":  "atc",
    "IP":   "atc",
    # Wins → Steamer (specifically mentioned as best)
    "W":    "steamer",
    # K/9, BB/9 → ATC (derived from K/SO which ATC wins)
    "K/9":  "atc",
    "BB/9": "atc",
}
_PITCH_STAT_DEFAULT = "atc"

_PITCH_COUNTING = ["IP", "W", "L", "SV", "HLD", "G", "GS", "BB", "SO", "H",
                   "HR", "ER"]
_PITCH_RATE     = ["ERA", "WHIP", "K/9", "BB/9", "K/BB", "HR/9", "K%", "BB%",
                   "GB%", "BABIP", "LOB%"]
_PITCH_ADVANCED = ["FIP", "WAR", "QS", "K-BB%"]


def fetch_consensus_pitching(season: int = 2026) -> list[NormalizedPlayerData]:
    """Return one NormalizedPlayerData per pitcher, using per-stat best system."""
    logger.info("Fetching consensus pitching projections (season=%d)...", season)

    steamer = _fetch_raw("steamer", "pit")
    atc     = _fetch_raw("atc",     "pit")
    zips    = _fetch_raw("zips",    "pit")

    system_map = {"steamer": steamer, "atc": atc, "zips": zips, "thebat": {}}

    all_ids = set(steamer) | set(atc) | set(zips)
    logger.info("  Total unique pitching players: %d", len(all_ids))

    players: list[NormalizedPlayerData] = []

    for pid in all_ids:
        ref_row = atc.get(pid) or steamer.get(pid) or zips.get(pid)
        if ref_row is None:
            continue
        ip_check = _safe_float(ref_row.get("IP"))
        if ip_check is None or ip_check < 10:
            continue

        id_row = atc.get(pid) or steamer.get(pid) or zips.get(pid)
        name = str(id_row.get("PlayerName", "")).strip()
        team = str(id_row.get("Team", "")).strip()

        # SP/RP: prefer ATC → Steamer for GS/G data
        for sys_name in ["atc", "steamer", "zips"]:
            row = system_map[sys_name].get(pid)
            if row is not None:
                gs = _safe_float(row.get("GS"))
                g  = _safe_float(row.get("G"))
                if g is not None:
                    pos_label = _infer_pitcher_position(gs, g)
                    break
        else:
            pos_label = "RP"
        positions = [pos_label]

        counting: dict[str, float] = {}
        for key in _PITCH_COUNTING:
            preferred = _PITCH_STAT_SYSTEM.get(key, _PITCH_STAT_DEFAULT)
            for sys_name in [preferred, "atc", "steamer", "zips"]:
                row = system_map[sys_name].get(pid)
                if row is not None:
                    v = _safe_float(row.get(key))
                    if v is not None:
                        counting[key] = v
                        break

        if "SO" in counting:
            counting["K"] = counting.pop("SO")

        rate: dict[str, float] = {}
        for key in _PITCH_RATE:
            preferred = _PITCH_STAT_SYSTEM.get(key, _PITCH_STAT_DEFAULT)
            for sys_name in [preferred, "atc", "steamer", "zips"]:
                row = system_map[sys_name].get(pid)
                if row is not None:
                    v = _safe_float(row.get(key))
                    if v is not None:
                        rate[key] = v
                        break

        advanced: dict[str, float] = {}
        for key in _PITCH_ADVANCED:
            for sys_name in ["atc", "steamer", "zips"]:
                row = system_map[sys_name].get(pid)
                if row is not None:
                    v = _safe_float(row.get(key))
                    if v is not None:
                        advanced[key] = v
                        break

        players.append(NormalizedPlayerData(
            player_id=pid,
            name=name,
            team=team,
            positions=positions,
            stat_type="pitching",
            counting_stats=counting,
            rate_stats=rate,
            advanced_stats=advanced,
        ))

    logger.info("  Consensus pitching records: %d", len(players))
    return players


# ---------------------------------------------------------------------------
# Legacy single-system fetchers (kept for backward compat / testing)
# ---------------------------------------------------------------------------

def fetch_steamer_batting(season: int = 2026) -> list[NormalizedPlayerData]:
    """Single-system Steamer batting fetch.  Prefer fetch_consensus_batting()."""
    logger.info("Fetching Steamer batting projections (season=%d)...", season)
    raw_map = _fetch_raw("steamer", "bat")
    players: list[NormalizedPlayerData] = []
    for pid, row in raw_map.items():
        pa = _safe_float(row.get("PA"))
        if pa is None or pa < 50:
            continue
        pos_str = _normalise_position(row.get("minpos"))
        counting = _extract(row, _BAT_COUNTING)
        if "SO" in counting:
            counting["K"] = counting.pop("SO")
        players.append(NormalizedPlayerData(
            player_id=pid,
            name=str(row.get("PlayerName", "")).strip(),
            team=str(row.get("Team", "")).strip(),
            positions=[pos_str] if pos_str else [],
            stat_type="batting",
            counting_stats=counting,
            rate_stats=_extract(row, _BAT_RATE),
            advanced_stats=_extract(row, _BAT_ADVANCED),
        ))
    logger.info("  Normalized batting records: %d", len(players))
    return players


def fetch_steamer_pitching(season: int = 2026) -> list[NormalizedPlayerData]:
    """Single-system Steamer pitching fetch.  Prefer fetch_consensus_pitching()."""
    logger.info("Fetching Steamer pitching projections (season=%d)...", season)
    raw_map = _fetch_raw("steamer", "pit")
    players: list[NormalizedPlayerData] = []
    for pid, row in raw_map.items():
        ip = _safe_float(row.get("IP"))
        if ip is None or ip < 10:
            continue
        pos_label = _infer_pitcher_position(
            _safe_float(row.get("GS")), _safe_float(row.get("G"))
        )
        counting = _extract(row, _PITCH_COUNTING)
        if "SO" in counting:
            counting["K"] = counting.pop("SO")
        players.append(NormalizedPlayerData(
            player_id=pid,
            name=str(row.get("PlayerName", "")).strip(),
            team=str(row.get("Team", "")).strip(),
            positions=[pos_label],
            stat_type="pitching",
            counting_stats=counting,
            rate_stats=_extract(row, _PITCH_RATE),
            advanced_stats=_extract(row, _PITCH_ADVANCED),
        ))
    logger.info("  Normalized pitching records: %d", len(players))
    return players
