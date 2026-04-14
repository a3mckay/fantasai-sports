"""Matchup Analyzer service — projects weekly H2H category totals and generates narratives.

Called by APScheduler (daily refresh) and the POST /matchups/analyze API endpoint.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Optional

import httpx

from fantasai.adapters.base import NormalizedPlayerData
from fantasai.engine.projection import (
    HORIZON_CONFIGS,
    ProjectionHorizon,
    project_hitter_stats,
    project_pitcher_stats,
)
from fantasai.engine.schedule import (
    PARK_FACTORS,
    build_week_configs,
    fetch_weekly_schedule,
    get_current_week_bounds,
)
from fantasai.models.matchup import MatchupAnalysis
from fantasai.models.player import Player, PlayerStats

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from fantasai.models.league import League

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-stat categories — these need weighted averaging, not plain summing
# ---------------------------------------------------------------------------

_RATE_CATS: frozenset[str] = frozenset({"AVG", "OBP", "SLG", "OPS", "ERA", "WHIP", "K/9"})
_PITCHER_RATE_CATS: frozenset[str] = frozenset({"ERA", "WHIP", "K/9"})
_HITTER_RATE_CATS: frozenset[str] = frozenset({"AVG", "OBP", "SLG", "OPS"})

# ERA/WHIP: lower is better
_LOWER_IS_BETTER: frozenset[str] = frozenset({"ERA", "WHIP"})

# Yahoo Fantasy stat ID → canonical fantasy category name.
# Only IDs that appear in standard fantasy baseball leagues are included.
# Unknown / league-specific IDs are silently dropped when building live_stats.
_YAHOO_STAT_ID_TO_CAT: dict[str, str] = {
    "2":  "AB",
    "3":  "AVG",
    "4":  "OBP",
    "5":  "SLG",
    "6":  "BB",
    "7":  "HR",
    "8":  "H",
    "9":  "1B",
    "10": "2B",
    "11": "3B",
    "12": "RBI",
    "13": "R",
    "16": "SB",
    "17": "CS",
    "23": "IP",
    "24": "W",
    "25": "L",
    "26": "GS",
    "27": "CG",
    "28": "SHO",
    "29": "SV",
    "30": "BS",
    "31": "K",
    "32": "ERA",
    "33": "WHIP",
    "37": "K/9",
    "39": "BB/9",
    "42": "HLD",
    "55": "H",   # alternate H stat_id used by some leagues
    "60": "OPS",
}


# ---------------------------------------------------------------------------
# Yahoo scoreboard fetch
# ---------------------------------------------------------------------------

def fetch_league_scoreboard(
    access_token: str,
    league_key: str,
    week: Optional[int] = None,
) -> list[dict]:
    """Fetch Yahoo Fantasy scoreboard for a league and parse matchups.

    Returns a list of matchup dicts, one per head-to-head pairing:
      {
        "team1_key": str,
        "team1_name": str,
        "manager1_name": str,
        "team2_key": str,
        "team2_name": str,
        "manager2_name": str,
        "week": int,
        "live_stats": dict,  # {category: {team1_key: val, team2_key: val}}
      }
    """
    url = f"https://fantasysports.yahooapis.com/fantasy/v2/league/{league_key}/scoreboard"
    params: dict[str, str] = {"format": "json"}
    if week is not None:
        params["week"] = str(week)

    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch Yahoo scoreboard for league %s: %s", league_key, exc)
        return []

    try:
        league_data = data["fantasy_content"]["league"]
        # league_data is a 2-element list: [league_metadata_dict, {scoreboard: ...}]
        scoreboard_container = league_data[-1]
        scoreboard = scoreboard_container["scoreboard"]
        # Yahoo nests the actual matchup list under scoreboard["0"]["matchups"]
        # (scoreboard["week"] holds the week number; scoreboard["0"] holds matchups)
        matchups_raw = scoreboard["0"]["matchups"]
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning(
            "Unexpected Yahoo scoreboard shape for %s: %s — top-level keys: %s",
            league_key, exc,
            list(data.get("fantasy_content", {}).keys()) if isinstance(data, dict) else type(data).__name__,
        )
        return []

    logger.info(
        "fetch_league_scoreboard: parsing matchups for league %s (matchups type=%s, count=%s)",
        league_key, type(matchups_raw).__name__,
        matchups_raw.get("count") if isinstance(matchups_raw, dict) else "?",
    )

    matchups: list[dict] = []

    for key, value in matchups_raw.items():
        # Keys are "0", "1", ... (numeric strings); skip "count"
        if not key.isdigit():
            continue

        try:
            # value = {"matchup": { "week": "1", ..., "0": {"teams": {...}} }}
            matchup_raw_val = value.get("matchup", {}) if isinstance(value, dict) else {}
            if not isinstance(matchup_raw_val, dict):
                logger.debug("Unexpected matchup type %s for entry %s", type(matchup_raw_val).__name__, key)
                continue

            # Week number
            week_num: int = 0
            try:
                week_num = int(matchup_raw_val.get("week", 0))
            except (TypeError, ValueError):
                week_num = 0

            # Teams live at matchup["0"]["teams"] — Yahoo wraps them one level deep
            teams_wrapper = matchup_raw_val.get("0", {})
            teams_data: Optional[dict] = None
            if isinstance(teams_wrapper, dict):
                teams_data = teams_wrapper.get("teams")

            if not isinstance(teams_data, dict):
                logger.debug("No teams dict found in matchup entry %s (wrapper=%s)", key, type(teams_wrapper).__name__)
                continue

            team_entries: list[list] = []
            for tkey, tval in teams_data.items():
                if tkey.isdigit():
                    team_entries.append(tval.get("team", []) if isinstance(tval, dict) else [])

            if len(team_entries) < 2:
                logger.debug("Expected 2 teams in matchup %s, got %d", key, len(team_entries))
                continue

            def _parse_team(team_list: list) -> tuple[str, str, str, dict]:
                """Return (team_key, team_name, manager_name, live_stats)."""
                if not team_list:
                    return ("", "", "", {})

                # team_list[0] is a list of metadata dicts
                meta_list: list = team_list[0] if isinstance(team_list[0], list) else []
                team_key = ""
                team_name = ""
                manager_name = ""

                for meta_item in meta_list:
                    if not isinstance(meta_item, dict):
                        continue
                    if "team_key" in meta_item:
                        team_key = meta_item["team_key"]
                    if "name" in meta_item:
                        team_name = meta_item["name"]
                    if "managers" in meta_item:
                        managers = meta_item["managers"]
                        if isinstance(managers, list) and managers:
                            mgr = managers[0]
                            if isinstance(mgr, dict) and "manager" in mgr:
                                manager_name = mgr["manager"].get("nickname", "") or ""

                # team_list[1] may contain live stats/points
                live_stats: dict = {}
                if len(team_list) > 1 and isinstance(team_list[1], dict):
                    team_stats = (
                        team_list[1].get("team_stats") or team_list[1].get("team_points")
                    )
                    if isinstance(team_stats, dict):
                        stats_list = team_stats.get("stats", [])
                        for stat_entry in stats_list:
                            if isinstance(stat_entry, dict) and "stat" in stat_entry:
                                stat = stat_entry["stat"]
                                stat_id = stat.get("stat_id", "")
                                value_str = stat.get("value", "")
                                try:
                                    live_stats[str(stat_id)] = float(value_str)
                                except (TypeError, ValueError):
                                    pass

                return team_key, team_name, manager_name, live_stats

            t1_key, t1_name, t1_mgr, t1_live = _parse_team(team_entries[0])
            t2_key, t2_name, t2_mgr, t2_live = _parse_team(team_entries[1])

            # Merge live stats: {category_name: {"team1": val, "team2": val}}
            # Translate Yahoo numeric stat IDs to readable category names.
            # Unknown stat IDs are skipped to keep the dict clean.
            live_stats_merged: dict = {}
            all_stat_keys = set(t1_live.keys()) | set(t2_live.keys())
            for stat_key in all_stat_keys:
                cat_name = _YAHOO_STAT_ID_TO_CAT.get(str(stat_key))
                if cat_name is None:
                    continue  # skip unknown/unused stat IDs
                if cat_name in live_stats_merged:
                    continue  # don't overwrite (e.g. both "8" and "55" → "H")
                entry: dict = {}
                if stat_key in t1_live:
                    entry["team1"] = t1_live[stat_key]
                if stat_key in t2_live:
                    entry["team2"] = t2_live[stat_key]
                live_stats_merged[cat_name] = entry

            matchups.append({
                "team1_key": t1_key,
                "team1_name": t1_name,
                "manager1_name": t1_mgr,
                "team2_key": t2_key,
                "team2_name": t2_name,
                "manager2_name": t2_mgr,
                "week": week_num,
                "live_stats": live_stats_merged,
            })

        except Exception as exc:
            logger.warning("Error parsing matchup entry %s: %s", key, exc)
            continue

    return matchups


# ---------------------------------------------------------------------------
# Team week stat projection
# ---------------------------------------------------------------------------

def project_team_week_stats(
    roster_player_ids: list[int],
    db: "Session",
    categories: list[str],
    week_schedule: Optional[dict] = None,
    steamer_lookup: Optional[dict[int, PlayerStats]] = None,
) -> dict[str, float]:
    """Project this week's fantasy category totals for a roster.

    Args:
        roster_player_ids: List of FanGraphs player_ids on the active roster.
        db: SQLAlchemy session.
        categories: League scoring categories to project.
        week_schedule: Optional {player_id: PlayerSchedule} from fetch_weekly_schedule.
        steamer_lookup: Optional {player_id: PlayerStats} with data_source="projection".

    Returns:
        {category: projected_total} rounded to 3 decimal places.
    """
    if not roster_player_ids:
        return {}

    config = HORIZON_CONFIGS[ProjectionHorizon.WEEK]

    # -- Load PlayerStats rows (prefer "actual", fall back to "projection") --
    actual_rows: list[PlayerStats] = (
        db.query(PlayerStats)
        .filter(
            PlayerStats.player_id.in_(roster_player_ids),
            PlayerStats.season == 2026,
            PlayerStats.week.is_(None),
            PlayerStats.data_source == "actual",
        )
        .all()
    )
    actual_map: dict[int, list[PlayerStats]] = {}
    for row in actual_rows:
        actual_map.setdefault(row.player_id, []).append(row)

    proj_rows: list[PlayerStats] = (
        db.query(PlayerStats)
        .filter(
            PlayerStats.player_id.in_(roster_player_ids),
            PlayerStats.season == 2026,
            PlayerStats.week.is_(None),
            PlayerStats.data_source == "projection",
        )
        .all()
    )
    proj_map: dict[int, list[PlayerStats]] = {}
    for row in proj_rows:
        proj_map.setdefault(row.player_id, []).append(row)

    # -- Load Player rows for position info --
    player_rows: list[Player] = (
        db.query(Player)
        .filter(Player.player_id.in_(roster_player_ids))
        .all()
    )
    player_map: dict[int, Player] = {p.player_id: p for p in player_rows}

    # -- Accumulate projections --
    # Counting stats: sum directly.
    # Rate stats: accumulate (weighted_value, weight) pairs for final averaging.
    counting_totals: dict[str, float] = {}
    rate_accum: dict[str, list[tuple[float, float]]] = {}  # cat -> [(value, weight)]

    for pid in roster_player_ids:
        player = player_map.get(pid)
        if player is None:
            continue

        # Prefer actual stats, fall back to projection
        stats_list = actual_map.get(pid) or proj_map.get(pid) or []
        if not stats_list:
            continue

        steamer_row: Optional[PlayerStats] = None
        if steamer_lookup:
            steamer_row = steamer_lookup.get(pid)

        for stats_row in stats_list:
            # Build NormalizedPlayerData
            npd = NormalizedPlayerData(
                player_id=pid,
                name=player.name,
                team=player.team,
                positions=player.positions or [],
                status=player.status,
                stat_type=stats_row.stat_type,
                counting_stats=stats_row.counting_stats or {},
                rate_stats=stats_row.rate_stats or {},
                advanced_stats=stats_row.advanced_stats or {},
                birth_year=player.birth_year,
                injury_status=player.status,
                risk_flag=player.risk_flag,
                risk_note=player.risk_note,
                week_hr_factor=1.0,
                week_run_factor=1.0,
            )

            # Inject schedule factors if available; also resolve actual game count
            team_games: int = 7  # default = full 7-game week
            if week_schedule and pid in week_schedule:
                sched = week_schedule[pid]
                npd.week_hr_factor = getattr(sched, "weather_hr_factor", 1.0)
                npd.week_run_factor = getattr(sched, "vegas_run_factor", 1.0)
                team_games = int(getattr(sched, "team_games", 7) or 7)

            # Build Steamer NormalizedPlayerData if available
            steamer_npd: Optional[NormalizedPlayerData] = None
            if steamer_row is not None:
                steamer_npd = NormalizedPlayerData(
                    player_id=pid,
                    name=player.name,
                    team=player.team,
                    positions=player.positions or [],
                    stat_type=steamer_row.stat_type,
                    counting_stats=steamer_row.counting_stats or {},
                    rate_stats=steamer_row.rate_stats or {},
                    advanced_stats=steamer_row.advanced_stats or {},
                )

            # Park factor from player's team
            park_factor = PARK_FACTORS.get(player.team, 1.0)

            positions = player.positions or []
            is_pitcher = stats_row.stat_type == "pitching"
            is_sp = "SP" in positions or (is_pitcher and "RP" not in positions)

            try:
                if is_pitcher:
                    proj = project_pitcher_stats(
                        player=npd,
                        config=config,
                        is_sp=is_sp,
                        steamer_data=steamer_npd,
                    )
                    # Weight for rate stats: projected IP
                    ip_weight = proj.get("IP", config.sp_ip if is_sp else config.rp_ip)
                else:
                    proj = project_hitter_stats(
                        player=npd,
                        config=config,
                        steamer_data=steamer_npd,
                        park_factor=park_factor,
                    )
                    # Scale counting stats to actual games scheduled this week.
                    # hitter_pa=26 assumes 7 games; a short week (e.g. 4 games)
                    # should produce ~4/7 of that volume, not a full week's worth.
                    if team_games != 7:
                        game_scale = team_games / 7.0
                        proj = {
                            cat: (val * game_scale if cat not in _RATE_CATS else val)
                            for cat, val in proj.items()
                        }
                    # Weight for rate stats: approximate PA
                    ip_weight = float(config.hitter_pa)
            except Exception as exc:
                logger.warning("Projection failed for player %d: %s", pid, exc)
                continue

            for cat in categories:
                if cat not in proj:
                    continue
                val = proj[cat]
                if cat in _RATE_CATS:
                    rate_accum.setdefault(cat, []).append((val, ip_weight))
                else:
                    counting_totals[cat] = counting_totals.get(cat, 0.0) + val

    # -- Finalize rate stats via weighted average --
    result: dict[str, float] = {}
    for cat, total in counting_totals.items():
        result[cat] = round(total, 3)

    for cat, pairs in rate_accum.items():
        if not pairs:
            continue
        total_weight = sum(w for _, w in pairs)
        if total_weight == 0.0:
            weighted_avg = sum(v for v, _ in pairs) / len(pairs)
        else:
            weighted_avg = sum(v * w for v, w in pairs) / total_weight
        result[cat] = round(weighted_avg, 3)

    return result


# ---------------------------------------------------------------------------
# Category comparison
# ---------------------------------------------------------------------------

def compare_category_projections(
    team1_stats: dict[str, float],
    team2_stats: dict[str, float],
    categories: list[str],
) -> dict:
    """Compare category projections and assign edges.

    Returns:
        {category: {"team1": float, "team2": float, "edge": "team1"|"team2"|"toss_up"}}
    """
    result: dict[str, dict] = {}

    for cat in categories:
        t1 = team1_stats.get(cat, 0.0)
        t2 = team2_stats.get(cat, 0.0)

        if cat in _RATE_CATS:
            # Rate-stat thresholds
            if cat in {"AVG", "OBP", "SLG", "OPS"}:
                threshold = 0.003  # 3 batting-average points; 0.259 vs 0.263 is a real edge
            elif cat in {"ERA", "WHIP"}:
                threshold = 0.05
            elif cat == "K/9":
                threshold = 0.3
            else:
                threshold = 0.005

            diff = abs(t1 - t2)
            if diff <= threshold:
                edge = "toss_up"
            else:
                if cat in _LOWER_IS_BETTER:
                    # Lower value is better
                    edge = "team1" if t1 < t2 else "team2"
                else:
                    edge = "team1" if t1 > t2 else "team2"
        else:
            # Counting stats: 5% of the larger value
            max_val = max(t1, t2)
            threshold = max_val * 0.05 if max_val > 0 else 0.0
            diff = abs(t1 - t2)
            if diff <= threshold:
                edge = "toss_up"
            else:
                edge = "team1" if t1 > t2 else "team2"

        result[cat] = {"team1": t1, "team2": t2, "edge": edge}

    return result


# ---------------------------------------------------------------------------
# Claude narrative generation
# ---------------------------------------------------------------------------

import re as _re

def _strip_markdown(text: str) -> str:
    """Remove common markdown artifacts from LLM output.

    Belt-and-suspenders safety net for when the model ignores the plain-prose
    instruction.  Handles: ATX headers (#), bold/italic (**/*/_), inline code,
    horizontal rules, and leading list markers (- / * / 1.).
    """
    # ATX headers: "# Foo" → "Foo"
    text = _re.sub(r"^\s*#{1,6}\s+", "", text, flags=_re.MULTILINE)
    # Bold/italic: **text** or *text* or __text__ or _text_
    text = _re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)
    text = _re.sub(r"_{1,2}(.*?)_{1,2}", r"\1", text)
    # Inline code
    text = _re.sub(r"`([^`]+)`", r"\1", text)
    # Horizontal rules
    text = _re.sub(r"^\s*[-*_]{3,}\s*$", "", text, flags=_re.MULTILINE)
    # Leading list markers
    text = _re.sub(r"^\s*[-*]\s+", "", text, flags=_re.MULTILINE)
    text = _re.sub(r"^\s*\d+\.\s+", "", text, flags=_re.MULTILINE)
    # Collapse extra blank lines left behind
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def generate_matchup_narrative(
    matchup_data: dict,
    league_categories: list[str],
    anthropic_api_key: str,
) -> str:
    """Generate a 3-5 sentence matchup preview using Claude Haiku.

    Args:
        matchup_data: Dict with keys team1_name, team2_name, manager1_name,
                      manager2_name, category_projections (from compare_category_projections),
                      and optionally live_stats ({cat: {team1: val, team2: val}} from Yahoo
                      scoreboard — actual accumulated totals so far this week).
        league_categories: Ordered list of scoring categories.
        anthropic_api_key: Anthropic API key.

    Returns:
        Narrative string (stripped of leading/trailing whitespace).
    """
    team1_name = matchup_data.get("team1_name", "Team 1")
    team2_name = matchup_data.get("team2_name", "Team 2")
    manager1 = matchup_data.get("manager1_name") or "Manager 1"
    manager2 = matchup_data.get("manager2_name") or "Manager 2"
    cat_proj = matchup_data.get("category_projections", {})
    live_stats: dict = matchup_data.get("live_stats") or {}

    # Summarize edges
    team1_edges: list[str] = []
    team2_edges: list[str] = []
    toss_ups: list[str] = []

    for cat in league_categories:
        if cat not in cat_proj:
            continue
        info = cat_proj[cat]
        edge = info.get("edge", "toss_up")
        t1_val = info.get("team1", 0.0)
        t2_val = info.get("team2", 0.0)

        if edge == "team1":
            team1_edges.append(f"{cat} ({t1_val:.3f} vs {t2_val:.3f})")
        elif edge == "team2":
            team2_edges.append(f"{cat} ({t1_val:.3f} vs {t2_val:.3f})")
        else:
            toss_ups.append(f"{cat} ({t1_val:.3f} vs {t2_val:.3f})")

    team1_edge_count = len(team1_edges)
    team2_edge_count = len(team2_edges)

    if team1_edge_count > team2_edge_count:
        projected_winner = f"{team1_name} (managed by {manager1})"
        projected_winner_edge = team1_edge_count
        projected_loser_edge = team2_edge_count
    elif team2_edge_count > team1_edge_count:
        projected_winner = f"{team2_name} (managed by {manager2})"
        projected_winner_edge = team2_edge_count
        projected_loser_edge = team1_edge_count
    else:
        projected_winner = "neither team — this is a coin flip"
        projected_winner_edge = team1_edge_count
        projected_loser_edge = team2_edge_count

    # Build the live-stats section if we have actual accumulated data.
    # live_stats format: {cat_name: {team1: float, team2: float}}
    live_stats_lines: list[str] = []
    if live_stats:
        live_stats_lines.append(
            "\nActual stats accumulated so far this week (from Yahoo scoreboard — "
            "these are REAL numbers, not projections):"
        )
        for cat, vals in live_stats.items():
            t1_live = vals.get("team1")
            t2_live = vals.get("team2")
            if t1_live is not None and t2_live is not None:
                live_stats_lines.append(
                    f"  {cat}: {team1_name} {t1_live} vs {team2_name} {t2_live}"
                )
        live_stats_lines.append(
            "Use the live stats to ground the narrative — if the week is underway, "
            "acknowledge what's actually happened and frame what's still at stake."
        )

    user_prompt = (
        f"Matchup preview for this week's fantasy baseball H2H:\n\n"
        f"Team 1: {team1_name} (manager: {manager1})\n"
        f"Team 2: {team2_name} (manager: {manager2})\n\n"
        f"Projected category edges for {team1_name}: "
        f"{', '.join(team1_edges) if team1_edges else 'none'}\n"
        f"Projected category edges for {team2_name}: "
        f"{', '.join(team2_edges) if team2_edges else 'none'}\n"
        f"Toss-up categories: {', '.join(toss_ups) if toss_ups else 'none'}\n\n"
        f"Projected winner: {projected_winner} "
        f"({projected_winner_edge} vs {projected_loser_edge} category edges)"
        + ("\n" + "\n".join(live_stats_lines) if live_stats_lines else "")
        + "\n\nWrite a 3-5 sentence matchup preview. Highlight the key narrative (who has the edge "
        "and why), call out the biggest single-category advantage, and name the most contested "
        "toss-up categories where either team could swing the result. "
        "If live stats are provided and the week is underway, weave in the actual scoreline — "
        "what's locked up, what's still in play. Be punchy and specific."
    )

    from fantasai.brain.writer_persona import SYSTEM_PROMPT as _WRITER_PERSONA
    system_prompt = (
        _WRITER_PERSONA
        + "\n\n"
        "You are writing a MATCHUP PREVIEW — not a player blurb. "
        "Same voice, same persona, but focused on the head-to-head battle between two teams.\n\n"
        "MATCHUP WRITING RULES:\n"
        "• Plain prose only. No markdown — no headers (#), no bold (**), no bullets.\n"
        "• 3–5 sentences. Punchy. Specific. Every sentence earns its place.\n"
        "• Lead with the narrative, not the scoreline.\n"
        "• BANNED phrases (do not use under any circumstances):\n"
        "  - 'not particularly close'\n"
        "  - 'it's not particularly close'\n"
        "  - 'comfortable margin'\n"
        "  - 'clear advantage'\n"
        "  - 'significant gap'\n"
        "  - 'dominant performance'\n"
        "  - 'commanding lead'\n"
        "  Instead: reach for specific, vivid language about the categories, "
        "the teams, or the matchup stakes. Say WHY one team has the edge, not just THAT they do.\n"
        "• HARD RULE — PERSONALITY MINIMUM: MINIMUM TWO personality elements required. "
        "Include AT LEAST ONE analogy or cultural reference (Canadian, baseball, pop culture) "
        "AND AT LEAST ONE signature phrase or irreverent observation. 'When they fit naturally' "
        "is not an excuse — fit them. A matchup preview with zero voice is a failure."
    )

    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 400,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_prompt}],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content_blocks = data.get("content", [])
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    return _strip_markdown(block["text"].strip())
    except Exception as exc:
        logger.warning("Claude narrative generation failed: %s", exc)

    # Fallback: minimal narrative
    return (
        f"{team1_name} faces {team2_name} this week. "
        f"Projected category edge: {team1_edge_count}-{team2_edge_count} "
        f"in favor of {projected_winner}."
    ).strip()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_league_matchups(
    db: "Session",
    league: "League",
    access_token: str,
    anthropic_api_key: str,
    season: int = 2026,
) -> int:
    """Analyze all matchups for a league and upsert MatchupAnalysis rows.

    Called by APScheduler (daily) and POST /matchups/analyze.

    Returns:
        Number of matchups successfully analyzed.
    """
    # 1. Current week bounds
    try:
        week_start, week_end = get_current_week_bounds()
    except Exception as exc:
        logger.warning("Could not determine current week bounds: %s", exc)
        week_start = date.today()
        week_end = date.today()

    # 2. Fetch scoreboard — week number comes from Yahoo, not ISO calendar
    scoreboard = fetch_league_scoreboard(
        access_token=access_token,
        league_key=league.league_id,
    )
    if not scoreboard:
        logger.warning(
            "No scoreboard data for league %s", league.league_id,
        )
        return 0

    # Extract the fantasy week number from the first scoreboard entry.
    # Yahoo returns the actual fantasy week (e.g. 4), NOT the ISO calendar
    # week (e.g. 15 in mid-April).  Never fall back to ISO week — ISO week 15
    # in early April would overwrite valid week-4 data with the wrong week number,
    # causing users to see stale matchups from "week 15" instead of the current week.
    week_num: int = int(scoreboard[0].get("week") or 0)
    if week_num == 0:
        logger.error(
            "Yahoo returned week=0 for league %s — aborting to prevent stale data corruption. "
            "Click Refresh again once the Yahoo API is responsive.",
            league.league_id,
        )
        return 0

    # Purge any rows for this league+season where the week number looks like an
    # ISO calendar week rather than a real fantasy week.  ISO weeks in April are
    # typically 14–15; fantasy week 4 would never legitimately produce week>13
    # this early in the season.  We clean up anything > week_num + 5 to be safe.
    try:
        stale = (
            db.query(MatchupAnalysis)
            .filter(
                MatchupAnalysis.league_id == league.league_id,
                MatchupAnalysis.season == season,
                MatchupAnalysis.week > week_num + 5,
            )
            .all()
        )
        if stale:
            logger.warning(
                "Purging %d stale MatchupAnalysis rows for league %s "
                "(stored week > %d + 5, likely ISO-week corruption)",
                len(stale), league.league_id, week_num,
            )
            for row in stale:
                db.delete(row)
            db.flush()
    except Exception as exc:
        logger.warning("Stale matchup cleanup failed (non-fatal): %s", exc)

    # 4. Weekly schedule for weather/Vegas enrichment
    week_schedule: dict = {}
    try:
        week_schedule_raw = fetch_weekly_schedule(
            week_start=week_start,
            week_end=week_end,
            db=db,
        )
        if isinstance(week_schedule_raw, dict):
            week_schedule = week_schedule_raw
    except Exception as exc:
        logger.warning("Could not fetch weekly schedule: %s", exc)

    # 5. Build steamer lookup from DB (projection rows for all potentially relevant players)
    all_proj_rows: list[PlayerStats] = (
        db.query(PlayerStats)
        .filter(
            PlayerStats.season == season,
            PlayerStats.week.is_(None),
            PlayerStats.data_source == "projection",
        )
        .all()
    )
    steamer_lookup: dict[int, PlayerStats] = {}
    for row in all_proj_rows:
        # If multiple rows per player, keep the last seen (stat_type differentiated elsewhere)
        steamer_lookup[row.player_id] = row

    # Build team key -> Team lookup from the league's teams
    team_by_yahoo_key: dict = {}
    for team in (league.teams or []):
        if team.yahoo_team_key:
            team_by_yahoo_key[team.yahoo_team_key] = team

    categories: list[str] = league.scoring_categories or []

    analyzed_count = 0

    # 6. Analyze each matchup
    for matchup_info in scoreboard:
        t1_key = matchup_info.get("team1_key", "")
        t2_key = matchup_info.get("team2_key", "")
        t1_name = matchup_info.get("team1_name", "")
        t2_name = matchup_info.get("team2_name", "")
        mgr1 = matchup_info.get("manager1_name")
        mgr2 = matchup_info.get("manager2_name")
        live_stats = matchup_info.get("live_stats", {})

        if not t1_key or not t2_key:
            logger.debug("Skipping matchup with missing team keys")
            continue

        # a. Look up both teams
        team1 = team_by_yahoo_key.get(t1_key)
        team2 = team_by_yahoo_key.get(t2_key)

        # Exclude bench (BN) and IL players — they don't contribute to weekly
        # scoring.  Projecting the full roster inflates counting stat totals
        # by 20–30% because bench players are counted at full 26 PA each.
        def _active_roster(team: Optional[object]) -> list[int]:
            if team is None:
                return []
            all_ids: list[int] = list(team.roster or [])
            excluded: set[int] = set(
                list(getattr(team, "bench_player_ids", None) or [])
                + list(getattr(team, "il_player_ids", None) or [])
            )
            return [pid for pid in all_ids if pid not in excluded]

        roster1: list[int] = _active_roster(team1)
        roster2: list[int] = _active_roster(team2)

        # b. Project each team's week stats
        try:
            team1_stats = project_team_week_stats(
                roster_player_ids=roster1,
                db=db,
                categories=categories,
                week_schedule=week_schedule,
                steamer_lookup=steamer_lookup,
            )
        except Exception as exc:
            logger.warning("Failed to project stats for team %s: %s", t1_key, exc)
            team1_stats = {}

        try:
            team2_stats = project_team_week_stats(
                roster_player_ids=roster2,
                db=db,
                categories=categories,
                week_schedule=week_schedule,
                steamer_lookup=steamer_lookup,
            )
        except Exception as exc:
            logger.warning("Failed to project stats for team %s: %s", t2_key, exc)
            team2_stats = {}

        # c. Compare categories
        cat_proj = compare_category_projections(team1_stats, team2_stats, categories)

        # d. Generate narrative
        narrative_input = {
            "team1_name": t1_name,
            "team2_name": t2_name,
            "manager1_name": mgr1,
            "manager2_name": mgr2,
            "category_projections": cat_proj,
            "live_stats": live_stats,  # actual accumulated stats mid-week from Yahoo
        }
        try:
            narrative = generate_matchup_narrative(
                matchup_data=narrative_input,
                league_categories=categories,
                anthropic_api_key=anthropic_api_key,
            )
        except Exception as exc:
            logger.warning(
                "Narrative generation failed for %s vs %s: %s", t1_name, t2_name, exc
            )
            narrative = None

        # e. Upsert MatchupAnalysis row
        try:
            existing: Optional[MatchupAnalysis] = (
                db.query(MatchupAnalysis)
                .filter(
                    MatchupAnalysis.league_id == league.league_id,
                    MatchupAnalysis.season == season,
                    MatchupAnalysis.week == week_num,
                    MatchupAnalysis.team1_key == t1_key,
                    MatchupAnalysis.team2_key == t2_key,
                )
                .first()
            )

            if existing is None:
                existing = MatchupAnalysis(
                    league_id=league.league_id,
                    season=season,
                    week=week_num,
                    team1_key=t1_key,
                    team2_key=t2_key,
                )
                db.add(existing)

            existing.team1_name = t1_name
            existing.team2_name = t2_name
            existing.manager1_name = mgr1
            existing.manager2_name = mgr2
            existing.category_projections = cat_proj
            existing.live_stats = live_stats if live_stats else None
            existing.narrative = narrative
            existing.suggestions = []
            existing.generated_at = datetime.now(timezone.utc)

            analyzed_count += 1

        except Exception as exc:
            logger.warning(
                "Failed to upsert MatchupAnalysis for %s vs %s: %s", t1_key, t2_key, exc
            )
            continue

    # 7. Commit
    try:
        db.commit()
    except Exception as exc:
        logger.error(
            "Failed to commit matchup analyses for league %s: %s", league.league_id, exc
        )
        db.rollback()
        return 0

    logger.info(
        "Analyzed %d matchups for league %s week %d",
        analyzed_count,
        league.league_id,
        week_num,
    )
    return analyzed_count
