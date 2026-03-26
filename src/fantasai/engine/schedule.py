"""Weekly schedule context for This Week rankings.

Fetches probable starting pitchers and team game counts from the MLB Stats API,
then maps MLBAM player IDs back to our player_ids so the scoring engine can
use per-player HorizonConfig overrides for This Week projections.
"""
from __future__ import annotations

import logging
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
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PlayerSchedule:
    """Weekly schedule context for a single player."""

    probable_starts: int      # 0, 1, or 2 — for SPs
    team_games: int           # typically 5-7
    home_park: Optional[str]  # team abbreviation for home games this week


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
) -> dict[int, "PlayerSchedule"]:
    """Fetch MLB schedule for the given week and return per-player context.

    Returns {player_id: PlayerSchedule}.

    Calls the MLB Stats API and parses probable pitchers + game counts, then
    maps MLBAM IDs back to our internal player_ids via the Player table.

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
        f"&hydrate=probablePitcher"
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

    dates = data.get("dates") or []
    for day in dates:
        games = day.get("games") or []
        for game in games:
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
        player_ids_for_team = abbr_to_player_ids.get(abbr_upper, [])
        for player_id in player_ids_for_team:
            existing = result.get(player_id)
            if existing is not None:
                # Update the team_games on an already-created entry (pitcher)
                result[player_id] = PlayerSchedule(
                    probable_starts=existing.probable_starts,
                    team_games=game_count,
                    home_park=abbr_upper,
                )
            else:
                result[player_id] = PlayerSchedule(
                    probable_starts=0,
                    team_games=game_count,
                    home_park=abbr_upper,
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
