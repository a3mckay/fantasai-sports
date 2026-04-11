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
    PavComponents,
    PlayerContextResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/explore", tags=["explore"])

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

    return PlayerContextResponse(
        player_id=player_id,
        name=player.name,
        team=player.team or "—",
        positions=positions,
        stat_type=stat_type,
        mlbam_id=player.mlbam_id,
        actual_stats=actual_stats,
        projection_stats=projection_stats,
        overall_rank=overall_rank,
        rank_score=rank_score,
        rank_list_name=_RANK_LIST_NAME,
        is_prospect=is_prospect,
        pav_score=pav_score,
        pav_components=pav_components,
        owned_by=owned_by_map.get(player_id),
    )


def _build_owned_by_map(league_id: Optional[int], db: Session) -> dict[int, str]:
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


def _build_rag_context(contexts: list[PlayerContextResponse]) -> str:
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

        parts.append("")

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

RESPONSE LENGTH: Match length to question complexity. \
A quick "who should I start?" deserves 2-3 sentences. A deep dynasty breakdown warrants more. \
Never pad. Never repeat information already given in this conversation.
"""
    return _WRITER_PERSONA + extra


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/player/{player_id}", response_model=PlayerContextResponse)
def get_player_context(
    player_id: int,
    league_id: Optional[int] = Query(default=None),
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
    system_prompt = _build_system_prompt(player_names)
    rag_context = _build_rag_context(contexts)

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
