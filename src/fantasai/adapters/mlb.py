from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import requests
from pybaseball import batting_stats, batting_stats_range, pitching_stats, pitching_stats_range

from fantasai.adapters.base import NormalizedPlayerData, SportAdapter

logger = logging.getLogger(__name__)


def _build_fg_to_mlbam(fg_ids: list[int]) -> dict[int, int]:
    """Return {fangraphs_id: mlbam_id} for the given FanGraphs IDs.

    Uses the Chadwick Bureau register (downloaded by pybaseball) which maps
    player IDs across all major baseball data systems.
    """
    try:
        import pybaseball as pb
        reg = pb.chadwick_register()
        fg_ids_set = set(fg_ids)
        subset = reg[reg["key_fangraphs"].isin(fg_ids_set)]
        mapping: dict[int, int] = {}
        for _, row in subset.iterrows():
            fg = int(row["key_fangraphs"]) if not pd.isna(row["key_fangraphs"]) else None
            mlbam = int(row["key_mlbam"]) if not pd.isna(row["key_mlbam"]) else None
            if fg and mlbam and mlbam > 0:
                mapping[fg] = mlbam
        return mapping
    except Exception as e:
        logger.warning("Chadwick register lookup failed: %s", e)
        return {}


def _fetch_mlb_positions(mlbam_ids: list[int]) -> dict[int, str]:
    """Return {mlbam_id: position_abbrev} by querying the MLB Stats API in batches."""
    result: dict[int, str] = {}
    batch_size = 100
    for i in range(0, len(mlbam_ids), batch_size):
        batch = mlbam_ids[i : i + batch_size]
        try:
            resp = requests.get(
                "https://statsapi.mlb.com/api/v1/people",
                params={
                    "personIds": ",".join(str(x) for x in batch),
                    "fields": "people,id,primaryPosition,abbreviation",
                },
                timeout=10,
            )
            resp.raise_for_status()
            for person in resp.json().get("people", []):
                pid = person.get("id")
                pos = person.get("primaryPosition", {}).get("abbreviation", "")
                if pid and pos:
                    result[pid] = pos
        except Exception as e:
            logger.warning("MLB API position fetch failed for batch: %s", e)
    return result


def _get_batter_positions(fg_ids: list[int]) -> dict[int, list[str]]:
    """Fetch real position abbreviations for a list of FanGraphs batter IDs.

    Returns {fangraphs_id: ["CF"], ...}.  Falls back to [] on any error.
    Maps OF sub-positions (LF/RF) to the fantasy-standard "OF".
    """
    # FanGraphs ID → MLBAM ID
    fg_to_mlbam = _build_fg_to_mlbam(fg_ids)
    if not fg_to_mlbam:
        return {}

    # MLBAM ID → position string
    mlbam_to_pos = _fetch_mlb_positions(list(fg_to_mlbam.values()))
    if not mlbam_to_pos:
        return {}

    # Combine — map all OF sub-positions (LF/CF/RF) to fantasy-standard "OF"
    _of_map = {"LF": "OF", "CF": "OF", "RF": "OF"}
    result: dict[int, list[str]] = {}
    for fg_id, mlbam_id in fg_to_mlbam.items():
        raw = mlbam_to_pos.get(mlbam_id, "")
        if raw:
            canonical = _of_map.get(raw, raw)
            result[fg_id] = [canonical]
    return result

# Mapping from FanGraphs column names to our normalized stat buckets
BATTING_COUNTING = ["PA", "AB", "R", "H", "HR", "RBI", "SB", "BB", "SO", "1B", "2B", "3B"]
BATTING_RATE = ["AVG", "OBP", "SLG", "OPS", "BABIP", "BB%", "K%", "ISO"]
BATTING_ADVANCED = [
    "wOBA", "wRC+", "xwOBA", "xBA", "xSLG",
    "Barrel%", "HardHit%", "EV", "LA",
    "Pull%", "Cent%", "Oppo%",
    "GB%", "FB%", "LD%",
    "SwStr%", "O-Swing%", "Z-Contact%",
    "CSW%", "Spd", "WAR",
]

PITCHING_COUNTING = ["IP", "W", "L", "SV", "HLD", "SO", "BB", "ER", "H", "HR", "G", "GS"]
PITCHING_RATE = ["ERA", "WHIP", "K/9", "BB/9", "K/BB", "HR/9", "BABIP", "LOB%", "K%", "BB%"]
# Baseball Reference column names for rolling-window stats.
# BRef uses different names than FanGraphs — normalised on ingest.
BREF_BATTING_COUNTING = ["G", "PA", "AB", "R", "H", "HR", "RBI", "SB", "CS", "BB", "SO", "2B", "3B"]
BREF_BATTING_RATE = ["BA", "OBP", "SLG", "OPS"]  # BA → stored as AVG

BREF_PITCHING_COUNTING = ["W", "L", "G", "GS", "SV", "IP", "H", "R", "ER", "HR", "BB", "SO"]
# batting_stats_range / pitching_stats_range column names differ from the _bref variants:
#   SO9  (not SO/9)    BAbip (not BABIP)
BREF_PITCHING_RATE = ["ERA", "WHIP", "SO9", "BAbip"]  # SO9 → stored as K9

# Minimum qualification thresholds per window length.
WINDOW_MIN_PA = {7: 10, 14: 20, 30: 40, 60: 80}
WINDOW_MIN_IP = {7: 3.0, 14: 5.0, 30: 10.0, 60: 18.0}

PITCHING_ADVANCED = [
    "FIP", "xFIP", "SIERA", "xERA",
    "Barrel%", "HardHit%", "EV", "LA",
    "GB%", "FB%", "LD%",
    "SwStr%", "O-Swing%", "Z-Contact%",
    "CSW%", "K-BB%", "Stuff+", "WAR",
]


def _safe_float(val: Any) -> float | None:
    """Convert a value to float, returning None for non-numeric/missing values.

    Returns None instead of 0.0 so downstream consumers can distinguish
    between "stat is zero" and "stat is missing/invalid".
    """
    if val is None:
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _extract_stats(row: pd.Series, columns: list[str]) -> dict[str, float]:
    """Extract named stats from a pandas row, skipping missing columns.

    Only includes stats that have valid numeric values — missing/NaN stats
    are omitted entirely rather than stored as 0.0.
    """
    result: dict[str, float] = {}
    for col in columns:
        if col not in row.index:
            continue
        val = _safe_float(row[col])
        if val is not None:
            result[col] = val
    return result


def _parse_positions(pos_str: Any) -> list[str]:
    """Parse position string from FanGraphs (e.g., '3B/SS') into a list."""
    if not pos_str or pd.isna(pos_str):
        return []
    return [p.strip() for p in str(pos_str).split("/") if p.strip()]


def _infer_pitcher_position(row: pd.Series) -> list[str]:
    """Infer SP or RP from games started vs total games.

    FanGraphs pitching data doesn't include a Pos column, so we use
    the GS/G ratio: if a pitcher started at least 40% of their games,
    they're classified as SP. Otherwise RP.
    """
    gs = _safe_float(row.get("GS", 0)) or 0.0
    g = _safe_float(row.get("G", 0)) or 0.0
    if g == 0:
        return []
    # 40% threshold accounts for spot starters and openers
    if gs / g >= 0.4:
        return ["SP"]
    return ["RP"]


def _stamp_birth_years(
    players: list,
    df: "pd.DataFrame",
    season: int,
) -> None:
    """Set birth_year on NormalizedPlayerData using the pybaseball Age column.

    Modifies players in-place.  birth_year = season − age so the value is
    season-agnostic and stable across re-ingest.  Players whose age is
    missing or non-numeric are left with birth_year=None.
    """
    # Build {IDfg: age} lookup from the DataFrame
    age_map: dict[int, int] = {}
    for _, row in df.iterrows():
        player_id = int(row.get("IDfg", 0))
        if player_id == 0:
            continue
        age_raw = row.get("Age")
        if age_raw is None:
            continue
        try:
            age = int(float(age_raw))
            if 15 <= age <= 50:  # sanity range
                age_map[player_id] = age
        except (TypeError, ValueError):
            pass

    for p in players:
        age = age_map.get(p.player_id)
        if age is not None:
            p.birth_year = season - age


class MLBAdapter(SportAdapter):
    """MLB sport adapter using pybaseball for data."""

    def get_positions(self) -> list[str]:
        return ["C", "1B", "2B", "3B", "SS", "OF", "SP", "RP", "DH"]

    def get_available_stats(self) -> list[str]:
        return [
            "R", "HR", "RBI", "SB", "AVG", "OPS",  # hitting categories
            "IP", "W", "SV", "K", "ERA", "WHIP",    # pitching categories
        ]

    def get_predictive_stats(self) -> list[str]:
        return [
            # Hitters
            "xwOBA", "xBA", "xSLG", "Barrel%", "HardHit%",
            "Spd", "Pull%", "GB%", "FB%", "LD%", "SwStr%",
            # Pitchers
            "xERA", "xFIP", "SIERA", "Stuff+", "CSW%", "K-BB%",
        ]

    def fetch_player_data(
        self, season: int, week: int | None = None, min_pa: int = 150, min_ip: int = 20
    ) -> list[NormalizedPlayerData]:
        """Fetch batting and pitching stats from FanGraphs via pybaseball.

        Args:
            min_pa: Minimum plate appearances for batters. Default 150 ensures
                    Statcast metrics (xwOBA, Barrel%) are sample-stable.
            min_ip: Minimum innings pitched for pitchers.
        """
        players: list[NormalizedPlayerData] = []

        logger.info(f"Fetching {season} batting stats (min {min_pa} PA)...")
        try:
            batting_df = batting_stats(season, qual=min_pa)
            # Build real positions from MLB API (FanGraphs Pos col is a float adjustment)
            fg_ids = [int(v) for v in batting_df["IDfg"].dropna() if int(v) != 0]
            pos_map = _get_batter_positions(fg_ids)
            logger.info("MLB API position lookup: resolved %d / %d batters", len(pos_map), len(fg_ids))
            batter_players = self.normalize_stats(batting_df, stat_type="batting", positions_map=pos_map)
            _stamp_birth_years(batter_players, batting_df, season)
            players.extend(batter_players)
            logger.info(f"  Got {len(batting_df)} batters")
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"Network error fetching batting stats: {e}", exc_info=True)
            raise
        except (ValueError, KeyError) as e:
            logger.error(f"Data parsing error in batting stats: {e}", exc_info=True)
            raise

        logger.info(f"Fetching {season} pitching stats (min {min_ip} IP)...")
        try:
            pitching_df = pitching_stats(season, qual=min_ip)
            pitcher_players = self.normalize_stats(pitching_df, stat_type="pitching")
            _stamp_birth_years(pitcher_players, pitching_df, season)
            players.extend(pitcher_players)
            logger.info(f"  Got {len(pitching_df)} pitchers")
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"Network error fetching pitching stats: {e}", exc_info=True)
            raise
        except (ValueError, KeyError) as e:
            logger.error(f"Data parsing error in pitching stats: {e}", exc_info=True)
            raise

        return players

    def normalize_stats(
        self,
        raw_data: Any,
        stat_type: str = "batting",
        positions_map: dict[int, list[str]] | None = None,
    ) -> list[NormalizedPlayerData]:
        """Convert FanGraphs DataFrame to NormalizedPlayerData list.

        Args:
            raw_data: FanGraphs DataFrame from pybaseball.
            stat_type: "batting" or "pitching".
            positions_map: Optional {fangraphs_id: [position_str, ...]} pre-fetched
                from the MLB API.  When provided, overrides the FanGraphs Pos column
                (which holds a WAR positional-adjustment float, not a position code).
                Pass ``None`` to skip position resolution (useful in tests that supply
                their own synthetic position data via the Pos column).
        """
        if not isinstance(raw_data, pd.DataFrame):
            raise TypeError(f"Expected DataFrame, got {type(raw_data)}")

        if stat_type == "batting":
            counting_cols = BATTING_COUNTING
            rate_cols = BATTING_RATE
            advanced_cols = BATTING_ADVANCED
        else:
            counting_cols = PITCHING_COUNTING
            rate_cols = PITCHING_RATE
            advanced_cols = PITCHING_ADVANCED

        # Resolve batter positions from MLB API when caller provides a map.
        # fetch_player_data() builds this map; callers that skip it (tests, etc.)
        # get positions from the Pos column via _parse_positions instead.
        batter_positions: dict[int, list[str]] = positions_map or {}

        players = []
        for _, row in raw_data.iterrows():
            player_id = int(row.get("IDfg", 0))
            if player_id == 0:
                continue

            if stat_type == "pitching":
                positions = _infer_pitcher_position(row)
            elif positions_map is not None:
                # MLB API map provided — use it; unknown players get empty list
                positions = batter_positions.get(player_id) or []
            else:
                # No map supplied (e.g. tests) — fall back to Pos column
                positions = _parse_positions(row.get("Pos", ""))

            players.append(
                NormalizedPlayerData(
                    player_id=player_id,
                    name=str(row.get("Name", "")),
                    team=str(row.get("Team", "")),
                    positions=positions,
                    stat_type=stat_type,
                    counting_stats=_extract_stats(row, counting_cols),
                    rate_stats=_extract_stats(row, rate_cols),
                    advanced_stats=_extract_stats(row, advanced_cols),
                )
            )

        return players

    def fetch_rolling_batting_stats(
        self,
        start_dt: str,
        end_dt: str,
        window_days: int,
    ) -> list[dict]:
        """Fetch batting stats for a specific date range from Baseball Reference.

        Uses pybaseball's batting_stats_range (BRef date-range aggregates).
        Returns a list of dicts with normalised keys ready for PlayerRollingStats.
        Applies minimum PA threshold for the window length to filter noise.

        Args:
            start_dt: ISO date string "YYYY-MM-DD"
            end_dt:   ISO date string "YYYY-MM-DD"
            window_days: Window length (7/14/30/60) — used for min-PA lookup.
        """
        min_pa = WINDOW_MIN_PA.get(window_days, 10)
        logger.info("Fetching rolling batting stats %s–%s (min %d PA)", start_dt, end_dt, min_pa)

        df = batting_stats_range(start_dt=start_dt, end_dt=end_dt)
        if df is None or df.empty:
            return []

        # Only MLB players (Lev column contains e.g. "Maj-AL", "Maj-NL")
        if "Lev" in df.columns:
            df = df[df["Lev"].str.startswith("Maj", na=False)]

        # Filter by minimum PA
        if "PA" in df.columns:
            pa_col = df["PA"].apply(_safe_float).fillna(0)
            df = df[pa_col >= min_pa]

        records = []
        for _, row in df.iterrows():
            counting = _extract_stats(row, BREF_BATTING_COUNTING)
            rate_raw = _extract_stats(row, BREF_BATTING_RATE)

            # Normalise BRef column names to our schema
            rate = {}
            for k, v in rate_raw.items():
                key = "AVG" if k == "BA" else k
                rate[key] = v

            # Rename SO → K for consistency with season stats
            if "SO" in counting:
                counting["K"] = counting.pop("SO")

            records.append({
                "name": str(row.get("Name", "")),
                "team": str(row.get("Tm", "")),
                "stat_type": "batting",
                "counting_stats": counting,
                "rate_stats": rate,
            })

        return records

    def fetch_rolling_pitching_stats(
        self,
        start_dt: str,
        end_dt: str,
        window_days: int,
    ) -> list[dict]:
        """Fetch pitching stats for a specific date range from Baseball Reference.

        Uses pybaseball's pitching_stats_range (BRef date-range aggregates).
        Returns a list of dicts with normalised keys ready for PlayerRollingStats.
        Applies minimum IP threshold for the window length.
        """
        min_ip = WINDOW_MIN_IP.get(window_days, 3.0)
        logger.info("Fetching rolling pitching stats %s–%s (min %.1f IP)", start_dt, end_dt, min_ip)

        df = pitching_stats_range(start_dt=start_dt, end_dt=end_dt)
        if df is None or df.empty:
            return []

        # Only MLB players
        if "Lev" in df.columns:
            df = df[df["Lev"].str.startswith("Maj", na=False)]

        # Filter by minimum IP
        if "IP" in df.columns:
            ip_col = df["IP"].apply(_safe_float).fillna(0.0)
            df = df[ip_col >= min_ip]

        records = []
        for _, row in df.iterrows():
            counting = _extract_stats(row, BREF_PITCHING_COUNTING)
            rate_raw = _extract_stats(row, BREF_PITCHING_RATE)

            # Normalise BRef column names to our schema
            rate = {}
            for k, v in rate_raw.items():
                key = "K9" if k == "SO9" else ("BABIP" if k == "BAbip" else k)
                rate[key] = v

            # Rename SO → K for consistency with season stats
            if "SO" in counting:
                counting["K"] = counting.pop("SO")

            records.append({
                "name": str(row.get("Name", "")),
                "team": str(row.get("Tm", "")),
                "stat_type": "pitching",
                "counting_stats": counting,
                "rate_stats": rate,
            })

        return records

    def get_schedule(self, season: int, week: int) -> list[dict]:
        raise NotImplementedError("Schedule data not yet available via pybaseball")
