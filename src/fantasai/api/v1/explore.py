from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.config import settings
from fantasai.models.player import Player, PlayerStats
from fantasai.models.prospect import ProspectProfile
from fantasai.schemas.explore import (
    ChatMessage,
    ExploreChatRequest,
    InjuryContext,
    PavComponents,
    PlayerContextResponse,
    ScheduleContext,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/explore", tags=["explore"])

# Schedule / park / injury imports — lazy-loaded to avoid circular imports at module level
from fantasai.engine.schedule import (
    PARK_FACTORS,
    build_player_week_context,
    get_current_week_bounds,
    fetch_weekly_schedule,
)

# Which ranking list name to show in the stat card UI
_RANK_LIST_NAME = "Predictive Rankings (Rest of Season)"

# Stat labels for the RAG context block (batting)
_BATTING_STAT_LABELS = {
    "PA": "Plate Appearances", "R": "Runs", "HR": "HR", "RBI": "RBI",
    "SB": "SB", "AVG": "AVG", "OPS": "OPS", "OBP": "OBP",
    "xwOBA": "xwOBA", "Barrel%": "Barrel%", "HardHit%": "HardHit%",
    "wRC+": "wRC+",
}
_PITCHING_STAT_LABELS = {
    "IP": "IP", "W": "W", "SV": "SV", "SO": "K", "ERA": "ERA", "WHIP": "WHIP",
    "xERA": "xERA", "K/9": "K/9", "BB/9": "BB/9", "FIP": "FIP",
    "Stuff+": "Stuff+", "SIERA": "SIERA",
}


# ---------------------------------------------------------------------------
# Async Anthropic client (for streaming)
# ---------------------------------------------------------------------------

def _async_llm_client():
    """Return an async Anthropic client or None if no API key is configured."""
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
        return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    except Exception as exc:
        logger.warning("Could not create AsyncAnthropic client: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Helper: fetch player context from DB
# ---------------------------------------------------------------------------

def _get_player_context(
    player_id: int,
    db: Session,
    owned_by_map: dict[int, str],
) -> Optional[PlayerContextResponse]:
    """Build a PlayerContextResponse for a single player from DB data."""
    player = db.get(Player, player_id)
    if not player:
        return None

    # Determine primary stat type (prefer pitching for two-way if pitcher)
    positions = player.positions or []
    is_pitcher = any(p in ("SP", "RP", "P") for p in positions)
    stat_type = "pitching" if is_pitcher else "batting"

    # Current season actual stats
    actual_row = (
        db.query(PlayerStats)
        .filter(
            PlayerStats.player_id == player_id,
            PlayerStats.season == 2026,
            PlayerStats.stat_type == stat_type,
            PlayerStats.data_source == "actual",
        )
        .first()
    )
    actual_stats: dict = {}
    if actual_row:
        actual_stats = {
            **(actual_row.counting_stats or {}),
            **(actual_row.rate_stats or {}),
            **(actual_row.advanced_stats or {}),
        }

    # Steamer rest-of-season projection
    proj_row = (
        db.query(PlayerStats)
        .filter(
            PlayerStats.player_id == player_id,
            PlayerStats.season == 2026,
            PlayerStats.stat_type == stat_type,
            PlayerStats.data_source == "projection",
        )
        .first()
    )
    projection_stats: Optional[dict] = None
    if proj_row:
        projection_stats = {
            **(proj_row.counting_stats or {}),
            **(proj_row.rate_stats or {}),
            **(proj_row.advanced_stats or {}),
        }

    # Overall rank from rankings cache
    overall_rank: Optional[int] = None
    rank_score: Optional[float] = None
    try:
        from fantasai.api.v1.recommendations import _get_cached_raw_rankings
        from fantasai.engine.projection import ProjectionHorizon
        from fantasai.api.v1.rankings import RANKINGS_DEFAULT_CATEGORIES

        raw = _get_cached_raw_rankings(RANKINGS_DEFAULT_CATEGORIES, ProjectionHorizon.SEASON)
        if raw:
            _, predictive = raw
            for r in predictive:
                if r.player_id == player_id and r.stat_type == stat_type:
                    overall_rank = r.overall_rank
                    rank_score = round(r.score, 3)
                    break
    except Exception:
        logger.debug("Could not fetch ranking for player %s", player_id, exc_info=True)

    # PAV — prospects only (pav_score is pre-computed and cached on ProspectProfile)
    is_prospect = False
    pav_score: Optional[float] = None
    pav_components: Optional[PavComponents] = None

    prospect = db.get(ProspectProfile, player_id)
    if prospect and prospect.pav_score is not None:
        is_prospect = True
        pav_score = round(prospect.pav_score, 1)
        # Build component breakdown from stored PAV inputs if available
        try:
            from fantasai.brain.pav_scorer import calculate_pav
            result = calculate_pav(
                prospect_grade=float(prospect.pipeline_grade or prospect.ba_grade or prospect.fg_grade or 50.0),
                age_adj_perf=float(prospect.pav_score),  # best proxy available from stored data
                vertical_velocity=float(prospect.levels_in_season or 0) * 25.0 if prospect.levels_in_season else 50.0,
                eta_proximity=50.0,  # default midpoint
                position=(positions[0] if positions else "OF"),
                pitcher=is_pitcher,
            )
            cs = result.get("component_scores", {})
            pav_components = PavComponents(
                prospect_grade=round(cs.get("prospect_grade", float(prospect.pipeline_grade or 50.0)), 1),
                age_adj_performance=round(cs.get("age_adj_perf", 50.0), 1),
                vertical_velocity=round(cs.get("vertical_velocity", 50.0), 1),
                eta_proximity=round(cs.get("eta_proximity", 50.0), 1),
            )
        except Exception:
            logger.debug("PAV component calculation failed for player %s", player_id, exc_info=True)

    # Injury / health context
    injury_ctx = _get_injury_context(player)

    # Schedule context (non-fatal — returns None if schedule not available)
    schedule_ctx = _get_schedule_context(player_id, positions, stat_type, db)

    return PlayerContextResponse(
        player_id=player_id,
        name=player.name,
        team=player.team or "—",
        positions=positions,
        stat_type=stat_type,
        mlbam_id=player.mlbam_id,
        bats=player.bats,
        throws=player.throws,
        actual_stats=actual_stats,
        projection_stats=projection_stats,
        overall_rank=overall_rank,
        rank_score=rank_score,
        rank_list_name=_RANK_LIST_NAME,
        is_prospect=is_prospect,
        pav_score=pav_score,
        pav_components=pav_components,
        owned_by=owned_by_map.get(player_id),
        injury=injury_ctx,
        schedule=schedule_ctx,
    )


def _get_injury_context(player) -> Optional[InjuryContext]:
    """Build InjuryContext from Player ORM object.

    Returns None if the player has no active injury record and no risk flag.
    """
    has_injury = player.injury_record is not None
    has_risk = bool(player.risk_flag)
    if not has_injury and not has_risk:
        return None
    ir = player.injury_record
    return InjuryContext(
        status=ir.status if ir else None,
        description=ir.injury_description if ir else None,
        expected_return=ir.return_date.isoformat() if ir and ir.return_date else None,
        risk_flag=player.risk_flag,
        risk_note=player.risk_note,
    )


def _get_schedule_context(
    player_id: int,
    positions: list[str],
    stat_type: str,
    db,
) -> Optional[ScheduleContext]:
    """Build ScheduleContext for a player from the cached weekly schedule.

    Uses the schedule already fetched by the rankings pipeline (warm cache).
    Falls back to a fresh fetch if the cache is cold (non-fatal).
    Returns None if no schedule data is available.
    """
    from datetime import date as _date
    from fantasai.models.player import Player as _Player

    # Try warm cache first
    try:
        from fantasai.api.v1.recommendations import get_cached_week_schedule
        week_sched = get_cached_week_schedule()
    except Exception:
        week_sched = {}

    ps = week_sched.get(player_id)

    # Cache cold — attempt a fresh fetch (non-fatal)
    if ps is None:
        try:
            from fantasai.config import settings as _settings
            week_start, week_end = get_current_week_bounds()
            fresh = fetch_weekly_schedule(
                week_start, week_end, db,
                vegas_api_key=_settings.the_odds_api_key or None,
            )
            ps = fresh.get(player_id)
        except Exception:
            logger.debug("Schedule fetch failed for player %s (non-fatal)", player_id, exc_info=True)

    if ps is None:
        return None

    today = _date.today()

    # Find today's game in batter_game_log
    today_opponent = None
    today_is_home = None
    today_park = None
    today_park_factor = None
    today_sp_name = None
    today_sp_throws = None

    today_entry = next(
        (g for g in (ps.batter_game_log or []) if g.get("date") == today.isoformat()),
        None,
    )
    if today_entry:
        today_opponent = today_entry.get("opponent_abbr")
        today_is_home = today_entry.get("is_home", True)
        # Home park: our park if home game, opponent's park if away
        today_park = (ps.home_park or today_opponent) if today_is_home else today_opponent
        if today_park:
            today_park_factor = PARK_FACTORS.get(today_park, 1.0)

        # Look up opposing SP for batters
        if stat_type == "batting":
            sp_mlbam = today_entry.get("sp_mlbam_id")
            if sp_mlbam:
                try:
                    sp_player = db.query(_Player).filter(_Player.mlbam_id == sp_mlbam).first()
                    if sp_player:
                        today_sp_name = sp_player.name
                        today_sp_throws = sp_player.throws
                except Exception:
                    pass

    # Build the pre-formatted week context text (notable items only)
    week_context_text = None
    try:
        week_context_text = build_player_week_context(ps, stat_type, positions)
    except Exception:
        pass

    return ScheduleContext(
        games_this_week=ps.team_games,
        probable_starts=ps.probable_starts,
        future_starts=ps.future_starts,
        opponent_teams=list(ps.opponent_teams or []),
        today_opponent=today_opponent,
        today_is_home=today_is_home,
        today_park=today_park,
        today_park_factor=today_park_factor,
        today_sp_name=today_sp_name,
        today_sp_throws=today_sp_throws,
        weather_hr_factor=ps.weather_hr_factor,
        weather_temp_f=ps.weather_temp_f,
        weather_wind_mph=ps.weather_wind_mph,
        vegas_run_factor=ps.vegas_run_factor,
        week_context_text=week_context_text,
    )


def _build_owned_by_map(league_id: Optional[str], db: Session) -> dict[int, str]:
    """Return {player_id: team_name} for all rostered players in a league.

    Teams store their roster as a JSON list of FanGraphs player IDs (int)
    in the Team.roster column — no separate RosterPlayer table.
    """
    if not league_id:
        return {}
    try:
        from fantasai.models.league import Team
        teams = db.query(Team).filter(Team.league_id == league_id).all()
        mapping: dict[int, str] = {}
        for team in teams:
            roster_ids = team.roster or []
            team_name = team.team_name or f"Team {team.team_id}"
            for pid in roster_ids:
                if pid is not None:
                    mapping[int(pid)] = team_name
        return mapping
    except Exception:
        logger.debug("Could not build owned_by_map for league %s", league_id, exc_info=True)
        return {}


# ---------------------------------------------------------------------------
# RAG context block builder
# ---------------------------------------------------------------------------

def _format_stats_block(stats: dict, stat_type: str, label: str) -> str:
    """Format a stats dict into a human-readable text block."""
    if not stats:
        return f"{label}: No data available\n"

    labels = _PITCHING_STAT_LABELS if stat_type == "pitching" else _BATTING_STAT_LABELS
    lines = [f"{label}:"]
    for key, human in labels.items():
        val = stats.get(key)
        if val is not None:
            if isinstance(val, float):
                lines.append(f"  {human}: {val:.3f}" if val < 1.0 and key not in ("ERA", "WHIP", "FIP", "SIERA", "xERA", "K/9", "BB/9") else f"  {human}: {val:.2f}")
            else:
                lines.append(f"  {human}: {val}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Pace projection helpers
# ---------------------------------------------------------------------------

def _pace_adjust(stats: dict, stat_type: str) -> tuple[dict, str]:
    """Return (pace_dict, sample_note) for inclusion in the RAG context.

    Pace scales current counting stats to a full-season equivalent:
      - Batters: per 600 PA
      - Pitchers: per 180 IP

    The sample_note describes the reliability of the projection so the
    LLM can calibrate how much weight to give the pace numbers.
    """
    if stat_type == "batting":
        pa = float(stats.get("PA") or 0)
        if pa < 5:
            return {}, ""
        scale = 600.0 / pa
        pace = {}
        for key in ("R", "HR", "RBI", "SB"):
            val = stats.get(key)
            if val is not None:
                pace[key] = int(round(float(val) * scale))
        if pa < 20:
            note = f"EXTREME small sample ({int(pa)} PA) — pace numbers are almost certainly misleading; acknowledge the absurdity if asked"
        elif pa < 60:
            note = f"Small sample ({int(pa)} PA) — directional only, treat with scepticism"
        elif pa < 150:
            note = f"Moderate sample ({int(pa)} PA) — pace is a reasonable estimate, not gospel"
        else:
            note = f"{int(pa)} PA — pace estimate is reasonably stable"
        return pace, note

    else:  # pitching
        ip = float(stats.get("IP") or 0)
        if ip < 3:
            return {}, ""
        scale = 180.0 / ip
        pace = {}
        for key in ("W", "SO", "SV", "HLD"):
            val = stats.get(key)
            if val is not None:
                pace[key] = int(round(float(val) * scale))
        if ip < 10:
            note = f"EXTREME small sample ({ip:.1f} IP) — pace numbers are almost certainly misleading"
        elif ip < 30:
            note = f"Small sample ({ip:.1f} IP) — directional only, treat with scepticism"
        elif ip < 60:
            note = f"Moderate sample ({ip:.1f} IP) — pace is a reasonable estimate"
        else:
            note = f"{ip:.1f} IP — pace estimate is reasonably stable"
        return pace, note


# ---------------------------------------------------------------------------
# Time-horizon detection
# ---------------------------------------------------------------------------

def _detect_horizon(user_message: str) -> Optional[str]:
    """Heuristically detect the user's intended time horizon.

    Returns one of: "week", "month", "season", "dynasty", "current", None.
    """
    msg = user_message.lower()

    # Dynasty / multi-year (check first — most specific)
    if any(k in msg for k in [
        "next year", "dynasty", "2027", "2028", "long term", "long-term",
        "future seasons", "career", "keeper", "age curve", "farm system",
    ]):
        return "dynasty"

    # Rest of season
    if any(k in msg for k in [
        "rest of season", "rest of the season", "rest of year", " ros ",
        "full season", "going forward", "season-long", "season long",
    ]):
        return "season"

    # This month (~1–3 weeks out)
    if any(k in msg for k in [
        "this month", "next month", "next few weeks", "next couple weeks",
        "next 3 weeks", "next 4 weeks", "short term", "short-term",
    ]):
        return "month"

    # This week / start-sit
    if any(k in msg for k in [
        "this week", "today", "tonight", "start him", "start her", "start them",
        "stream", "tomorrow", "this weekend", "this start", "start or sit",
        "start/sit", "should i start", "should i sit", "plug in",
    ]):
        return "week"

    # Current season retrospective
    if any(k in msg for k in [
        "so far this year", "so far this season", "ytd", "2026 stats",
        "through today", "this season so far", "year to date", "so far",
    ]):
        return "current"

    return None


# Horizon framing blocks injected into the RAG context
_HORIZON_FRAMES: dict[str, dict] = {
    "week": {
        "label": "This Week",
        "blend": "65% YTD actuals / 35% talent (Steamer)",
        "volume": "~26 PA per hitter / ~6 IP per SP",
        "guidance": (
            "Recent form dominates. Weight YTD rate stats and current streaks heavily. "
            "Matchup and health context is critical at this horizon."
        ),
    },
    "month": {
        "label": "This Month",
        "blend": "40% YTD actuals / 60% talent (Steamer + xStats)",
        "volume": "~100 PA per hitter / ~28 IP per SP",
        "guidance": (
            "Balanced window. Steamer talent signal (60%) anchors the projection "
            "but YTD performance (40%) carries real signal. xStats are stabilising at this sample range."
        ),
    },
    "season": {
        "label": "Rest of Season",
        "blend": "15% YTD actuals / 85% talent (Steamer + xStats)",
        "volume": "~540 PA per hitter / ~170 IP per SP",
        "guidance": (
            "Steamer projections and process metrics (xwOBA, xERA) drive ROS value. "
            "YTD actuals are a small correction signal (15%). "
            "Reference the Predictive Rest of Season ranking as the primary composite."
        ),
    },
    "dynasty": {
        "label": "Dynasty / Beyond 2026",
        "blend": "Age curve + Steamer long-term trajectory + prospect PAV",
        "volume": "Multi-year",
        "guidance": (
            "Weight age curve (batter peak: 26–27; pitcher peak: 27–28), "
            "Steamer long-term projections, and PAV score for prospects. "
            "2026 YTD stats are near-noise at this horizon. "
            "Identify whether this player is ascending, peaking, or entering decline."
        ),
    },
    "current": {
        "label": "2026 Season — YTD Performance",
        "blend": "100% YTD actuals (lookback z-scores)",
        "volume": "Season to date",
        "guidance": (
            "Focus on what has actually happened. Reference Current Season rankings. "
            "Note sample size limitations explicitly if PA < 150 or IP < 40."
        ),
    },
}


def _format_horizon_block(horizon: str) -> str:
    frame = _HORIZON_FRAMES.get(horizon)
    if not frame:
        return ""
    return (
        "=== ANALYSIS FRAME ===\n"
        f"Detected question horizon: {frame['label']}\n"
        f"Ranking blend: {frame['blend']}\n"
        f"Volume window: {frame['volume']}\n"
        f"Guidance: {frame['guidance']}\n"
    )


def _build_rag_context(contexts: list[PlayerContextResponse], horizon: Optional[str] = None) -> str:
    """Build the full RAG context block string for all selected players."""
    parts: list[str] = ["=== PLAYER DATA (source: FanGraphs / FantasAI engine) ===\n"]

    for ctx in contexts:
        parts.append(f"--- PLAYER: {ctx.name} ---")
        parts.append(f"Team: {ctx.team} | Positions: {', '.join(ctx.positions)} | Type: {ctx.stat_type.title()}")

        if ctx.overall_rank is not None:
            parts.append(f"App Ranking: #{ctx.overall_rank} overall ({ctx.rank_list_name}), score: {ctx.rank_score}")
        else:
            parts.append("App Ranking: Not in current top-400 predictive list")

        ownership = f"Owned by {ctx.owned_by}" if ctx.owned_by else "Available — not owned in your league"
        parts.append(f"League Status: {ownership}")

        # Actual stats
        parts.append("")
        parts.append(_format_stats_block(ctx.actual_stats, ctx.stat_type, "2026 Season Stats (FanGraphs actuals)"))

        # Pace-adjusted season projection
        pace, sample_note = _pace_adjust(ctx.actual_stats, ctx.stat_type)
        if pace:
            scale_label = "per 600 PA" if ctx.stat_type == "batting" else "per 180 IP"
            pace_parts = [f"Season Pace ({scale_label}):"]
            for k, v in pace.items():
                pace_parts.append(f"  {k}: {v}")
            pace_parts.append(f"  Sample note: {sample_note}")
            parts.append("\n".join(pace_parts) + "\n")

        # Projections
        if ctx.projection_stats:
            parts.append(_format_stats_block(ctx.projection_stats, ctx.stat_type, "Steamer Rest-of-Season Projections"))
        else:
            parts.append("Steamer Rest-of-Season Projections: Not available\n")

        # PAV
        if ctx.is_prospect and ctx.pav_score is not None:
            parts.append(f"PAV (Prospect Adjusted Value) Score: {ctx.pav_score}/100")
            if ctx.pav_components:
                c = ctx.pav_components
                parts.append("  PAV Component Breakdown:")
                parts.append(f"    Prospect Grade: {c.prospect_grade}/100")
                parts.append(f"    Age-Adjusted Performance: {c.age_adj_performance}/100")
                parts.append(f"    Vertical Velocity: {c.vertical_velocity}/100")
                parts.append(f"    ETA Proximity: {c.eta_proximity}/100")
            parts.append("")

        # Injury / health context
        if ctx.injury:
            inj = ctx.injury
            inj_lines = ["Injury / Health Status:"]
            if inj.status:
                status_display = {
                    "il_10": "10-Day IL",
                    "il_60": "60-Day IL",
                    "day_to_day": "Day-to-Day (not on formal IL)",
                    "out_for_season": "Out for Season",
                }.get(inj.status, inj.status)
                inj_lines.append(f"  Current status: {status_display}")
            if inj.description:
                inj_lines.append(f"  Description: {inj.description}")
            if inj.expected_return:
                inj_lines.append(f"  Expected return: {inj.expected_return}")
            if inj.risk_flag:
                risk_display = {
                    "fragile": "Fragile — chronically injury-prone; availability discounted in projections",
                    "recent_surgery": "Post-major-surgery risk — availability discounted in projections",
                }.get(inj.risk_flag, inj.risk_flag)
                inj_lines.append(f"  Risk profile: {risk_display}")
                if inj.risk_note:
                    inj_lines.append(f"  Note: {inj.risk_note}")
            parts.append("\n".join(inj_lines) + "\n")

        # This week's schedule context
        if ctx.schedule:
            sc = ctx.schedule
            sched_lines = ["This Week Schedule:"]
            sched_lines.append(f"  Games this week: {sc.games_this_week}")
            if ctx.stat_type == "pitching" and "SP" in ctx.positions:
                sched_lines.append(f"  Probable starts: {sc.probable_starts} total, {sc.future_starts} remaining")
                if sc.opponent_teams:
                    sched_lines.append(f"  Opponents: {', '.join(sc.opponent_teams)}")
            if sc.today_opponent:
                home_away = "vs" if sc.today_is_home else "@"
                today_line = f"  Today: {home_away} {sc.today_opponent}"
                if sc.today_park_factor is not None and abs(sc.today_park_factor - 1.0) >= 0.05:
                    pct = int(round((sc.today_park_factor - 1.0) * 100))
                    direction = "+" if pct > 0 else ""
                    today_line += f" | Park: {sc.today_park} ({direction}{pct}% HR factor)"
                if sc.today_sp_name:
                    throws_str = f" ({sc.today_sp_throws})" if sc.today_sp_throws else ""
                    today_line += f" | Probable SP: {sc.today_sp_name}{throws_str}"
                sched_lines.append(today_line)
            # Handedness matchup note for batters
            if ctx.stat_type == "batting" and ctx.bats and sc.today_sp_throws:
                sched_lines.append(f"  Handedness: bats {ctx.bats} vs throws {sc.today_sp_throws}")
            if sc.vegas_run_factor != 1.0:
                implied = round(4.4 * sc.vegas_run_factor, 1)
                sched_lines.append(f"  Vegas run environment: {implied} R/G implied ({'+' if sc.vegas_run_factor > 1 else ''}{int(round((sc.vegas_run_factor-1)*100))}% vs avg)")
            if sc.weather_temp_f > 0 or sc.weather_wind_mph > 0:
                wx_parts = []
                if sc.weather_temp_f > 0:
                    wx_parts.append(f"{int(round(sc.weather_temp_f))}°F")
                if sc.weather_wind_mph >= 5:
                    wx_parts.append(f"{int(round(sc.weather_wind_mph))}mph wind")
                if wx_parts:
                    hr_delta = int(round((sc.weather_hr_factor - 1.0) * 100))
                    direction = "+" if hr_delta >= 0 else ""
                    sched_lines.append(f"  Weather: {', '.join(wx_parts)} ({direction}{hr_delta}% HR environment)")
            if sc.week_context_text:
                sched_lines.append(f"  Week note: {sc.week_context_text}")
            parts.append("\n".join(sched_lines) + "\n")

        # Handedness (for pitcher context — batter handedness in schedule block above)
        if ctx.stat_type == "pitching" and ctx.throws:
            parts.append(f"Pitcher handedness: throws {ctx.throws}\n")

        parts.append("")

    # Append horizon framing block when a time horizon was detected
    if horizon:
        parts.append(_format_horizon_block(horizon))

    parts.append("=== END PLAYER DATA ===")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# System prompt for Explore Players chat
# ---------------------------------------------------------------------------

def _build_system_prompt(player_names: list[str]) -> str:
    from fantasai.brain.writer_persona import SYSTEM_PROMPT as _WRITER_PERSONA

    extra = f"""

───────────────────────────────────────
EXPLORE PLAYERS — ANALYST CHAT MODE
───────────────────────────────────────
You are operating as an interactive analyst. The user is researching {'and '.join(player_names)}.

DATA BOUNDARY: You have access only to the player data provided in the USER CONTEXT BLOCK below. \
Do not reference any external information, news, injury reports, or facts not present in that context. \
Your training knowledge may be used for historical comps and cultural references only — never for current facts.

CITATION: When citing a stat or ranking, name the source explicitly \
(e.g. "per FanGraphs xwOBA" or "per our Predictive Rankings list").

STANCE: Take clear, confident positions when the data supports them. \
Avoid non-committal hedging like "it depends" or "could go either way" unless the data is genuinely ambiguous.

HONESTY: If a question requires data not in the provided context, say so clearly. \
Acknowledge the question, state specifically what data would be needed to answer it, \
and offer a related question you CAN answer from the available data.

MULTI-PLAYER: Discuss players independently by default. \
Only compare head-to-head when the user explicitly asks for a comparison.

RESPONSE LENGTH (HARD RULES — not guidelines, rules):
- Single-player question: ≤ 120 words. Count before sending.
- Multi-player comparison: ≤ 180 words.
- Start/sit or quick verdict: 1–2 sentences only. Lead with the answer first.
- Never restate the question. Never open with filler ("Great question", "Sure!", "Absolutely", etc.).
- Never end with a summary paragraph. Stop when the point is made.
- Bullet points only when listing 3 or more distinct items. Use prose otherwise.
- One tight paragraph is almost always the right format.

PACE STATS: When context includes "Season Pace" numbers, use them to frame what counting stats \
a player might finish with. If the sample note warns of an extreme small sample (< 20 PA / < 10 IP), \
acknowledge that the pace is for illustration only — do not cite it as a prediction.

HORIZON: If the context includes an ANALYSIS FRAME block, use the specified ranking blend and guidance \
to calibrate which signals to emphasise. Always name the horizon explicitly in your answer \
(e.g. "for ROS value…", "as a streamer this week…").

MATCHUP QUESTIONS: When the context includes a "This Week Schedule" block with today's opponent, \
probable SP, park factor, weather, or Vegas run environment — use ALL of it. For start/sit verdicts: \
(1) state the recommendation in the first sentence, (2) cite the 2–3 most relevant factors \
(park, SP handedness advantage, Vegas environment, weather), (3) one sentence on the risk. \
Never waffle — give a verdict.

INJURY QUESTIONS: When the context includes an "Injury / Health Status" block, \
lead with the current status and expected return, then frame the fantasy impact \
(how many games missed, what the projections assume about availability).

TRADE QUESTIONS: When evaluating a trade, address both sides explicitly. \
For each player: 3-year trajectory (age + Steamer), current health, schedule strength. \
Give a clear recommendation: accept, decline, or negotiate.
"""
    return _WRITER_PERSONA + extra


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/player/{player_id}", response_model=PlayerContextResponse)
def get_player_context(
    player_id: int,
    league_id: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
) -> PlayerContextResponse:
    """Fetch stat card context for a single player.

    Returns current-season actuals, Steamer projections, PAV breakdown (prospects),
    app ranking, and league ownership status.
    """
    owned_by_map = _build_owned_by_map(league_id, db)
    ctx = _get_player_context(player_id, db, owned_by_map)
    if ctx is None:
        raise HTTPException(status_code=404, detail=f"Player {player_id} not found")
    return ctx


@router.post("/chat")
async def explore_chat(
    body: ExploreChatRequest,
    db: Session = Depends(get_db),
) -> StreamingResponse:
    """Streaming analyst chat for selected players.

    Returns a text/event-stream SSE response. Events:
      data: {"type": "text", "text": "..."}   — incremental token
      data: {"type": "done"}                   — stream complete
      data: {"type": "error", "message": "..."} — error

    The client should keep the last 5 user+assistant turn pairs and send them
    in `messages`. Chat history resets when the player selection changes.
    """
    client = _async_llm_client()
    if not client:
        raise HTTPException(status_code=503, detail="LLM service not configured")

    if not body.player_ids:
        raise HTTPException(status_code=400, detail="At least one player_id is required")

    # Fetch all player contexts
    owned_by_map = _build_owned_by_map(body.league_id, db)
    contexts: list[PlayerContextResponse] = []
    for pid in body.player_ids[:5]:  # hard cap at 5
        ctx = _get_player_context(pid, db, owned_by_map)
        if ctx:
            contexts.append(ctx)

    if not contexts:
        raise HTTPException(status_code=404, detail="No valid players found")

    player_names = [c.name for c in contexts]
    horizon = _detect_horizon(body.user_message)
    system_prompt = _build_system_prompt(player_names)
    rag_context = _build_rag_context(contexts, horizon=horizon)

    # Build message list: [user_context_block (as first user msg), history, current message]
    # The RAG context is prepended to the very first user message in the thread.
    messages_for_api: list[dict] = []

    history = body.messages[-10:]  # last 5 turns = 10 messages (5u + 5a)

    if not history:
        # First turn: prepend RAG context to user message
        messages_for_api = [
            {
                "role": "user",
                "content": f"PLAYER CONTEXT:\n{rag_context}\n\n---\n\nMY QUESTION: {body.user_message}",
            }
        ]
    else:
        # Subsequent turns: re-inject RAG context as a preamble in the first message of history
        # to ensure the model always has full context, then append current message
        first_msg = history[0]
        if first_msg.role == "user" and not first_msg.content.startswith("PLAYER CONTEXT:"):
            history[0] = ChatMessage(
                role="user",
                content=f"PLAYER CONTEXT:\n{rag_context}\n\n---\n\n{first_msg.content}",
            )
        messages_for_api = [{"role": m.role, "content": m.content} for m in history]
        messages_for_api.append({"role": "user", "content": body.user_message})

    async def generate():
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=messages_for_api,
            ) as stream:
                async for text in stream.text_stream:
                    yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
        except Exception as exc:
            logger.error("explore_chat stream error: %s", exc, exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': 'Analyst encountered an error. Please try again.'})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering for SSE
        },
    )
