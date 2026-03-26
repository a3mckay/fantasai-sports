"""Weekly schedule context for This Week rankings.

Fetches probable starting pitchers and team game counts from the MLB Stats API,
then maps MLBAM player IDs back to our player_ids so the scoring engine can
use per-player HorizonConfig overrides for This Week projections.

Also enriches PlayerSchedule with:
  - weather_hr_factor: wind/temp modifier for outdoor games (Open-Meteo, free)
  - vegas_run_factor:  implied-runs modifier from Vegas team totals (The Odds API)
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from fantasai.engine.projection import HorizonConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Park factors — team abbreviation → HR park factor (1.0 = neutral)
# ---------------------------------------------------------------------------

PARK_FACTORS: dict[str, float] = {
    "COL": 1.22,
    "CIN": 1.14,
    "PHI": 1.08,
    "BOS": 1.06,
    "NYY": 1.05,
    "TOR": 1.04,
    "BAL": 1.03,
    "CHC": 1.02,
    "TEX": 1.02,
    "ARI": 1.01,
    "ATL": 0.99,
    "HOU": 0.98,
    "WSH": 0.98,
    "SEA": 0.97,
    "STL": 0.96,
    "LAD": 0.95,
    "CLE": 0.95,
    "DET": 0.94,
    "MIN": 0.94,
    "KCR": 0.93,
    "MIL": 0.93,
    "LAA": 0.92,
    "PIT": 0.91,
    "NYM": 0.91,
    "CHW": 0.91,
    "SFG": 0.88,
    "SDP": 0.87,
    "MIA": 0.86,
    "TBR": 0.85,
    # Athletics (temporary name variants)
    "OAK": 0.92,
    "ATH": 0.92,
}


# ---------------------------------------------------------------------------
# Venue coordinates for outdoor ballparks (venueId → (name, lat, lng))
# ---------------------------------------------------------------------------

_VENUE_COORDS: dict[int, tuple[str, float, float]] = {
    3313: ("Dodger Stadium", 34.0739, -118.2400),
    2395: ("Busch Stadium", 38.6226, -90.1928),
    2394: ("PNC Park", 40.4469, -80.0057),
    4705: ("Oracle Park", 37.7786, -122.3893),
    2680: ("Citi Field", 40.7571, -73.8458),
    3289: ("Yankee Stadium", 40.8296, -73.9262),
    2602: ("Fenway Park", 42.3467, -71.0972),
    2381: ("Camden Yards", 39.2838, -76.6218),
    2376: ("Kauffman Stadium", 39.0517, -94.4803),
    2500: ("Angel Stadium", 33.8003, -117.8827),
    680:  ("Wrigley Field", 41.9484, -87.6553),
    2889: ("Great American Ball Park", 39.0979, -84.5082),
    5107: ("Target Field", 44.9817, -93.2784),
    2593: ("Progressive Field", 41.4962, -81.6852),
    2386: ("Nationals Park", 38.8730, -77.0074),
    2406: ("Petco Park", 32.7076, -117.1570),
    4321: ("Truist Park", 33.8908, -84.4678),
    4140: ("T-Mobile Park", 47.5914, -122.3325),
    # retractable/indoor venues included for reference but excluded by _INDOOR_VENUE_IDS
    4169: ("LoanDepot Park", 25.7781, -80.2197),
    2392: ("Minute Maid Park", 29.7573, -95.3555),
    5325: ("Globe Life Field", 32.7474, -97.0830),
    14:   ("Rogers Centre", 43.6414, -79.3894),
    32:   ("American Family Field", 43.0280, -87.9712),
}

# venueIds for indoor / retractable-roof stadiums — skip weather for these
_INDOOR_VENUE_IDS: frozenset[int] = frozenset({12, 5325, 14, 2392, 32, 15, 4169})

# ---------------------------------------------------------------------------
# Module-level caches
# ---------------------------------------------------------------------------

# {week_start_iso: (monotonic_ts, {game_pk_str: weather_hr_factor})}
_WEATHER_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_WEATHER_TTL = 3600  # 1 hour

# {week_start_iso: (monotonic_ts, {team_abbr: vegas_run_factor})}
_VEGAS_CACHE: dict[str, tuple[float, dict[str, float]]] = {}
_VEGAS_TTL = 4 * 3600  # 4 hours

# ---------------------------------------------------------------------------
# Full name → abbreviation for all 30 MLB teams (The Odds API returns full names)
# ---------------------------------------------------------------------------

_TEAM_NAME_TO_ABBR: dict[str, str] = {
    "Arizona Diamondbacks": "ARI",
    "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC",
    "Chicago White Sox": "CHW",
    "Cincinnati Reds": "CIN",
    "Cleveland Guardians": "CLE",
    "Colorado Rockies": "COL",
    "Detroit Tigers": "DET",
    "Houston Astros": "HOU",
    "Kansas City Royals": "KCR",
    "Los Angeles Angels": "LAA",
    "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN",
    "New York Mets": "NYM",
    "New York Yankees": "NYY",
    "Oakland Athletics": "OAK",
    "Athletics": "ATH",
    "Philadelphia Phillies": "PHI",
    "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SDP",
    "San Francisco Giants": "SFG",
    "Seattle Mariners": "SEA",
    "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TBR",
    "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PlayerSchedule:
    """Weekly schedule context for a single player."""

    probable_starts: int      # 0, 1, or 2 — for SPs
    team_games: int           # typically 5-7
    home_park: Optional[str]  # team abbreviation for home games this week
    weather_hr_factor: float = 1.0  # 0.85–1.15 wind/temp modifier for outdoor games
    vegas_run_factor: float = 1.0   # 0.90–1.10 implied-runs modifier from Vegas totals


# ---------------------------------------------------------------------------
# Weather enrichment
# ---------------------------------------------------------------------------

def _is_indoor_venue(venue: dict) -> bool:
    """Return True if the venue is indoor or has a retractable roof."""
    venue_id: Optional[int] = venue.get("id")
    if venue_id is not None and venue_id in _INDOOR_VENUE_IDS:
        return True
    field_type: str = (venue.get("fieldType") or "").lower()
    for keyword in ("indoor", "dome", "turf"):
        if keyword in field_type:
            return True
    return False


def fetch_game_weather(
    games: list[dict],
    week_start: date,
) -> dict[str, float]:
    """Return {game_pk_str: weather_hr_factor} for outdoor games this week.

    Calls Open-Meteo (free, no API key) once per unique outdoor venue.
    Results are cached per week_start with a 1-hour TTL.

    On any HTTP error the affected game(s) return factor 1.0 (neutral).
    """
    cache_key = week_start.isoformat()
    cached = _WEATHER_CACHE.get(cache_key)
    if cached is not None:
        ts, result = cached
        if time.monotonic() - ts <= _WEATHER_TTL:
            return result

    # Group outdoor games by venueId to minimise API calls
    # venue_id → list of (game_pk_str, game_datetime_iso)
    venue_games: dict[int, list[tuple[str, str]]] = {}

    for game in games:
        game_pk = game.get("gamePk")
        if game_pk is None:
            continue
        game_pk_str = str(game_pk)

        venue = game.get("venue") or {}
        if _is_indoor_venue(venue):
            continue

        venue_id: Optional[int] = venue.get("id")
        if venue_id is None or venue_id not in _VENUE_COORDS:
            continue

        game_dt: str = game.get("gameDate") or ""  # ISO 8601 string from MLB API
        venue_games.setdefault(venue_id, []).append((game_pk_str, game_dt))

    result: dict[str, float] = {}

    for venue_id, game_entries in venue_games.items():
        _name, lat, lng = _VENUE_COORDS[venue_id]
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lng}"
            f"&hourly=temperature_2m,wind_speed_10m"
            f"&wind_speed_unit=mph"
            f"&temperature_unit=fahrenheit"
            f"&timezone=auto"
            f"&forecast_days=7"
        )
        try:
            resp = httpx.get(url, timeout=10.0)
            resp.raise_for_status()
            wx_data = resp.json()
        except Exception as exc:
            logger.warning("Open-Meteo request failed for venue %s: %s", venue_id, exc)
            for game_pk_str, _ in game_entries:
                result[game_pk_str] = 1.0
            continue

        hourly = wx_data.get("hourly") or {}
        times: list[str] = hourly.get("time") or []
        temps: list[Optional[float]] = hourly.get("temperature_2m") or []
        winds: list[Optional[float]] = hourly.get("wind_speed_10m") or []

        for game_pk_str, game_dt in game_entries:
            factor = _compute_weather_hr_factor(times, temps, winds, game_dt)
            result[game_pk_str] = factor

    _WEATHER_CACHE[cache_key] = (time.monotonic(), result)
    return result


def _compute_weather_hr_factor(
    times: list[str],
    temps: list[Optional[float]],
    winds: list[Optional[float]],
    game_dt: str,
) -> float:
    """Find the forecast hour nearest to game_dt and compute the HR factor."""
    if not times or not game_dt:
        return 1.0

    # Normalise the game datetime string to a bare "YYYY-MM-DDTHH:MM" prefix
    # so it can be compared to Open-Meteo hourly time strings.
    game_prefix = game_dt[:16]  # "2026-04-08T19:05" → "2026-04-08T19"

    best_idx: Optional[int] = None
    best_diff = float("inf")
    for i, t in enumerate(times):
        diff = abs(len(t) - len(game_prefix))  # fallback length diff
        # Compare prefix: find closest hour
        if len(t) >= 13 and len(game_prefix) >= 13:
            try:
                from datetime import datetime
                t_dt = datetime.fromisoformat(t[:16])
                g_dt = datetime.fromisoformat(game_prefix[:16])
                diff = abs((t_dt - g_dt).total_seconds())
            except ValueError:
                pass
        if diff < best_diff:
            best_diff = diff
            best_idx = i

    if best_idx is None:
        return 1.0

    temp_f: float = float(temps[best_idx]) if best_idx < len(temps) and temps[best_idx] is not None else 72.0
    wind_mph: float = float(winds[best_idx]) if best_idx < len(winds) and winds[best_idx] is not None else 10.0

    # Wind component: only boost (high wind = more HRs on average across directions)
    wind_factor = 1.0 + max(0.0, wind_mph - 10.0) * 0.004
    wind_factor = max(0.88, min(1.12, wind_factor))

    # Temp component: 72°F = neutral; cold = fewer HRs
    temp_factor = 1.0 + (temp_f - 72.0) * 0.002
    temp_factor = max(0.92, min(1.08, temp_factor))

    combined = wind_factor * temp_factor
    return max(0.85, min(1.15, combined))


# ---------------------------------------------------------------------------
# Vegas odds enrichment
# ---------------------------------------------------------------------------

def fetch_vegas_run_factors(
    week_start: date,
    week_end: date,
    api_key: Optional[str],
) -> dict[str, float]:
    """Return {team_abbreviation: vegas_run_factor} for all MLB teams this week.

    Calls The Odds API (requires api_key). Returns {} immediately if api_key
    is absent. Results are cached per week_start with a 4-hour TTL.

    Factor is (team_avg_implied_runs / 4.4) clamped to [0.90, 1.10].
    League average team total ≈ 4.4 runs/game.
    """
    if not api_key:
        return {}

    cache_key = week_start.isoformat()
    cached = _VEGAS_CACHE.get(cache_key)
    if cached is not None:
        ts, result = cached
        if time.monotonic() - ts <= _VEGAS_TTL:
            return result

    url = (
        "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds/"
        f"?apiKey={api_key}"
        "&regions=us"
        "&markets=totals"
        "&oddsFormat=american"
    )
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        games_data: list[dict] = resp.json()
    except Exception as exc:
        logger.warning("The Odds API request failed: %s", exc)
        return {}

    # Accumulate implied total runs and game count per team abbreviation
    team_total_implied: dict[str, float] = {}
    team_game_count: dict[str, int] = {}

    for game in games_data:
        home_team: str = game.get("home_team") or ""
        away_team: str = game.get("away_team") or ""
        home_abbr = _TEAM_NAME_TO_ABBR.get(home_team)
        away_abbr = _TEAM_NAME_TO_ABBR.get(away_team)

        # Find the "totals" bookmaker entry and extract the Over point
        total_runs: Optional[float] = None
        for bookmaker in (game.get("bookmakers") or []):
            for market in (bookmaker.get("markets") or []):
                if market.get("key") != "totals":
                    continue
                for outcome in (market.get("outcomes") or []):
                    if (outcome.get("name") or "").lower() == "over":
                        try:
                            total_runs = float(outcome["point"])
                        except (KeyError, TypeError, ValueError):
                            pass
                        break
                if total_runs is not None:
                    break
            if total_runs is not None:
                break

        if total_runs is None:
            continue

        # Each team gets half the implied total
        team_implied = total_runs / 2.0
        for abbr in (home_abbr, away_abbr):
            if abbr:
                team_total_implied[abbr] = team_total_implied.get(abbr, 0.0) + team_implied
                team_game_count[abbr] = team_game_count.get(abbr, 0) + 1

    _LEAGUE_AVG_PER_TEAM = 4.4
    result: dict[str, float] = {}
    for abbr, total in team_total_implied.items():
        count = team_game_count.get(abbr, 1)
        avg_implied = total / count
        factor = avg_implied / _LEAGUE_AVG_PER_TEAM
        result[abbr] = max(0.90, min(1.10, factor))

    _VEGAS_CACHE[cache_key] = (time.monotonic(), result)
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_current_week_bounds() -> tuple[date, date]:
    """Return (week_start_monday, week_end_sunday) for the current week."""
    today = date.today()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)             # Sunday
    return week_start, week_end


def fetch_weekly_schedule(
    week_start: date,
    week_end: date,
    db: "Session",
    vegas_api_key: Optional[str] = None,
) -> dict[int, "PlayerSchedule"]:
    """Fetch MLB schedule for the given week and return per-player context.

    Returns {player_id: PlayerSchedule}.

    Calls the MLB Stats API and parses probable pitchers + game counts, then
    maps MLBAM IDs back to our internal player_ids via the Player table.

    Also enriches each PlayerSchedule with weather_hr_factor and
    vegas_run_factor when data is available (failures are non-fatal).

    On any HTTP error, logs a warning and returns {} so rankings continue
    without schedule adjustments.
    """
    from fantasai.models.player import Player

    # ── Fetch MLB Stats API ─────────────────────────────────────────────────
    url = (
        f"https://statsapi.mlb.com/api/v1/schedule"
        f"?sportId=1"
        f"&startDate={week_start.isoformat()}"
        f"&endDate={week_end.isoformat()}"
        f"&hydrate=probablePitcher,venue"
    )
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch MLB schedule from %s: %s", url, exc)
        return {}

    # ── Parse games ─────────────────────────────────────────────────────────
    # Counts keyed by MLBAM ID
    pitcher_starts: dict[int, int] = {}       # mlbam_pitcher_id → start count
    team_game_counts: dict[int, int] = {}     # mlbam_team_id    → game count
    team_mlbam_to_abbr: dict[int, str] = {}   # mlbam_team_id    → abbreviation

    # Collect all raw game dicts for weather enrichment
    all_games: list[dict] = []

    # team_abbr → list of game_pk_str for weather factor averaging
    team_abbr_to_game_pks: dict[str, list[str]] = {}

    dates = data.get("dates") or []
    for day in dates:
        games = day.get("games") or []
        for game in games:
            all_games.append(game)
            teams = game.get("teams") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}

            # Team abbreviations
            home_team = home.get("team") or {}
            away_team = away.get("team") or {}
            home_team_id: Optional[int] = home_team.get("id")
            away_team_id: Optional[int] = away_team.get("id")
            home_abbr: Optional[str] = home_team.get("abbreviation")
            away_abbr: Optional[str] = away_team.get("abbreviation")

            if home_team_id is not None:
                team_game_counts[home_team_id] = team_game_counts.get(home_team_id, 0) + 1
                if home_abbr:
                    team_mlbam_to_abbr[home_team_id] = home_abbr
            if away_team_id is not None:
                team_game_counts[away_team_id] = team_game_counts.get(away_team_id, 0) + 1
                if away_abbr:
                    team_mlbam_to_abbr[away_team_id] = away_abbr

            # Map game_pk to team abbreviations for weather factor lookups
            game_pk_str = str(game.get("gamePk") or "")
            if game_pk_str:
                for abbr in (home_abbr, away_abbr):
                    if abbr:
                        team_abbr_to_game_pks.setdefault(abbr.upper(), []).append(game_pk_str)

            # Probable pitchers
            home_pp = home.get("probablePitcher") or {}
            away_pp = away.get("probablePitcher") or {}
            home_pitcher_id: Optional[int] = home_pp.get("id")
            away_pitcher_id: Optional[int] = away_pp.get("id")

            if home_pitcher_id is not None:
                prev = pitcher_starts.get(home_pitcher_id, 0)
                pitcher_starts[home_pitcher_id] = min(prev + 1, 2)
            if away_pitcher_id is not None:
                prev = pitcher_starts.get(away_pitcher_id, 0)
                pitcher_starts[away_pitcher_id] = min(prev + 1, 2)

    if not team_game_counts and not pitcher_starts:
        logger.warning("MLB schedule response contained no usable game data")
        return {}

    # ── Weather enrichment ───────────────────────────────────────────────────
    weather_by_game: dict[str, float] = {}
    try:
        weather_by_game = fetch_game_weather(all_games, week_start)
    except Exception as exc:
        logger.warning("Weather enrichment failed (non-fatal): %s", exc)

    # Pre-compute per-team average weather_hr_factor from all outdoor games
    team_weather_factor: dict[str, float] = {}
    for abbr, pks in team_abbr_to_game_pks.items():
        factors = [weather_by_game[pk] for pk in pks if pk in weather_by_game]
        if factors:
            team_weather_factor[abbr] = sum(factors) / len(factors)

    # ── Vegas enrichment ─────────────────────────────────────────────────────
    vegas_factors: dict[str, float] = {}
    try:
        vegas_factors = fetch_vegas_run_factors(week_start, week_end, vegas_api_key)
    except Exception as exc:
        logger.warning("Vegas enrichment failed (non-fatal): %s", exc)

    # ── Load players from DB ─────────────────────────────────────────────────
    # Build mlbam_id → player_id for individual lookup (pitchers)
    players_with_mlbam = (
        db.query(Player)
        .filter(Player.mlbam_id.isnot(None))
        .all()
    )
    mlbam_to_player_id: dict[int, int] = {
        p.mlbam_id: p.player_id
        for p in players_with_mlbam
        if p.mlbam_id is not None
    }

    # Build team_abbr → [player_id] for batters who need team game count
    # (batters don't have individual MLBAM probable-start entries)
    all_players = db.query(Player).all()
    abbr_to_player_ids: dict[str, list[int]] = {}
    for p in all_players:
        team_abbr = (p.team or "").strip().upper()
        if team_abbr:
            abbr_to_player_ids.setdefault(team_abbr, []).append(p.player_id)

    # ── Build result dict ────────────────────────────────────────────────────
    result: dict[int, PlayerSchedule] = {}

    # Add pitcher schedule entries
    for mlbam_pitcher_id, starts in pitcher_starts.items():
        player_id = mlbam_to_player_id.get(mlbam_pitcher_id)
        if player_id is None:
            continue
        # Find this pitcher's team to get game count and park
        # We don't know their team MLBAM ID from just the pitcher ID, so
        # we look at existing result or fall back to 6 (league average)
        result[player_id] = PlayerSchedule(
            probable_starts=starts,
            team_games=6,       # will be updated below if we can match the team
            home_park=None,
        )

    # Add all-player schedule entries using team game counts
    for mlbam_team_id, game_count in team_game_counts.items():
        abbr = team_mlbam_to_abbr.get(mlbam_team_id)
        if abbr is None:
            continue
        abbr_upper = abbr.strip().upper()
        weather_factor = team_weather_factor.get(abbr_upper, 1.0)
        vegas_factor = vegas_factors.get(abbr_upper, 1.0)
        player_ids_for_team = abbr_to_player_ids.get(abbr_upper, [])
        for player_id in player_ids_for_team:
            existing = result.get(player_id)
            if existing is not None:
                # Update the team_games on an already-created entry (pitcher)
                result[player_id] = PlayerSchedule(
                    probable_starts=existing.probable_starts,
                    team_games=game_count,
                    home_park=abbr_upper,
                    weather_hr_factor=weather_factor,
                    vegas_run_factor=vegas_factor,
                )
            else:
                result[player_id] = PlayerSchedule(
                    probable_starts=0,
                    team_games=game_count,
                    home_park=abbr_upper,
                    weather_hr_factor=weather_factor,
                    vegas_run_factor=vegas_factor,
                )

    return result


def build_week_configs(
    schedule: dict[int, "PlayerSchedule"],
    base_config: "HorizonConfig",
) -> dict[int, "HorizonConfig"]:
    """Build per-player HorizonConfig overrides from schedule data.

    For each player_id in schedule, create a modified HorizonConfig:
      - SPs with probable_starts > 0: sp_ip scaled by probable_starts
      - SPs with probable_starts == 0: sp_ip = 0.0
      - All players: hitter_pa and rp_ip scaled by (team_games / 6.0)

    Returns only entries where the config differs from base_config.
    """
    from dataclasses import replace

    overrides: dict[int, HorizonConfig] = {}

    for player_id, ps in schedule.items():
        games_ratio = ps.team_games / 6.0

        new_hitter_pa = int(base_config.hitter_pa * games_ratio)
        new_rp_ip = round(base_config.rp_ip * games_ratio, 1)

        # SP IP scaling: use probable_starts as multiplier
        if ps.probable_starts > 0:
            new_sp_ip = base_config.sp_ip * ps.probable_starts
        elif ps.probable_starts == 0:
            new_sp_ip = 0.0
        else:
            new_sp_ip = base_config.sp_ip

        # Only include if at least one field differs from base
        if (
            new_hitter_pa != base_config.hitter_pa
            or new_sp_ip != base_config.sp_ip
            or new_rp_ip != base_config.rp_ip
        ):
            overrides[player_id] = replace(
                base_config,
                hitter_pa=new_hitter_pa,
                sp_ip=new_sp_ip,
                rp_ip=new_rp_ip,
            )

    return overrides


# ---------------------------------------------------------------------------
# Blurb context helpers — notable schedule facts for AI prompt enrichment
# ---------------------------------------------------------------------------

# Venue display names for park factor notes
_PARK_NAMES: dict[str, str] = {
    "COL": "Coors Field",
    "CIN": "Great American Ball Park",
    "PHI": "Citizens Bank Park",
    "BOS": "Fenway Park",
    "NYY": "Yankee Stadium",
    "SFG": "Oracle Park",
    "SDP": "Petco Park",
    "MIA": "loanDepot Park",
    "TBR": "Tropicana Field",
    "PIT": "PNC Park",
    "LAD": "Dodger Stadium",
    "SEA": "T-Mobile Park",
    "MIN": "Target Field",
    "DET": "Comerica Park",
}

# Thresholds for "notable" signals — below these we omit from blurbs
_PARK_NOTABLE_THRESHOLD   = 0.07   # |pf - 1.0| ≥ 7%  → mention park
_VEGAS_NOTABLE_THRESHOLD  = 0.05   # |vf - 1.0| ≥ 5%  → mention run env
_WEATHER_NOTABLE_THRESHOLD = 0.06  # |wf - 1.0| ≥ 6%  → mention weather


def build_player_week_context(
    player_id: int,
    player_schedule: "PlayerSchedule",
    stat_type: str,   # "batting" or "pitching"
    positions: list[str],
) -> str | None:
    """Return a short context string with notable schedule facts for This Week blurbs.

    Returns None when nothing is notable enough to mention.
    Examples:
      "2 starts this week; pitching at Coors (+22% HR)"
      "playing at Petco Park (suppressed HR environment); Vegas implies 3.8 runs/game"
      "7-game week; strong offensive implied total (5.3 R/G)"
    """
    ps = player_schedule
    notes: list[str] = []

    is_sp = "SP" in positions
    is_rp = "RP" in positions and not is_sp

    # ── Starts note (pitchers only) ──────────────────────────────────────────
    if stat_type == "pitching" and is_sp:
        if ps.probable_starts == 2:
            notes.append("2 starts this week")
        elif ps.probable_starts == 0:
            notes.append("no probable starts this week")
        # 1 start is default — only call it out if combined with other context

    # ── Games note (batters / RPs when non-standard) ─────────────────────────
    if stat_type == "batting" or is_rp:
        if ps.team_games >= 7:
            notes.append(f"{ps.team_games}-game week")
        elif ps.team_games <= 4:
            notes.append(f"light schedule ({ps.team_games} games)")

    # ── Park factor ──────────────────────────────────────────────────────────
    team = ps.home_park or ""
    pf = PARK_FACTORS.get(team, 1.0)
    if abs(pf - 1.0) >= _PARK_NOTABLE_THRESHOLD:
        park_name = _PARK_NAMES.get(team, f"{team} ballpark")
        pct = int(round((pf - 1.0) * 100))
        direction = "+" if pct > 0 else ""
        if stat_type == "batting":
            notes.append(f"games at {park_name} ({direction}{pct}% HR)")
        else:
            notes.append(f"pitching environment at {park_name} ({direction}{pct}% HR)")

    # ── Vegas run environment ────────────────────────────────────────────────
    vf = ps.vegas_run_factor
    if abs(vf - 1.0) >= _VEGAS_NOTABLE_THRESHOLD:
        implied_per_game = round(4.4 * vf, 1)
        if vf > 1.0:
            notes.append(f"Vegas implies {implied_per_game} R/G (above avg)")
        else:
            notes.append(f"Vegas implies {implied_per_game} R/G (below avg)")

    # ── Weather ──────────────────────────────────────────────────────────────
    wf = ps.weather_hr_factor
    if abs(wf - 1.0) >= _WEATHER_NOTABLE_THRESHOLD:
        if wf > 1.0:
            notes.append("favorable weather conditions (wind/heat)")
        else:
            notes.append("cold/adverse weather conditions")

    if not notes:
        return None

    return "; ".join(notes)
