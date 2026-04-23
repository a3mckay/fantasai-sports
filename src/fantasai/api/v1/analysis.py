"""Analysis API endpoints.

Eight features:
  1. POST /compare        — head-to-head player comparison with optional context
  2. POST /trade          — trade evaluation with talent-density-aware verdict
  3. POST /find-player    — suggest an available player for a specific roster slot
                            with persistent history so repeat calls avoid duplicates.
  4. POST /team-eval      — holistic team evaluation: letter grade, position
                            breakdown, category strengths/gaps, improvement tips.
  5. POST /keeper-eval    — keeper/dynasty planning: evaluate an existing keeper
                            core or recommend who to keep from a full roster.
  6. POST /compare-teams  — head-to-head comparison of 2–6 teams with trade
                            opportunity surfacing.
  7. GET  /league-power/{league_id} — full league power rankings, tier groupings,
                            and top cross-league trade pairs.
  8. POST /extract-players — extract player names from a screenshot using Claude vision.

All endpoints compute fresh rankings from stored PlayerStats and call the
brain layer for algorithmic analysis. LLM blurbs are generated via direct
Anthropic API calls and never block the response if they fail.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from fantasai.api.deps import check_rate_limit, get_db
from fantasai.brain.comparator import CompareContext, compare_players
from fantasai.brain.lineup import apply_weights, build_roster_notes, compute_roster_weights
from fantasai.brain.league_analyzer import (
    LeaguePowerReport,
    TeamsComparison,
    compare_teams,
    compute_league_power,
)
from fantasai.brain.recommender import (
    BuildPreferences,
    Recommender,
    WaiverContext,
    _compute_team_strengths,
    _player_eligible_for_slot,
)
from fantasai.brain.team_evaluator import (
    KeeperEvaluation,
    TeamEvaluation,
    evaluate_keepers,
    evaluate_team,
    plan_keepers,
)
from fantasai.brain.trade_evaluator import (
    TradeContext,
    TradeEvaluation,
    _adjusted_side_value,
    _parse_pros_cons,
    evaluate_trade,
)
from fantasai.brain.trade_builder import (
    TradeBuildContext,
    TradeSuggestion as _TradeSuggestion,
    build_trades,
)
from fantasai.config import settings
from fantasai.engine.projection import ProjectionHorizon
from fantasai.engine.scoring import PlayerRanking
from fantasai.models.league import Team
from fantasai.models.player import Player
from fantasai.models.prospect import ProspectProfile
from fantasai.models.recommendation import Recommendation
from fantasai.schemas.analysis import (
    ComparePlayerResultRead,
    CompareRequest,
    CompareResponse,
    ExtractPlayersRequest,
    ExtractPlayersResponse,
    FindPlayerRequest,
    FindPlayerResponse,
    FindPlayerSuggestionRead,
    TradeBuildRequest,
    TradeBuildResponse,
    TradeSuggestionRead,
    TradeRequest,
    TradeResponse,
)
from fantasai.schemas.team_analysis import (
    CompareTeamsRequest,
    CompareTeamsResponse,
    DraftProfileRead,
    KeeperEvalRequest,
    KeeperEvalResponse,
    LeaguePowerResponse,
    ManualTeam,  # noqa: F401 — re-exported for consumers
    PlayerSummaryRead,
    PositionGroupRead,
    TeamEvalRequest,
    TeamEvalResponse,
    TeamSnapshotRead,
    TradeOpportunityRead,
)

# Reuse the shared rankings helpers from the recommendations module
from fantasai.api.v1.rankings import RANKINGS_DEFAULT_CATEGORIES as DEFAULT_CATEGORIES
from fantasai.api.v1.recommendations import (
    _compute_projection_rankings,
    _compute_rankings,
    _fetch_raw_stats_map,
    _fetch_team_and_league,
    _get_cached_raw_rankings,
)
from fantasai.brain.writer_persona import SYSTEM_PROMPT as _WRITER_PERSONA

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])

# Default scoring categories — must match RANKINGS_DEFAULT_CATEGORIES in
# rankings.py exactly so that overall_rank values are consistent across
# the Rankings page and every analysis endpoint (compare, trade, etc.).

# System prompt for analysis-type LLM calls (compare, trade verdict, team eval, etc.).
# Shares the writer persona voice — same Canadian easter eggs, pop culture refs,
# signature phrases, and opinionated tone. Extended with analysis-specific rules.
_ANALYSIS_SYSTEM_PROMPT = (
    _WRITER_PERSONA
    + "\n\n"
    "ANALYSIS WRITING RULES (applies to all team/league analysis — NOT player blurbs):\n"
    "• You are writing team or league analysis, not a player blurb. Same voice, "
    "same persona. Broader lens.\n"
    "• HARD RULE — PERSONALITY MINIMUM: MINIMUM TWO personality elements are required "
    "in every piece of analysis. You must include AT LEAST ONE analogy or cultural "
    "reference (baseball culture, pop culture, or Canadian reference) AND AT LEAST ONE "
    "signature phrase or irreverent observation the writer actually holds. Generic stat "
    "recitation with zero voice is an automatic failure. If you cannot find a natural fit, "
    "force one — the voice is non-negotiable.\n"
    "• BANNED phrases — do not use under any circumstances:\n"
    "  - 'not particularly close' / 'it's not particularly close'\n"
    "  - 'comfortable margin'\n"
    "  - 'clear advantage' (say WHY the advantage exists instead)\n"
    "  - 'significant gap'\n"
    "  - 'dominant performance'\n"
    "  - 'commanding lead'\n"
    "  These phrases describe gaps without explaining them. Replace with specifics: "
    "which categories, which players, what the edge actually means for the matchup.\n"
    "• Plain prose only. No markdown, no headers, no bullets. Just clean sentences.\n"
    "• Canadian references, approved pop culture, signature phrases — all available. "
    "One per piece, only when they fit naturally."
)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------


def _llm_client():
    """Return an Anthropic client or None if no API key is configured."""
    if not settings.anthropic_api_key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=settings.anthropic_api_key)
    except Exception as exc:
        logger.warning("Could not create Anthropic client: %s", exc)
        return None


def _generate_compare_blurb(
    ranked_players: list,  # list[ComparePlayerResult]
    categories: list[str],
    context: Optional[str],
    raw_stats_map: Optional[dict[int, dict]] = None,  # player_id → {stat: value}
    player_context_blocks: Optional[dict[int, dict]] = None,  # player_id → {stats, injury, workload}
) -> str:
    """Generate a comparison blurb via Anthropic API.

    Uses the shared writer persona. The data block exposes real stats and
    overall rank percentile — no internal z-scores or "adjusted score" jargon.

    player_context_blocks: enriched context from brain.player_context — includes
    rate stats (K/9, xFIP, xwOBA), injury notes, and pitcher workload framing.

    Returns empty string on failure — never raises.
    """
    client = _llm_client()
    if not client:
        return ""

    # Category → human label for the prompt
    _CAT_LABELS = {
        "R": "Runs", "HR": "HR", "RBI": "RBI", "SB": "SB", "AVG": "AVG",
        "OPS": "OPS", "OBP": "OBP", "W": "Wins", "SV": "Saves", "K": "K",
        "ERA": "ERA", "WHIP": "WHIP", "HLD": "Holds", "IP": "IP",
    }

    try:
        lines = ["━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━"]
        if context:
            lines.append(f"User context: {context}")
        lines.append(f"League scoring categories: {', '.join(categories)}")
        lines.append("")

        for p in ranked_players:
            header = (
                f"#{p.rank} in this matchup  |  {p.player_name} "
                f"({'/'.join(p.positions)}, {p.team})  |  "
                f"Overall rank: #{p.overall_rank} of {p.total_players} ranked players"
            )
            lines.append(header)

            # Enriched stats block (rate stats + advanced metrics + data source label)
            pctx = (player_context_blocks or {}).get(p.player_id, {})
            stats_block = pctx.get("stats", "")
            if stats_block:
                lines.append(f"  Stats:\n{stats_block}")
            else:
                # Fallback: category-level stats from raw_stats_map
                raw = (raw_stats_map or {}).get(p.player_id, {})
                if raw:
                    stat_line = "  Stats: " + "  |  ".join(
                        f"{_CAT_LABELS.get(k, k)} {v}"
                        for k, v in raw.items()
                        if v is not None
                    )
                    lines.append(stat_line)

            # Injury context
            injury_note = pctx.get("injury", "")
            if injury_note:
                lines.append(f"  Injury/risk: {injury_note}")

            # Pitcher workload note
            workload_note = pctx.get("workload", "")
            if workload_note:
                lines.append(f"  {workload_note}")

            # Tier signals — readable words, NOT z-scores
            _TIERS = [(2.0, "elite"), (1.0, "strong"), (0.3, "average"),
                      (-0.3, "below average"), (-1.0, "weak"), (float("-inf"), "drag")]

            def _tier(z: float) -> str:
                for threshold, label in _TIERS:
                    if z >= threshold:
                        return label
                return "drag"

            relevant_cats = [c for c in categories if c in p.category_scores]
            tier_parts = [
                f"{_CAT_LABELS.get(c, c)}: {_tier(p.category_scores[c])}"
                for c in sorted(relevant_cats, key=lambda c: -abs(p.category_scores.get(c, 0)))[:5]
            ]
            if tier_parts:
                lines.append("  Category tiers: " + ", ".join(tier_parts))
            lines.append("")

        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")
        lines.append(
            "This is a head-to-head fantasy baseball player comparison. "
            "Write 3–4 sentences in your voice. "
            "State clearly who wins and why, citing actual stats and advanced metrics from the DATA BLOCK. "
            "Use the data source labels — say 'projects for' when citing Steamer projections, "
            "'has posted' or 'is hitting' when citing 2026 actuals. "
            "If a player is injured or has a workload limit, factor that into your verdict. "
            "Reference real numbers (AVG, ERA, xwOBA, K/9, etc.) not internal scores. "
            "If the result is close, say so honestly."
            + (f" Address the user's context: {context}" if context else "")
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
            system=[
                {
                    "type": "text",
                    "text": _ANALYSIS_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        return text_blocks[0].text.strip() if text_blocks else ""
    except Exception as exc:
        logger.warning("Compare blurb generation failed: %s", exc)
        return ""


def _generate_trade_blurb_and_pros_cons(
    evaluation: TradeEvaluation,
    giving_rankings: list[PlayerRanking],
    receiving_rankings: list[PlayerRanking],
    giving_picks: list[str],
    receiving_picks: list[str],
    categories: list[str],
    has_keepers: bool,
    context: Optional[str],
    player_context_blocks: Optional[dict[int, dict]] = None,  # player_id → {stats, injury, workload}
) -> tuple[str, list[str], list[str]]:
    """Generate a trade verdict blurb with [PROS]/[CONS] via Anthropic API.

    Returns (blurb, pros, cons). Falls back to empty strings / algorithmic
    pros/cons on failure.
    """
    client = _llm_client()
    if not client:
        return "", evaluation.pros, evaluation.cons

    try:
        pctx = player_context_blocks or {}

        def _side_summary(players: list[PlayerRanking], picks: list[str]) -> str:
            parts = []
            for p in players:
                top_cats = ", ".join(
                    f"{cat}: {p.category_contributions.get(cat, 0):+.1f}"
                    for cat in sorted(categories, key=lambda c: -abs(p.category_contributions.get(c, 0)))[:4]
                )
                parts.append(f"  {p.name} ({'/'.join(p.positions)}, rank #{getattr(p, 'overall_rank', '?')}) | {top_cats}")
                # Append enriched context for this player
                ctx_block = pctx.get(p.player_id, {})
                if ctx_block.get("stats"):
                    parts.append(f"    Stats:\n    {ctx_block['stats']}")
                if ctx_block.get("injury"):
                    parts.append(f"    Injury/risk: {ctx_block['injury']}")
                if ctx_block.get("workload"):
                    parts.append(f"    {ctx_block['workload']}")
            for pick in picks:
                parts.append(f"  {pick} (draft pick)")
            return "\n".join(parts) if parts else "  (none)"

        lines = [
            "━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━",
            f"Verdict: {evaluation.verdict.upper()} | "
            f"Confidence: {evaluation.confidence:.0%} | "
            f"Value differential: {evaluation.value_differential:+.2f} (density-adjusted)",
            f"Talent density: {evaluation.talent_density_note}",
            f"Scoring categories: {', '.join(categories)}",
            "",
            "GIVING AWAY:",
            _side_summary(giving_rankings, giving_picks),
            "",
            "RECEIVING:",
            _side_summary(receiving_rankings, receiving_picks),
            "",
            "Category impact (positive = improves after trade):",
        ]
        for cat, delta in sorted(evaluation.category_impact.items(), key=lambda x: -abs(x[1])):
            lines.append(f"  {cat}: {delta:+.2f}")

        if has_keepers:
            lines.append("")
            lines.append("Note: Keeper league — weight future value and player age.")
        if context:
            lines.append(f"User context: {context}")

        lines += [
            "━━━ END DATA BLOCK ━━━",
            "",
            "Write a verdict blurb (3–5 sentences) followed by structured pros and cons.",
            "Use data source labels — 'projects for' when citing Steamer projections, "
            "'has posted' or 'is hitting' when citing 2026 actuals. "
            "If a player is injured or has a workload limit, factor that into the verdict. "
            "Cite specific rate stats (xFIP, xwOBA, K/9, AVG) from the DATA BLOCK — not internal scores.",
            "Format exactly as:",
            "",
            "BLURB: <your 3–5 sentence verdict here>",
            "",
            "[PROS]",
            "- Pro point one",
            "- Pro point two",
            "",
            "[CONS]",
            "- Con point one",
        ]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=[
                {
                    "type": "text",
                    "text": _ANALYSIS_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        if not text_blocks:
            return "", evaluation.pros, evaluation.cons

        raw = text_blocks[0].text.strip()

        # Extract blurb text (before [PROS] block)
        blurb = raw
        import re
        blurb_match = re.search(r"BLURB:\s*(.*?)(?:\[PROS\]|\Z)", raw, re.DOTALL | re.IGNORECASE)
        if blurb_match:
            blurb = blurb_match.group(1).strip()

        pros, cons = _parse_pros_cons(raw)

        # Fall back to algorithmic if LLM produced empty lists
        if not pros:
            pros = evaluation.pros
        if not cons:
            cons = evaluation.cons

        return blurb, pros, cons

    except Exception as exc:
        logger.warning("Trade blurb generation failed: %s", exc)
        return "", evaluation.pros, evaluation.cons


def _generate_find_player_blurb(
    ranking: PlayerRanking,
    categories: list[str],
    position_slot: str,
    db: Optional[Session] = None,
) -> str:
    """Generate a 'why now' framing blurb for a find-player suggestion.

    The blurb is framed around the specific roster slot being filled so the
    LLM can explain *why this player fits that need* rather than writing a
    generic positional preview.
    """
    from fantasai.brain.blurb_generator import get_blurb_generator

    if not settings.anthropic_api_key:
        return ""

    try:
        gen = get_blurb_generator(api_key=settings.anthropic_api_key)
        # Fetch season stats to ground the blurb — prevents hallucinating
        # counts (HR, RBI, etc.) that don't appear in the data block.
        raw_stats: Optional[dict] = None
        if db is not None:
            stats_map = _fetch_raw_stats_map(db, [ranking.player_id])
            raw_stats = stats_map.get(ranking.player_id)

        # Build roster context so the model frames the recommendation around
        # the specific slot (e.g. "Filling roster slot: SP — frame the blurb
        # around why this pitcher fills a starting-pitching need").
        roster_ctx: Optional[str] = None
        if position_slot:
            roster_ctx = (
                f"Filling roster slot: {position_slot} — frame the blurb around "
                f"why this player fills the manager's {position_slot} need, "
                f"not just as a generic waiver pickup."
            )

        return gen.generate_blurb(
            ranking=ranking,
            ranking_type="predictive_season",
            scoring_categories=categories,
            raw_stats=raw_stats,
            roster_context=roster_ctx,
        )
    except Exception as exc:
        logger.warning("Find-player blurb generation failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Endpoint 1 — Compare Players
# ---------------------------------------------------------------------------


@router.post(
    "/compare",
    response_model=CompareResponse,
    summary="Compare 2+ players head-to-head with optional user context",
)
def compare_players_endpoint(
    body: CompareRequest,
    db: Session = Depends(get_db),
    _limit: None = Depends(check_rate_limit("compare")),
) -> CompareResponse:
    """Rank two or more players against each other.

    Optionally accepts a ``context`` string (e.g. "I need stolen bases") to
    re-weight categories the user cares about. Returns an LLM-generated
    analysis blurb alongside the ranked player list.
    """
    # Determine scoring categories
    categories = DEFAULT_CATEGORIES
    if body.league_id:
        from fantasai.models.league import League
        league = db.get(League, body.league_id)
        if league and league.scoring_categories:
            categories = league.scoring_categories
    if body.custom_categories:
        categories = body.custom_categories

    proj_horizon = ProjectionHorizon(body.horizon) if body.ranking_type == "predictive" else ProjectionHorizon.SEASON
    lookback, predictive = _compute_rankings(db, categories, horizon=proj_horizon)
    if not lookback and not predictive:
        raise HTTPException(
            status_code=404,
            detail="No player stats available for rankings.",
        )

    source = predictive if body.ranking_type == "predictive" else lookback
    ranking_map = {r.player_id: r for r in source}

    # Look up each requested player
    found: list[PlayerRanking] = []
    missing: list[int] = []
    for pid in body.player_ids:
        r = ranking_map.get(pid)
        if r:
            found.append(r)
        else:
            missing.append(pid)

    if missing:
        logger.warning("Compare: player_ids not found in rankings: %s", missing)

    if len(found) < 2:
        raise HTTPException(
            status_code=404,
            detail=f"Need at least 2 ranked players to compare. Found {len(found)} of {len(body.player_ids)} requested.",
        )

    ctx = CompareContext(
        player_rankings=found,
        scoring_categories=categories,
        context=body.context,
        ranking_type=body.ranking_type,
    )

    results = compare_players(ctx)

    # Patch total_players onto each result using the full source pool size
    total_ranked = len(source)
    for r in results:
        r.total_players = total_ranked

    # Build enriched player context blocks (rate stats, injury, workload)
    from fantasai.brain.player_context import (
        build_player_stats_block,
        build_player_injury_note,
        build_pitcher_workload_note,
    )
    result_stat_type = {r.player_id: r.stat_type for r in results}
    player_context_blocks: dict[int, dict] = {}
    for pid in body.player_ids:
        stype = result_stat_type.get(pid)
        player_context_blocks[pid] = {
            "stats":    build_player_stats_block(pid, db, stat_type=stype),
            "injury":   build_player_injury_note(pid, db),
            "workload": build_pitcher_workload_note(pid, db),
        }

    # Determine which categories were boosted by context (for transparency)
    context_applied: Optional[str] = None
    if body.context:
        from fantasai.brain.comparator import _parse_context_keywords
        boosted = _parse_context_keywords(body.context, categories)
        if boosted:
            context_applied = f"Boosted categories: {', '.join(sorted(boosted))}"

    # Generate LLM blurb with enriched context
    blurb = _generate_compare_blurb(
        results, categories, body.context,
        player_context_blocks=player_context_blocks,
    )

    return CompareResponse(
        ranked_players=[
            ComparePlayerResultRead(
                player_id=r.player_id,
                player_name=r.player_name,
                team=r.team,
                positions=r.positions,
                rank=r.rank,
                composite_score=r.composite_score,
                category_scores=r.category_scores,
                stat_type=r.stat_type,
                overall_rank=r.overall_rank,
            )
            for r in results
        ],
        analysis_blurb=blurb,
        context_applied=context_applied,
    )


# ---------------------------------------------------------------------------
# Endpoint 2 — Evaluate Trade
# ---------------------------------------------------------------------------


@router.post(
    "/trade",
    response_model=TradeResponse,
    summary="Evaluate a trade proposal with talent-density-aware scoring",
)
def evaluate_trade_endpoint(
    body: TradeRequest,
    db: Session = Depends(get_db),
    _limit: None = Depends(check_rate_limit("trade")),
) -> TradeResponse:
    """Assess whether a trade proposal is fair, favors receiving, or favors giving.

    Uses talent-density adjustment so that trading one elite player for
    multiple average players is penalized even if raw totals are equal.
    In keeper leagues (detected from league settings), younger players
    receive a future-value bonus when age data is available.
    """
    # Resolve categories, league_type, and has_keepers
    if body.team_id:
        team, league = _fetch_team_and_league(body.team_id, db)
        categories = league.scoring_categories or DEFAULT_CATEGORIES
        league_type = league.league_type or "h2h_categories"
        has_keepers = (league.settings or {}).get("keepers", 0) > 0
        roster_ids: set[int] = set(team.roster or [])
    else:
        team = None
        league = None
        categories = body.custom_categories or DEFAULT_CATEGORIES
        league_type = body.custom_league_type or "h2h_categories"
        has_keepers = False
        roster_ids = set(body.roster_player_ids or [])

    # Override categories with custom if provided alongside team_id
    if body.custom_categories:
        categories = body.custom_categories
    if body.custom_league_type:
        league_type = body.custom_league_type

    trade_horizon = ProjectionHorizon(body.horizon)
    lookback, predictive = _compute_rankings(db, categories, horizon=trade_horizon)
    if not lookback:
        raise HTTPException(
            status_code=404,
            detail="No player stats available for trade evaluation.",
        )

    # Use predictive rankings for trade evaluation (forward-looking is more useful)
    ranking_map = {r.player_id: r for r in predictive} if predictive else {}
    if not ranking_map:
        ranking_map = {r.player_id: r for r in lookback}

    def _lookup_rankings(player_ids: list[int]) -> list[PlayerRanking]:
        found = []
        for pid in player_ids:
            r = ranking_map.get(pid)
            if r:
                found.append(r)
            else:
                logger.warning("Trade: player_id %d not found in rankings", pid)
        return found

    giving_rankings = _lookup_rankings(body.giving.player_ids)
    receiving_rankings = _lookup_rankings(body.receiving.player_ids)

    total_players = len(body.giving.player_ids) + len(body.receiving.player_ids)
    if total_players == 0 and not body.giving.draft_picks and not body.receiving.draft_picks:
        raise HTTPException(
            status_code=422,
            detail="Trade must include at least one player or draft pick on each side.",
        )

    # Compute team strengths for context
    roster_rankings = [r for r in lookback if r.player_id in roster_ids]
    team_strengths = _compute_team_strengths(roster_rankings, categories)

    ctx = TradeContext(
        giving_rankings=giving_rankings,
        receiving_rankings=receiving_rankings,
        giving_picks=body.giving.draft_picks,
        receiving_picks=body.receiving.draft_picks,
        team_strengths=team_strengths,
        scoring_categories=categories,
        league_type=league_type,
        has_keepers=has_keepers,
        context=body.context,
        player_ages={},  # Future: populate from Player.birth_date when available
    )

    evaluation = evaluate_trade(ctx)

    # Generate LLM blurb and refine pros/cons
    # Build enriched player context blocks for all trade participants
    from fantasai.brain.player_context import (
        build_player_stats_block,
        build_player_injury_note,
        build_pitcher_workload_note,
    )
    all_trade_player_ids = body.giving.player_ids + body.receiving.player_ids
    trade_context_blocks: dict[int, dict] = {}
    for r in giving_rankings + receiving_rankings:
        trade_context_blocks[r.player_id] = {
            "stats":    build_player_stats_block(r.player_id, db, stat_type=r.stat_type),
            "injury":   build_player_injury_note(r.player_id, db),
            "workload": build_pitcher_workload_note(r.player_id, db, overall_rank=r.overall_rank),
        }

    blurb, pros, cons = _generate_trade_blurb_and_pros_cons(
        evaluation=evaluation,
        giving_rankings=giving_rankings,
        receiving_rankings=receiving_rankings,
        giving_picks=body.giving.draft_picks,
        receiving_picks=body.receiving.draft_picks,
        categories=categories,
        has_keepers=has_keepers,
        context=body.context,
        player_context_blocks=trade_context_blocks,
    )

    return TradeResponse(
        verdict=evaluation.verdict,
        confidence=evaluation.confidence,
        value_differential=evaluation.value_differential,
        raw_value_differential=evaluation.raw_value_differential,
        talent_density_note=evaluation.talent_density_note,
        category_impact=evaluation.category_impact,
        give_value=evaluation.give_value,
        receive_value=evaluation.receive_value,
        pros=pros or evaluation.pros,
        cons=cons or evaluation.cons,
        analysis_blurb=blurb,
    )


# ---------------------------------------------------------------------------
# Endpoint 2b — Build Trade
# ---------------------------------------------------------------------------


def _generate_build_trade_fit_notes(
    suggestions: list[_TradeSuggestion],
    target_names: list[str],
    ranking_map: dict[int, PlayerRanking],
    context: Optional[str],
) -> list[str]:
    """Generate fit notes for all suggestions in one batched LLM call.

    Returns a list of note strings in the same order as suggestions.
    Falls back to empty strings on failure.
    """
    client = _llm_client()
    if not client or not suggestions:
        return [""] * len(suggestions)

    try:
        receive_label = " + ".join(target_names) if target_names else "target player(s)"
        lines: list[str] = [
            "━━━ BUILD TRADE FIT NOTES ━━━",
            f"The manager wants to receive: {receive_label}",
        ]
        if context:
            lines.append(f"Context from the other manager: {context}")
        lines.append("")

        for i, s in enumerate(suggestions, 1):
            give_names = [
                ranking_map[pid].name if pid in ranking_map else f"Player #{pid}"
                for pid in s.give_player_ids
            ]
            recv_names = list(target_names)
            give_str = " + ".join(give_names + s.give_picks) or "(none)"
            recv_str = " + ".join(recv_names + s.receive_picks) or "(none)"
            diff_sign = "+" if s.value_differential >= 0 else ""
            lines += [
                f"[SUGGESTION_{i}]",
                f"Label: {s.label}",
                f"You give: {give_str}",
                f"You receive: {recv_str}",
                f"Value differential: {diff_sign}{s.value_differential:.2f} "
                f"(positive = you get more value)",
                f"Respects other team's roster needs: {s.respects_roster_needs}",
            ]
            if s.positional_warnings:
                lines.append(f"Positional note: {'; '.join(s.positional_warnings)}")
            lines.append("")

        lines += [
            "━━━ END DATA ━━━",
            "",
            "For each suggestion write exactly 2-3 sentences explaining why this trade "
            "works for BOTH sides — what the offering manager gets (the target player) "
            "and what makes this package appealing to the other manager. "
            "Be specific to the players and context above. "
            "Format exactly as:",
            "",
            "[NOTE_1]",
            "<2-3 sentence fit note>",
            "",
            "[NOTE_2]",
            "<2-3 sentence fit note>",
            "... (one block per suggestion)",
        ]

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            system=[
                {
                    "type": "text",
                    "text": _ANALYSIS_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        if not text_blocks:
            return [""] * len(suggestions)

        raw = text_blocks[0].text.strip()

        import re
        notes: list[str] = []
        for i in range(1, len(suggestions) + 1):
            pattern = rf"\[NOTE_{i}\](.*?)(?:\[NOTE_{i+1}\]|\Z)"
            m = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
            notes.append(m.group(1).strip() if m else "")
        return notes

    except Exception as exc:
        logger.warning("Build trade fit notes failed: %s", exc)
        return [""] * len(suggestions)


@router.post(
    "/trade/build",
    response_model=TradeBuildResponse,
    summary="Generate fair trade proposals for a target player",
)
def build_trade_endpoint(
    body: TradeBuildRequest,
    db: Session = Depends(get_db),
    _limit: None = Depends(check_rate_limit("trade")),
) -> TradeBuildResponse:
    """Generate 3-5 trade packages from your roster that are fair value for
    the target player(s).

    Uses the same talent-density-adjusted scoring as Evaluate Trade. A
    value_tolerance slider (-1.0 to +1.0) controls whether proposals lean
    toward fair-or-better or allow overpaying. Draft picks (rounds 1-13 only)
    are included in some packages to bridge value gaps, always in equal counts
    on each side.
    """
    # Resolve categories and league context
    if body.my_team_id:
        my_team, league = _fetch_team_and_league(body.my_team_id, db)
        categories = league.scoring_categories or DEFAULT_CATEGORIES
        league_type = league.league_type or "h2h_categories"
        my_roster_ids: set[int] = set(my_team.roster or [])
    else:
        my_team = None
        league = None
        categories = body.custom_categories or DEFAULT_CATEGORIES
        league_type = body.custom_league_type or "h2h_categories"
        my_roster_ids = set(body.my_roster_player_ids or [])

    if body.custom_categories:
        categories = body.custom_categories
    if body.custom_league_type:
        league_type = body.custom_league_type

    trade_horizon = ProjectionHorizon(body.horizon)
    lookback, predictive = _compute_rankings(db, categories, horizon=trade_horizon)
    if not lookback:
        raise HTTPException(status_code=404, detail="No player stats available.")

    ranking_map = {r.player_id: r for r in predictive} if predictive else {}
    if not ranking_map:
        ranking_map = {r.player_id: r for r in lookback}

    def _lookup(pids: list[int]) -> list[PlayerRanking]:
        found = []
        for pid in pids:
            r = ranking_map.get(pid)
            if r:
                found.append(r)
            else:
                logger.warning("build_trade: player_id %d not in rankings", pid)
        return found

    # Resolve rosters
    if not my_roster_ids and my_team is None:
        raise HTTPException(
            status_code=422,
            detail="Provide my_team_id or my_roster_player_ids.",
        )

    target_rankings = _lookup(body.target_player_ids)
    if not target_rankings:
        raise HTTPException(
            status_code=404,
            detail="None of the target players were found in current rankings.",
        )

    my_rankings = [r for r in ranking_map.values() if r.player_id in my_roster_ids]

    # Their roster for need-fit scoring
    their_roster_ids: set[int] = set()
    if body.their_team_id:
        try:
            their_team, _ = _fetch_team_and_league(body.their_team_id, db)
            their_roster_ids = set(their_team.roster or [])
        except Exception:
            pass
    elif body.their_roster_player_ids:
        their_roster_ids = set(body.their_roster_player_ids)

    their_rankings = [r for r in ranking_map.values() if r.player_id in their_roster_ids]

    # Build roster position map for positional warnings
    my_roster_positions = {
        r.player_id: r.positions for r in my_rankings
    }

    build_ctx = TradeBuildContext(
        my_rankings=my_rankings,
        their_rankings=their_rankings,
        target_rankings=target_rankings,
        context=body.context,
        value_tolerance=body.value_tolerance,
        scoring_categories=categories,
        league_type=league_type,
        my_roster_positions=my_roster_positions,
    )

    suggestions = build_trades(build_ctx)

    # Compute total candidates evaluated (approximate — expose for transparency)
    from math import comb as _comb
    n_avail = max(len(my_rankings) - len(target_rankings), 0)
    n_pick_pairs = 11  # len(_PICK_PAIRS)
    candidates_approx = sum(
        _comb(n_avail, k) * n_pick_pairs
        for k in range(1, 4)
        if n_avail >= k
    )

    # Generate LLM fit notes in one batch call
    target_names = [r.name for r in target_rankings]
    fit_notes = _generate_build_trade_fit_notes(
        suggestions, target_names, ranking_map, body.context
    )

    # Compute target value for transparency
    target_value = _adjusted_side_value([r.score for r in target_rankings])

    return TradeBuildResponse(
        suggestions=[
            TradeSuggestionRead(
                label=s.label,
                give_player_ids=s.give_player_ids,
                give_picks=s.give_picks,
                receive_player_ids=s.receive_player_ids,
                receive_picks=s.receive_picks,
                give_value=s.give_value,
                receive_value=s.receive_value,
                value_differential=s.value_differential,
                fairness_score=s.fairness_score,
                positional_warnings=s.positional_warnings,
                respects_roster_needs=s.respects_roster_needs,
                fit_note=fit_notes[i] if i < len(fit_notes) else "",
            )
            for i, s in enumerate(suggestions)
        ],
        target_value=round(target_value, 3),
        candidates_evaluated=candidates_approx,
    )


# ---------------------------------------------------------------------------
# Endpoint 3 — Find Me a Player
# ---------------------------------------------------------------------------


@router.post(
    "/find-player",
    response_model=FindPlayerResponse,
    summary="Suggest an available player for a specific roster slot",
)
def find_player_endpoint(
    body: FindPlayerRequest,
    db: Session = Depends(get_db),
    _limit: None = Depends(check_rate_limit("find-player")),
) -> FindPlayerResponse:
    """Find the best available player for a given roster slot and/or priority categories.

    Tracks suggestion history for this team so repeat calls always return a
    fresh (previously unseen) suggestion.  History older than 1 day is
    auto-purged.  Pass ``extra_exclude_ids`` to manually exclude players.

    player_pool:
      - "mlb"   — standard recommender flow (default)
      - "milb"  — returns top unseen prospect sorted by PAV score
      - "both"  — MLB suggestion + top MiLB prospect in milb_suggestion
    """
    from datetime import timedelta

    team, league = _fetch_team_and_league(body.team_id, db)
    categories = league.scoring_categories or DEFAULT_CATEGORIES

    # ── Auto-purge history older than 1 day ───────────────────────────────────
    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    db.query(Recommendation).filter(
        Recommendation.team_id == body.team_id,
        Recommendation.rec_type.like("find_player_%"),
        Recommendation.created_at < cutoff,
    ).delete(synchronize_session=False)
    db.commit()

    # ── Human-readable label for the search parameters ────────────────────────
    label_parts = [p for p in [body.position_slot] + list(body.priority_categories) if p]
    search_params_label = " + ".join(label_parts) if label_parts else "Best Available"

    # ── Build rostered + seen exclusion set ──────────────────────────────────
    all_rostered: set[int] = set()
    for t in league.teams:
        all_rostered.update(t.roster or [])

    seen_mlb: set[int] = {
        r.player_id for r in db.query(Recommendation).filter(
            Recommendation.team_id == body.team_id,
            Recommendation.rec_type.like("find_player_mlb%"),
        ).all()
    }
    seen_milb: set[int] = {
        r.player_id for r in db.query(Recommendation).filter(
            Recommendation.team_id == body.team_id,
            Recommendation.rec_type == "find_player_milb",
        ).all()
    }

    all_excluded_mlb  = all_rostered | seen_mlb  | set(body.extra_exclude_ids)
    all_excluded_milb = all_rostered | seen_milb | set(body.extra_exclude_ids)

    # ── MiLB prospect helper ──────────────────────────────────────────────────
    def _get_top_prospect(slot: Optional[str]) -> Optional[FindPlayerSuggestionRead]:
        """Return the highest-PAV unseen prospect that fits slot (if given)."""
        rows = (
            db.query(ProspectProfile)
            .filter(ProspectProfile.player_id.notin_(all_excluded_milb))
            .filter(ProspectProfile.pav_score.isnot(None))
            .order_by(ProspectProfile.pav_score.desc())
            .limit(100)
            .all()
        )
        for pp in rows:
            player = db.get(Player, pp.player_id)
            if not player:
                continue
            positions = player.positions or (["SP"] if pp.stat_type == "pitching" else ["Util"])
            if slot and not _player_eligible_for_slot(positions, slot):
                continue
            # Persist to avoid re-recommending
            db.add(Recommendation(
                team_id=body.team_id,
                rec_type="find_player_milb",
                player_id=pp.player_id,
                action=f"search:{search_params_label}",
                rationale_blurb=None,
                category_impact={},
                priority_score=pp.pav_score or 0.0,
                created_at=datetime.now(timezone.utc),
                expires_at=None,
            ))
            db.commit()
            return FindPlayerSuggestionRead(
                player_id=pp.player_id,
                player_name=player.name,
                positions=positions,
                priority_score=pp.pav_score or 0.0,
                category_impact={},
                blurb=None,
                created_at=datetime.now(timezone.utc),
                search_params_label=search_params_label,
                is_prospect=True,
                pav_score=pp.pav_score,
            )
        return None

    # ── MiLB-only path ────────────────────────────────────────────────────────
    if body.player_pool == "milb":
        prospect = _get_top_prospect(body.position_slot)
        if not prospect:
            raise HTTPException(
                status_code=404,
                detail="No MiLB prospects found. Prospect data may not be synced yet.",
            )
        return FindPlayerResponse(suggestion=prospect, milb_suggestion=None, all_suggestions=[])

    # ── MLB path ──────────────────────────────────────────────────────────────
    lookback, predictive = _compute_rankings(db, categories)
    if not lookback:
        raise HTTPException(status_code=404, detail="No player stats available.")

    max_acq = (league.settings or {}).get("max_acquisitions_per_week", 4)
    roster_ids = team.roster or []

    ctx = WaiverContext(
        team_id=body.team_id,
        roster_player_ids=roster_ids,
        league_type=league.league_type,
        scoring_categories=categories,
        roster_positions=league.roster_positions or [],
        max_acquisitions_remaining=max_acq,
        all_rankings=lookback,
        predictive_rankings=predictive or lookback,
        all_rostered_ids=all_excluded_mlb,
        build_preferences=BuildPreferences(),
    )

    recommender = Recommender(categories, league_type=league.league_type)
    recommendations = recommender.get_waiver_recommendations(ctx, limit=50)

    # Filter by position slot (if provided)
    position_recs = (
        [r for r in recommendations if _player_eligible_for_slot(r.positions, body.position_slot)]
        if body.position_slot
        else list(recommendations)
    )

    # Fallback: the general recommender's need-weighted scoring may fill all 50
    # slots with players at other positions (e.g. all batters when RP is requested).
    # If the slot filter returns nothing, bypass need-weighting and pull directly
    # from the full predictive rankings for that position.
    if not position_recs and body.position_slot:
        slot_source = predictive or lookback
        fallback_candidates = [
            r for r in slot_source
            if r.player_id not in all_excluded_mlb
            and _player_eligible_for_slot(r.positions, body.position_slot)
        ]
        # Wrap in lightweight WaiverRecommendation-like objects so downstream
        # code (blurb, persist) works identically.
        from fantasai.brain.recommender import WaiverRecommendation
        position_recs = [
            WaiverRecommendation(
                player_id=r.player_id,
                player_name=r.name,
                team=r.team,
                positions=r.positions,
                priority_score=r.score,
                category_impact={
                    c: v for c, v in r.category_contributions.items() if v > 0
                },
                fills_positions=[body.position_slot],
                weak_categories_addressed=[],
                drop_candidates=[],
                action=f"Add {r.name} ({'/'.join(r.positions)})",
            )
            for r in fallback_candidates[:20]
        ]

    # Re-sort by priority categories if requested
    if body.priority_categories:
        position_recs.sort(
            key=lambda r: sum(r.category_impact.get(c, 0.0) for c in body.priority_categories),
            reverse=True,
        )

    if not position_recs:
        slot_desc = f"position '{body.position_slot}'" if body.position_slot else "any position"
        raise HTTPException(
            status_code=404,
            detail=(
                f"No available players found for {slot_desc}. "
                "All suggestions may have been exhausted — history resets daily."
            ),
        )

    best = position_recs[0]

    # Blurb generation
    pred_map = {r.player_id: r for r in (predictive or lookback)}
    pred_ranking = pred_map.get(best.player_id)
    blurb = ""
    if pred_ranking:
        blurb = _generate_find_player_blurb(pred_ranking, categories, body.position_slot or "", db=db)

    # Persist MLB suggestion
    slot_part = body.position_slot or "any"
    rec_type_mlb = f"find_player_mlb_{slot_part}"
    new_rec = Recommendation(
        team_id=body.team_id,
        rec_type=rec_type_mlb,
        player_id=best.player_id,
        action=f"search:{search_params_label}",
        rationale_blurb=blurb or best.rationale_blurb,
        category_impact=best.category_impact,
        priority_score=best.priority_score,
        created_at=datetime.now(timezone.utc),
        expires_at=None,
    )
    db.add(new_rec)
    db.commit()
    db.refresh(new_rec)

    current_suggestion = FindPlayerSuggestionRead(
        player_id=best.player_id,
        player_name=best.player_name,
        positions=best.positions,
        priority_score=best.priority_score,
        category_impact=best.category_impact,
        blurb=blurb or best.rationale_blurb,
        created_at=new_rec.created_at,
        search_params_label=search_params_label,
        is_prospect=False,
    )

    # MiLB suggestion for "both" mode
    milb_suggestion: Optional[FindPlayerSuggestionRead] = None
    if body.player_pool == "both":
        milb_suggestion = _get_top_prospect(body.position_slot)

    return FindPlayerResponse(
        suggestion=current_suggestion,
        milb_suggestion=milb_suggestion,
        all_suggestions=[],
    )


def _get_player_name(db: Session, player_id: int) -> str:
    """Fetch player name from DB, falling back to str(player_id)."""
    player = db.get(Player, player_id)
    return player.name if player else str(player_id)


# ---------------------------------------------------------------------------
# LLM helpers for new analysis features
# ---------------------------------------------------------------------------


def _generate_team_eval_blurb(
    evaluation: TeamEvaluation,
    categories: list[str],
    context: Optional[str],
    roster_notes: Optional[dict] = None,
    actual_category_percentiles: Optional[dict[str, float]] = None,
    grading_basis: str = "absolute_pool",
) -> str:
    """Generate a team evaluation narrative blurb via Anthropic API."""
    client = _llm_client()
    if not client:
        return ""

    try:
        lines = ["━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━"]
        if context:
            lines.append(f"User context: {context}")
        lines.append(
            f"Overall score: {evaluation.overall_score:.2f} | "
            f"Grade: {evaluation.letter_grade} ({evaluation.grade_percentile:.0f}th percentile)"
        )
        _JUNK = frozenset({"H/AB", "Batting", "Pitching", "AB"})
        strong_cats = [c for c in evaluation.strong_categories if c not in _JUNK]
        weak_cats   = [c for c in evaluation.weak_categories   if c not in _JUNK]
        lines.append(f"Strong categories: {', '.join(strong_cats) or 'none'}")
        lines.append(f"Weak categories: {', '.join(weak_cats) or 'none'}")
        if actual_category_percentiles:
            pct_parts = [f"{c}: {v:.0f}%" for c, v in sorted(actual_category_percentiles.items(), key=lambda x: -x[1])]
            lines.append(f"Actual YTD category standings (percentile vs league): {', '.join(pct_parts)}")
        if grading_basis == "absolute_pool":
            lines.append("Note: grade is vs full player pool (no league_id provided), not vs this specific league.")
        lines.append("")
        lines.append("Position breakdown (score | assessment):")
        for g in evaluation.position_breakdown[:8]:
            lines.append(f"  {g.position}: {g.group_score:.2f} — {g.assessment} ({', '.join(g.players[:3])})")
        lines.append("")
        lines.append("Improvement suggestions:")
        for s in evaluation.improvement_suggestions[:4]:
            lines.append(f"  - {s}")
        if roster_notes:
            if roster_notes.get("il_players"):
                lines.append(f"Players on IL (not scoring): {', '.join(roster_notes['il_players'])}")
            if roster_notes.get("active_injured"):
                lines.append(f"Hurt but starting (discounted): {', '.join(roster_notes['active_injured'])}")
            if roster_notes.get("bench_overflow"):
                lines.append(
                    f"Roster-locked players (scoring model gave <50% weight — "
                    f"these players genuinely cannot get regular at-bats/starts given roster construction): "
                    f"{', '.join(roster_notes['bench_overflow'])}"
                )
            if roster_notes.get("position_surplus"):
                surplus_str = ", ".join(f"{p} (+{n})" for p, n in roster_notes["position_surplus"].items())
                lines.append(f"Position depth surplus (too many eligible players for available slots): {surplus_str}")
            if roster_notes.get("position_deficit"):
                deficit_str = ", ".join(f"{p} (-{n})" for p, n in roster_notes["position_deficit"].items())
                lines.append(f"Position depth deficit: {deficit_str}")
        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")
        lines.append(
            "Write a 3–5 sentence team evaluation. State the grade, what the team "
            "does well, where they're vulnerable, and one key improvement priority. "
            "IMPORTANT: Actual YTD standings take precedence over projected weaknesses — "
            "do NOT say a team is weak in a category if their actual YTD percentile is above 50%. "
            "Use the actual standings to describe current strengths, and only flag projected weakness "
            "if it diverges significantly from actual (e.g. 'Currently 2nd in SBs but projected to weaken'). "
            "If players are on the IL, note the impact. "
            "If there's a position depth imbalance or players genuinely roster-locked, mention it as a structural issue — "
            "describe the surplus/deficit by POSITION GROUP, not by naming individual stars as benched. "
            "HARD RULE: NEVER state that a specific named player is 'on the bench', 'not starting', or 'sitting' "
            "unless they appear in the IL list. Position congestion is a structural roster issue, "
            "not a fact about any individual player's lineup deployment. "
            "HARD RULE — CATEGORY/PLAYER ATTRIBUTION: "
            "Pitching categories (K, W, SV, ERA, WHIP, IP) are produced by pitchers only — "
            "NEVER credit a named batter for K, W, SV, ERA, WHIP, or IP strength. "
            "Batting categories (HR, R, RBI, SB, AVG) are produced by batters only — "
            "NEVER credit a named pitcher for HR, R, RBI, SB, or AVG strength.\n"
            "HARD RULE — NO RAW Z-SCORES: NEVER print raw numeric z-score values (e.g. '+2.1', '-4.5'). "
            "Translate to natural language: 'league-leading in saves', 'last in innings pitched', 'positive in SBs'.\n"
            "HARD RULE — PERSONALITY: Minimum two personality elements — "
            "at least one analogy or cultural reference AND one signature phrase or irreverent observation. "
            "A stat recitation is not acceptable.\n"
            + (f"Address user context: {context}" if context else "")
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=450,
            system=[{"type": "text", "text": _ANALYSIS_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        return text_blocks[0].text.strip() if text_blocks else ""
    except Exception as exc:
        logger.warning("Team eval blurb generation failed: %s", exc)
        return ""


def _generate_keeper_eval_blurb(
    evaluation: KeeperEvaluation,
    categories: list[str],
    context: Optional[str],
) -> str:
    """Generate a keeper evaluation narrative via Anthropic API."""
    client = _llm_client()
    if not client:
        return ""

    try:
        lines = ["━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━"]
        if context:
            lines.append(f"User context: {context}")
        lines.append(f"Mode: {evaluation.mode}")
        lines.append(f"Keeper foundation grade: {evaluation.keeper_foundation_grade}")
        lines.append(
            f"Keeper threshold (top players most teams would keep): {evaluation.keeper_threshold}"
        )
        lines.append(
            f"Keepers below threshold (wasted slots): {evaluation.n_below_threshold}"
        )

        # Per-keeper detail: rank, positions, top category contributions.
        # This gives the LLM enough to describe WHY each keeper is valuable
        # (or not) rather than just naming them and their rank number.
        _CAT_TIERS = [(2.0, "elite"), (1.0, "strong"), (0.3, "avg"), (-0.3, "neutral"),
                      (-1.0, "below avg"), (float("-inf"), "drag")]

        def _cat_tier(z: float) -> str:
            for thr, lbl in _CAT_TIERS:
                if z >= thr:
                    return lbl
            return "drag"

        lines.append(f"Keepers ({len(evaluation.keepers)}) — name | rank | positions | top category signals:")
        for r in evaluation.keepers[:8]:
            rank_label = f"#{r.overall_rank}" if r.overall_rank > 0 else "unranked"
            below = " ⚠ below threshold" if r.overall_rank > evaluation.keeper_threshold else ""
            pos_str = "/".join(r.positions) if r.positions else "UTIL"
            # Show the 3 categories with the largest absolute contribution
            top_cats = sorted(
                r.category_contributions.items(), key=lambda kv: -abs(kv[1])
            )[:3]
            cat_str = ", ".join(
                f"{cat}: {_cat_tier(z)} ({'+' if z >= 0 else ''}{z:.1f})"
                for cat, z in top_cats
            ) if top_cats else "no signal data"
            lines.append(f"  {r.name} | {rank_label}{below} | {pos_str} | {cat_str}")

        if evaluation.cuts:
            lines.append(f"Cuts ({len(evaluation.cuts)}): {', '.join(r.name for r in evaluation.cuts[:5])}")
        lines.append(f"Category gaps: {', '.join(evaluation.category_gaps[:5]) or 'none'}")
        lines.append(f"Position gaps: {', '.join(evaluation.position_gaps[:5]) or 'none'}")
        lines.append("")
        lines.append("Top draft profiles:")
        for dp in evaluation.draft_profiles[:3]:
            examples = f" (e.g. {', '.join(dp.example_players[:2])})" if dp.example_players else ""
            lines.append(f"  #{dp.priority} {dp.position} [{', '.join(dp.category_targets)}]{examples}: {dp.rationale}")
        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")

        if evaluation.mode == "plan_keepers":
            instruction = (
                "Write 3–5 sentences evaluating the recommended keeper core and draft strategy. "
                "Mention the strongest keeper(s), the biggest gap(s) to fill, and the #1 draft priority. "
                "If any keepers are flagged '⚠ below threshold', note that those are questionable keeps "
                "that most teams in the league would not use a keeper slot on."
            )
        else:
            instruction = (
                "Write 3–5 sentences evaluating this keeper core's strengths, weaknesses, "
                "and most important draft target profiles. "
                "Be honest about the grade. If keepers are flagged '⚠ below threshold', "
                "note that these carries more value risk as keeper slots — but don't be rigid: "
                "a player outside the typical keep range can still make sense as a high-upside "
                "prospect, a punted-category specialist, or a player held at a favorable keeper "
                "cost. Factor in context before dismissing them outright."
            )
        if context:
            instruction += f" Address user context: {context}"

        instruction += (
            " Use the per-keeper category signals to explain WHY specific keepers are "
            "valuable or risky — not just their rank. "
            "HARD RULE — PERSONALITY MINIMUM: MINIMUM TWO personality elements required. "
            "Include AT LEAST ONE analogy or cultural reference AND AT LEAST ONE signature "
            "phrase or irreverent observation. A dry keeper verdict with zero voice is a failure."
        )

        lines.append(instruction)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
            system=[{"type": "text", "text": _ANALYSIS_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        return text_blocks[0].text.strip() if text_blocks else ""
    except Exception as exc:
        logger.warning("Keeper eval blurb generation failed: %s", exc)
        return ""


def _generate_compare_teams_blurb(
    comparison: TeamsComparison,
    context: Optional[str],
    roster_notes: Optional[dict[str, dict]] = None,
    actual_category_strengths: Optional[dict[str, dict[str, float]]] = None,
) -> str:
    """Generate a multi-team comparison narrative via Anthropic API."""
    client = _llm_client()
    if not client:
        return ""

    try:
        lines = ["━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━"]
        if context:
            lines.append(f"User context: {context}")
        _JUNK = frozenset({"H/AB", "Batting", "Pitching", "AB"})
        lines.append("Team comparison (ranked by projected power score):")
        for snap in comparison.snapshots:
            clean_strong = [c for c in snap.strong_cats if c not in _JUNK]
            clean_weak   = [c for c in snap.weak_cats   if c not in _JUNK]
            lines.append(
                f"  {snap.team_name} (id={snap.team_id}): power={snap.power_score:.2f} | "
                f"projected strong={', '.join(clean_strong[:3])} | "
                f"projected weak={', '.join(clean_weak[:3])} | "
                f"top players: {', '.join(snap.top_players[:2])}"
            )
        # Actual YTD category strengths — these reflect real standings, not projections.
        # The LLM MUST use these to describe current category performance.
        if actual_category_strengths:
            lines.append("")
            lines.append(
                "ACTUAL YTD category strengths (z-score sums vs league — use these, "
                "not projected labels, when describing current category performance):"
            )
            for team_name, cat_scores in actual_category_strengths.items():
                if not cat_scores:
                    continue
                # Sort strongest → weakest for LLM readability
                sorted_cats = sorted(cat_scores.items(), key=lambda x: -x[1])
                parts = [f"{c}:{v:+.2f}" for c, v in sorted_cats]
                lines.append(f"  {team_name}: {', '.join(parts)}")
            lines.append(
                "  NOTE: Positive z-score = above league average in that category. "
                "A team with SV:+2.1 is STRONG in saves, NOT weak. "
                "Do NOT contradict actual standings with projected weakness labels."
            )
        if comparison.trade_opportunities:
            lines.append("")
            lines.append("Trade opportunities:")
            for opp in comparison.trade_opportunities[:3]:
                lines.append(f"  {opp.rationale}")
        if roster_notes:
            for tname, notes in roster_notes.items():
                note_parts = []
                if notes.get("il_players"):
                    note_parts.append(f"IL: {', '.join(notes['il_players'])}")
                if notes.get("bench_overflow"):
                    note_parts.append(f"roster-locked (<50% weight): {', '.join(notes['bench_overflow'])}")
                if notes.get("position_surplus"):
                    surplus_str = ", ".join(f"{p}+{n}" for p, n in notes["position_surplus"].items())
                    note_parts.append(f"depth surplus: {surplus_str}")
                if note_parts:
                    lines.append(f"  {tname} roster notes: {'; '.join(note_parts)}")
        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")
        lines.append(
            "Write a 3–5 sentence comparison. Identify the strongest team and why — "
            "be specific about which categories and which players are driving the gap. "
            "Cover each team's real key advantage and real key weakness. Include the most interesting trade angle. "
            "IMPORTANT: Base ALL category descriptions on ACTUAL YTD standings above, not projected labels. "
            "If the projected data says a team is weak in SV but their actual SV z-score is positive, "
            "they are NOT weak in SV — describe it accurately based on what has actually happened. "
            "Note any IL players or roster construction issues (position gluts, thin spots). "
            "\n"
            "HARD RULE — CATEGORY/PLAYER ATTRIBUTION: "
            "Pitching categories (K, W, SV, ERA, WHIP, IP) are produced by PITCHERS. "
            "NEVER name a specific batter as the reason a team leads or lags in K, W, SV, ERA, WHIP, or IP. "
            "Batting categories (HR, R, RBI, SB, AVG, OBP, H, BB) are produced by BATTERS. "
            "NEVER name a specific pitcher as the reason a team leads or lags in HR, R, RBI, SB, or AVG. "
            "Only name a player when the category actually matches their role.\n"
            "HARD RULE — NO RAW Z-SCORES: NEVER print the raw numeric z-score values from the data block "
            "(e.g. '+11.50', '-4.52', '+2.15'). These numbers mean nothing to a reader. "
            "Translate them to natural language: 'leading the group in strikeouts', "
            "'bleeding innings pitched', 'the only team with a positive stolen-base balance'. "
            "You may say 'first', 'last', 'ahead of', 'behind' — but never paste the raw number.\n"
            "HARD RULE — BENCH/LINEUP: NEVER state that a specific named player is 'on the bench', "
            "'not starting', or 'sitting' unless they appear in the IL list.\n"
            "HARD RULE — PERSONALITY: Minimum two personality elements required. "
            "At least one analogy or cultural reference AND one signature phrase or irreverent observation. "
            "A neutral stat comparison is not acceptable — this is a league story, not a spreadsheet.\n"
            + (f"Address user context: {context}" if context else "")
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,  # 350 was causing mid-sentence truncation
            system=[{"type": "text", "text": _ANALYSIS_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        return text_blocks[0].text.strip() if text_blocks else ""
    except Exception as exc:
        logger.warning("Compare teams blurb generation failed: %s", exc)
        return ""


def _generate_league_power_blurb(
    report: LeaguePowerReport,
    tiers: dict[str, list[int]],
    team_names: dict[int, str],
    roster_notes: Optional[dict[str, dict]] = None,
) -> str:
    """Generate a league power rankings narrative via Anthropic API."""
    client = _llm_client()
    if not client:
        return ""

    # Display artefacts from Yahoo that sometimes bleed into category lists.
    # Strip them here so the LLM never prints "strong in H/AB" or "weak in Batting".
    _JUNK_CATS: frozenset = frozenset({"H/AB", "Batting", "Pitching", "AB"})

    try:
        lines = ["━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━"]
        lines.append(
            "League Power Rankings (predictive roster strength):\n"
            "  power = total roster strength (sum of player z-scores; bigger = more firepower)\n"
            "  depth = average score per player (higher = genuine top-to-bottom quality;\n"
            "          low depth with high power = one or two stars holding up a thin roster)"
        )
        for i, snap in enumerate(report.power_rankings, 1):
            # Strip Yahoo display artefacts from category labels
            clean_strong = [c for c in snap.strong_cats if c not in _JUNK_CATS]
            clean_weak   = [c for c in snap.weak_cats   if c not in _JUNK_CATS]
            weak_str = ", ".join(clean_weak[:2]) if clean_weak else "none"
            depth_str = f", depth={snap.average_score:.2f}" if snap.average_score else ""
            lines.append(
                f"  #{i} {snap.team_name} (power={snap.power_score:.2f}{depth_str}) | "
                f"strong={', '.join(clean_strong[:3])} | weak={weak_str}"
            )
        lines.append("")
        lines.append("Tiers:")
        for tier, ids in tiers.items():
            names = [team_names.get(tid, str(tid)) for tid in ids]
            lines.append(f"  {tier.capitalize()}: {', '.join(names)}")
        if roster_notes:
            notable = []
            for tname, notes in roster_notes.items():
                parts = []
                if notes.get("il_players"):
                    parts.append(f"{len(notes['il_players'])} on IL ({', '.join(notes['il_players'][:2])})")
                if notes.get("bench_overflow"):
                    parts.append(f"{len(notes['bench_overflow'])} bench-only")
                if parts:
                    notable.append(f"{tname}: {', '.join(parts)}")
            if notable:
                lines.append("Roster injury/depth notes:")
                for n in notable[:6]:  # cap at 6 to keep prompt reasonable
                    lines.append(f"  {n}")
        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")
        lines.append(
            "Write a 4–6 sentence league power rankings narrative. "
            "Name specific teams when describing each tier — explain WHY each contender is strong "
            "(which scoring categories they dominate) and WHY rebuilding teams are struggling "
            "(specific weak spots). "
            "Use the depth score to distinguish teams with one or two stars propping up a thin roster "
            "from rosters with genuine top-to-bottom quality — but translate this into plain language "
            "(e.g. 'deep roster', 'one injury away from trouble', 'genuine top-to-bottom quality') "
            "rather than printing the number or the word 'depth' literally. "
            "Mention which middle-pack teams are closest to breaking into the top tier and what's holding them back. "
            "Do NOT mention trades or trade opportunities — this is a pure power ranking summary. "
            "BANNED non-scoring categories — never mention these: H/AB, Batting, Pitching, AB. "
            "If any appear in the data, ignore them. "
            "Mandatory: MINIMUM two personality elements in this response. "
            "At least one analogy or cultural reference AND at least one signature phrase or irreverent observation. "
            "A neutral standings printout is not acceptable — this is a league storyline, not a spreadsheet."
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,  # 400 was too tight for a full 12-team narrative
            system=[{"type": "text", "text": _ANALYSIS_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text_blocks = [b for b in response.content if b.type == "text"]
        return text_blocks[0].text.strip() if text_blocks else ""
    except Exception as exc:
        logger.warning("League power blurb generation failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _snapshot_to_read(snap) -> TeamSnapshotRead:  # TeamSnapshot → schema
    return TeamSnapshotRead(
        team_id=snap.team_id,
        team_name=snap.team_name,
        power_score=snap.power_score,
        average_score=snap.average_score,
        category_strengths=snap.category_strengths,
        strong_cats=snap.strong_cats,
        weak_cats=snap.weak_cats,
        top_players=snap.top_players,
    )


def _trade_opp_to_read(opp) -> TradeOpportunityRead:  # TradeOpportunity → schema
    return TradeOpportunityRead(
        team_a_id=opp.team_a_id,
        team_b_id=opp.team_b_id,
        team_a_gives_cats=opp.team_a_gives_cats,
        team_b_gives_cats=opp.team_b_gives_cats,
        suggested_give=opp.suggested_give,
        suggested_receive=opp.suggested_receive,
        complementarity_score=opp.complementarity_score,
        rationale=opp.rationale,
    )


# ---------------------------------------------------------------------------
# Roster weighting helper
# ---------------------------------------------------------------------------


def _apply_team_weights(
    player_ids: list[int],
    ranking_map: dict,
    raw_multi: dict,
    consumed: dict,
    roster_positions: list[str],
    il_player_ids: Optional[list[int]],
    injured_player_statuses: Optional[dict],
) -> tuple[list, dict, dict]:
    """Build weighted roster rankings for a team.

    Returns (weighted_rankings, weights, roster_notes_dict).
    roster_notes_dict is empty if roster_positions is not set.
    """
    # Build raw rankings via multimap (handles two-way players)
    _raw_multi: dict = raw_multi
    _consumed:  dict = consumed
    raw_rankings = []
    for pid in player_ids:
        entries = _raw_multi.get(pid)
        if not entries:
            continue
        idx = _consumed[pid]
        raw_rankings.append(entries[idx % len(entries)])
        _consumed[pid] += 1

    if not roster_positions:
        return raw_rankings, {r.player_id: 1.0 for r in raw_rankings}, {}

    inj_int = {int(k): v for k, v in (injured_player_statuses or {}).items()}
    weights = compute_roster_weights(
        raw_rankings, roster_positions,
        il_player_ids=il_player_ids,
        injured_statuses=inj_int,
    )
    weighted = apply_weights(raw_rankings, weights)
    notes = build_roster_notes(
        raw_rankings, weights, roster_positions,
        il_player_ids=il_player_ids,
        injured_statuses=inj_int,
    )
    return weighted, weights, notes


# ---------------------------------------------------------------------------
# Endpoint 4 — Team Evaluation
# ---------------------------------------------------------------------------


@router.post(
    "/team-eval",
    response_model=TeamEvalResponse,
    summary="Evaluate a team holistically: letter grade, position breakdown, improvement tips",
)
def team_eval_endpoint(
    body: TeamEvalRequest,
    db: Session = Depends(get_db),
    _limit: None = Depends(check_rate_limit("team-eval")),
) -> TeamEvalResponse:
    """Evaluate a fantasy roster from top to bottom.

    Accepts either a ``team_id`` (roster fetched from DB) or a raw
    ``player_ids`` list. Returns a letter grade (A–F), position-by-position
    scoring breakdown, category strengths/gaps, and algorithmic improvement
    suggestions. An optional ``context`` string (e.g. "win-now mode") is
    factored into both the algorithmic analysis and the LLM narrative.
    """
    # Resolve scoring categories
    categories = DEFAULT_CATEGORIES
    league = None
    league_type = "h2h_categories"
    roster_positions: list[str] = []

    if body.league_id:
        from fantasai.models.league import League
        league = db.get(League, body.league_id)
        if league:
            categories = league.scoring_categories or DEFAULT_CATEGORIES
            league_type = league.league_type or "h2h_categories"
            roster_positions = league.roster_positions or []

    if body.custom_categories:
        categories = body.custom_categories
    if body.custom_league_type:
        league_type = body.custom_league_type
    if body.custom_roster_positions:
        roster_positions = body.custom_roster_positions

    # Compute rankings
    lookback, predictive = _compute_rankings(db, categories)
    source = predictive if body.ranking_type == "predictive" else lookback
    if not source:
        raise HTTPException(status_code=404, detail="No player stats available for rankings.")

    # Deduped map used for league-wide team comparisons (each player counted once).
    ranking_map = {r.player_id: r for r in source}

    # Build roster rankings using *raw* (non-deduped) rankings so that two-way
    # players like Ohtani retain separate batting and pitching entries.
    # The deduped `ranking_map` is used for league-wide comparisons below.
    from fantasai.api.v1.recommendations import ProjectionHorizon as _PH
    raw_pair = _get_cached_raw_rankings(categories, _PH.SEASON)
    raw_source = (raw_pair[1] if body.ranking_type == "predictive" else raw_pair[0]) if raw_pair else source

    # Multimap: player_id → list of raw ranking entries (sorted best-first).
    # Ohtani will have two entries; single-way players have one.
    from collections import defaultdict
    _raw_multi: dict = defaultdict(list)
    for r in raw_source:
        _raw_multi[r.player_id].append(r)

    # Build roster rankings
    if body.team_id:
        team = db.get(Team, body.team_id)
        if not team:
            raise HTTPException(status_code=404, detail=f"Team {body.team_id} not found.")
        player_ids     = team.roster or []
        il_ids         = list(team.il_player_ids or [])
        injured_stats  = dict(team.injured_player_statuses or {})
    else:
        player_ids    = body.player_ids or []
        il_ids        = []
        injured_stats = {}

    _consumed: dict = defaultdict(int)
    roster_rankings, _weights, _roster_notes = _apply_team_weights(
        player_ids, ranking_map, _raw_multi, _consumed,
        roster_positions, il_ids, injured_stats,
    )

    # Compute league-wide distributions for relative grading.
    # Score each team's position groups and category strengths so assessment
    # labels (Elite/Solid/Average/Weak) and category percentiles are
    # league-relative rather than vs the full player pool.
    league_team_scores: Optional[list[float]] = None
    league_position_mean_scores: Optional[dict[str, list[float]]] = None
    _league_category_scores: dict[str, list[float]] = {}
    _lb_league_category_scores: dict[str, list[float]] = {}

    if league:
        from fantasai.brain.team_evaluator import _compute_group_scores as _cgs
        league_team_scores = []
        _lp_means: dict[str, list[float]] = {}

        # Positions that every team should have — teams with no coverage get 0
        # in the distribution so the comparison pool is always the full league.
        _REQUIRED_POS = {"C", "SP", "RP"}

        for t in (league.teams or []):
            t_il   = list(t.il_player_ids or [])
            t_inj  = {int(k): v for k, v in (t.injured_player_statuses or {}).items()}
            t_raw  = [ranking_map[pid] for pid in (t.roster or []) if pid in ranking_map]
            if not t_raw:
                continue
            if roster_positions:
                t_w   = compute_roster_weights(t_raw, roster_positions, t_il, t_inj)
                t_rankings = apply_weights(t_raw, t_w)
            else:
                t_rankings = t_raw
            if not t_rankings:
                continue
            t_score = sum(r.score for r in t_rankings) / len(t_rankings)
            league_team_scores.append(t_score)

            t_groups = _cgs(t_rankings, categories)
            covered_pos = set(t_groups.keys())
            for pos, (_players, _gscore, mean_s) in t_groups.items():
                _lp_means.setdefault(pos, []).append(mean_s)

            # Teams missing a required position count as 0 so the pool
            # reflects all 12 teams (not just those with a catcher, etc.)
            for req_pos in _REQUIRED_POS:
                if req_pos not in covered_pos:
                    _lp_means.setdefault(req_pos, []).append(0.0)

            for cat, score in _compute_team_strengths(t_rankings, categories).items():
                _league_category_scores.setdefault(cat, []).append(score)

        if _lp_means:
            league_position_mean_scores = _lp_means

        # Compute lookback (actual YTD) category scores for league_category_percentiles.
        # Using lookback means percentiles reflect real standings, not Steamer projections.
        lb_ranking_map = {r.player_id: r for r in lookback} if lookback else {}
        for t in (league.teams or []):
            t_raw_lb = [lb_ranking_map[pid] for pid in (t.roster or []) if pid in lb_ranking_map]
            if not t_raw_lb:
                continue
            if roster_positions:
                t_w_lb = compute_roster_weights(
                    t_raw_lb, roster_positions,
                    list(t.il_player_ids or []),
                    {int(k): v for k, v in (t.injured_player_statuses or {}).items()},
                )
                t_rankings_lb = apply_weights(t_raw_lb, t_w_lb)
            else:
                t_rankings_lb = t_raw_lb
            if not t_rankings_lb:
                continue
            for cat, score in _compute_team_strengths(t_rankings_lb, categories).items():
                _lb_league_category_scores.setdefault(cat, []).append(score)

    evaluation = evaluate_team(
        roster_rankings=roster_rankings,
        categories=categories,
        roster_positions=roster_positions,
        league_type=league_type,
        league_team_scores=league_team_scores if league_team_scores else None,
        league_position_mean_scores=league_position_mean_scores,
        context=body.context,
    )

    # Compute actual YTD category strengths for the focal team (lookback source)
    lb_raw_multi: dict = defaultdict(list)
    for r in (lookback or []):
        lb_raw_multi[r.player_id].append(r)

    lb_roster_rankings: list[PlayerRanking] = []
    for pid in player_ids:
        entries = lb_raw_multi.get(pid, [])
        if entries:
            lb_roster_rankings.append(max(entries, key=lambda r: r.score))

    actual_category_strengths: Optional[dict[str, float]] = None
    if lb_roster_rankings:
        actual_category_strengths = _compute_team_strengths(lb_roster_rankings, categories)

    # Per-category league percentile rank — use lookback (actual YTD), not predictive
    league_category_percentiles: Optional[dict[str, float]] = None
    if _lb_league_category_scores and actual_category_strengths:
        league_category_percentiles = {}
        for cat, team_score in actual_category_strengths.items():
            scores = _lb_league_category_scores.get(cat, [])
            if scores:
                rank = sum(1 for s in scores if s < team_score)
                pct = round(rank / len(scores) * 100, 1)
            else:
                pct = 50.0
            league_category_percentiles[cat] = pct
    elif _league_category_scores:
        # Fallback to predictive if no lookback data
        league_category_percentiles = {}
        for cat, team_score in evaluation.category_strengths.items():
            scores = _league_category_scores.get(cat, [])
            if scores:
                rank = sum(1 for s in scores if s < team_score)
                pct = round(rank / len(scores) * 100, 1)
            else:
                pct = 50.0
            league_category_percentiles[cat] = pct

    grading_basis = "league_relative" if (league and league_team_scores) else "absolute_pool"

    blurb = _generate_team_eval_blurb(
        evaluation, categories, body.context, _roster_notes,
        actual_category_percentiles=league_category_percentiles,
        grading_basis=grading_basis,
    )

    return TeamEvalResponse(
        overall_score=evaluation.overall_score,
        letter_grade=evaluation.letter_grade,
        grade_percentile=evaluation.grade_percentile,
        category_strengths=evaluation.category_strengths,
        strong_categories=evaluation.strong_categories,
        weak_categories=evaluation.weak_categories,
        position_breakdown=[
            PositionGroupRead(
                position=g.position,
                players=g.players,
                group_score=g.group_score,
                assessment=g.assessment,
            )
            for g in evaluation.position_breakdown
        ],
        improvement_suggestions=evaluation.improvement_suggestions,
        pros=evaluation.pros,
        cons=evaluation.cons,
        analysis_blurb=blurb,
        league_category_percentiles=league_category_percentiles,
        grading_basis=grading_basis,
        actual_category_strengths=actual_category_strengths,
    )


# ---------------------------------------------------------------------------
# Endpoint 5 — Keeper / Dynasty Evaluation
# ---------------------------------------------------------------------------


@router.post(
    "/keeper-eval",
    response_model=KeeperEvalResponse,
    summary="Evaluate keepers or plan who to keep from a full roster",
)
def keeper_eval_endpoint(
    body: KeeperEvalRequest,
    db: Session = Depends(get_db),
    _limit: None = Depends(check_rate_limit("keeper-eval")),
) -> KeeperEvalResponse:
    """Keeper/dynasty planning endpoint.

    Two modes:
    - ``evaluate_keepers``: input players ARE the confirmed keepers —
      evaluate the keeper core and suggest draft target profiles.
    - ``plan_keepers``: input is the full current roster — app recommends
      the best N players to keep and then evaluates that keeper core.

    Accepts ``context`` for both algorithmic adjustments and LLM narrative.
    """
    # Resolve league / categories
    categories = DEFAULT_CATEGORIES
    league_type = "h2h_categories"
    roster_positions: list[str] = []

    if body.league_id:
        from fantasai.models.league import League
        league = db.get(League, body.league_id)
        if league:
            categories = league.scoring_categories or DEFAULT_CATEGORIES
            league_type = league.league_type or "h2h_categories"
            roster_positions = league.roster_positions or []

    if body.custom_categories:
        categories = body.custom_categories
    if body.custom_league_type:
        league_type = body.custom_league_type
    if body.custom_roster_positions:
        roster_positions = body.custom_roster_positions

    # Prefer Steamer projections (season=2026) for keeper evaluation —
    # they account for age regression, prior performance, and playing time
    # changes, giving a more accurate forward-looking value than YTD actuals.
    # Fall back to predictive/lookback rankings if projections aren't ingested yet.
    steamer_rankings = _compute_projection_rankings(db, categories, projection_season=2026)
    if steamer_rankings:
        source = steamer_rankings
        logger.info(
            "keeper_eval: using %d Steamer 2026 projection rankings", len(steamer_rankings)
        )
    else:
        lookback, predictive = _compute_rankings(db, categories)
        source = predictive or lookback
        logger.info(
            "keeper_eval: Steamer projections not available — using YTD rankings (%d)",
            len(source),
        )
    if not source:
        raise HTTPException(status_code=404, detail="No player stats available for rankings.")

    ranking_map = {r.player_id: r for r in source}

    # Resolve player IDs
    if body.team_id:
        team = db.get(Team, body.team_id)
        if not team:
            raise HTTPException(status_code=404, detail=f"Team {body.team_id} not found.")
        player_ids = team.roster or []
    else:
        player_ids = body.player_ids or []

    input_rankings = [ranking_map[pid] for pid in player_ids if pid in ranking_map]

    if not input_rankings:
        raise HTTPException(status_code=404, detail="No ranked players found for the provided IDs.")

    # Build available pool (non-rostered players) for example player suggestions
    if body.league_id:
        from fantasai.models.league import League
        league_obj = db.get(League, body.league_id)
        if league_obj:
            all_rostered: set[int] = set()
            for t in league_obj.teams or []:
                all_rostered.update(t.roster or [])
            available_pool = [r for r in source if r.player_id not in all_rostered]
        else:
            available_pool = None
    else:
        available_pool = None

    # Build player_ages from stored birth_year — used by plan_keepers for
    # age-based future-value multipliers.  current_year − birth_year gives
    # age for the current season.
    import datetime as _dt
    _current_year = _dt.datetime.now().year
    _roster_player_ids = [r.player_id for r in input_rankings]
    _players_with_ages = (
        db.query(Player)
        .filter(Player.player_id.in_(_roster_player_ids))
        .all()
    )
    player_ages: dict[int, int] = {
        p.player_id: _current_year - p.birth_year
        for p in _players_with_ages
        if p.birth_year is not None
    }

    # Run the appropriate brain function
    if body.mode == "plan_keepers":
        evaluation = plan_keepers(
            full_roster_rankings=input_rankings,
            n_keepers=body.n_keepers,
            categories=categories,
            roster_positions=roster_positions,
            league_type=league_type,
            available_pool=available_pool,
            player_ages=player_ages,
            context=body.context,
            n_teams=body.n_teams,
        )
    else:
        evaluation = evaluate_keepers(
            keeper_rankings=input_rankings,
            categories=categories,
            roster_positions=roster_positions,
            league_type=league_type,
            available_pool=available_pool,
            context=body.context,
            n_teams=body.n_teams,
        )

    blurb = _generate_keeper_eval_blurb(evaluation, categories, body.context)

    return KeeperEvalResponse(
        mode=evaluation.mode,
        keepers=[
            PlayerSummaryRead(
                player_id=r.player_id,
                player_name=r.name,
                positions=r.positions,
                score=r.score,
            )
            for r in evaluation.keepers
        ],
        cuts=[
            PlayerSummaryRead(
                player_id=r.player_id,
                player_name=r.name,
                positions=r.positions,
                score=r.score,
            )
            for r in evaluation.cuts
        ],
        keeper_foundation_grade=evaluation.keeper_foundation_grade,
        category_strengths=evaluation.category_strengths,
        category_gaps=evaluation.category_gaps,
        position_gaps=evaluation.position_gaps,
        draft_profiles=[
            DraftProfileRead(
                priority=dp.priority,
                position=dp.position,
                category_targets=dp.category_targets,
                rationale=dp.rationale,
                example_players=dp.example_players,
            )
            for dp in evaluation.draft_profiles
        ],
        pros=evaluation.pros,
        cons=evaluation.cons,
        analysis_blurb=blurb,
    )


# ---------------------------------------------------------------------------
# Endpoint 6 — Compare Teams
# ---------------------------------------------------------------------------


@router.post(
    "/compare-teams",
    response_model=CompareTeamsResponse,
    summary="Head-to-head comparison of 2–6 teams with trade opportunity surfacing",
)
def compare_teams_endpoint(
    body: CompareTeamsRequest,
    db: Session = Depends(get_db),
    _limit: None = Depends(check_rate_limit("compare-teams")),
) -> CompareTeamsResponse:
    """Compare multiple teams side-by-side.

    Each team is evaluated for power score, category strengths/weaknesses,
    and top players. Trade opportunities between complementary teams are
    detected automatically (can be disabled via ``include_trade_suggestions``).

    Teams can be from any league; ``league_id`` is used only for category
    context when provided.
    """
    categories = DEFAULT_CATEGORIES
    league_type = "h2h_categories"

    if body.league_id:
        from fantasai.models.league import League
        league = db.get(League, body.league_id)
        if league:
            categories = league.scoring_categories or DEFAULT_CATEGORIES
            league_type = league.league_type or "h2h_categories"

    if body.custom_categories:
        categories = body.custom_categories
    if body.custom_league_type:
        league_type = body.custom_league_type

    lookback, predictive = _compute_rankings(db, categories)
    source = predictive or lookback
    if not source:
        raise HTTPException(status_code=404, detail="No player stats available for rankings.")

    ranking_map = {r.player_id: r for r in source}

    # Get roster_positions for weighting (from league if available)
    ct_roster_positions: list[str] = []
    if body.league_id:
        from fantasai.models.league import League as _League
        _lg = db.get(_League, body.league_id)
        if _lg:
            ct_roster_positions = _lg.roster_positions or []

    compare_roster_notes: dict[str, dict] = {}

    # Build team data tuples
    team_data: list[tuple[int, str, list[PlayerRanking]]] = []

    if body.manual_teams:
        for i, mt in enumerate(body.manual_teams):
            fake_id = -(i + 1)
            roster_rankings = [ranking_map[pid] for pid in mt.player_ids if pid in ranking_map]
            # No IL data for manual teams — apply slot-only weighting if positions available
            if ct_roster_positions:
                w = compute_roster_weights(roster_rankings, ct_roster_positions)
                roster_rankings = apply_weights(roster_rankings, w)
                compare_roster_notes[mt.name] = build_roster_notes(roster_rankings, w, ct_roster_positions)
            team_data.append((fake_id, mt.name, roster_rankings))
    else:
        for tid in (body.team_ids or []):
            team = db.get(Team, tid)
            if not team:
                raise HTTPException(status_code=404, detail=f"Team {tid} not found.")
            t_il  = list(team.il_player_ids or [])
            t_inj = {int(k): v for k, v in (team.injured_player_statuses or {}).items()}
            raw   = [ranking_map[pid] for pid in (team.roster or []) if pid in ranking_map]
            name  = team.team_name or team.manager_name or f"Team {tid}"
            if ct_roster_positions:
                w = compute_roster_weights(raw, ct_roster_positions, t_il, t_inj)
                weighted = apply_weights(raw, w)
                compare_roster_notes[name] = build_roster_notes(raw, w, ct_roster_positions, t_il, t_inj)
            else:
                weighted = raw
            team_data.append((tid, name, weighted))

    if len(team_data) < 2:
        raise HTTPException(status_code=422, detail="At least 2 valid teams are required.")

    comparison = compare_teams(
        team_data=team_data,
        categories=categories,
        league_type=league_type,
        include_trades=body.include_trade_suggestions,
    )

    # Compute actual YTD category strengths for each team from lookback rankings.
    # These reflect real standings vs the league, not Steamer projections.
    # The blurb LLM uses these to avoid incorrectly calling a team "weak" in a
    # category where they actually lead the league (e.g. SV, SB).
    ct_actual_strengths: Optional[dict[str, dict[str, float]]] = None
    if lookback:
        lb_map = {r.player_id: r for r in lookback}
        ct_actual_strengths = {}
        for tid, name, _weighted in team_data:
            # Retrieve the original player_ids for this team
            if body.manual_teams:
                # manual teams: get ids from the corresponding manual team entry
                manual_idx = next(
                    (i for i, mt in enumerate(body.manual_teams) if -(i + 1) == tid), None
                )
                pids = body.manual_teams[manual_idx].player_ids if manual_idx is not None else []
            else:
                t_obj = db.get(Team, tid)
                pids = list(t_obj.roster or []) if t_obj else []
            lb_rankings = [lb_map[pid] for pid in pids if pid in lb_map]
            if lb_rankings:
                ct_actual_strengths[name] = _compute_team_strengths(lb_rankings, categories)

    blurb = _generate_compare_teams_blurb(
        comparison, body.context, compare_roster_notes,
        actual_category_strengths=ct_actual_strengths,
    )

    return CompareTeamsResponse(
        snapshots=[_snapshot_to_read(s) for s in comparison.snapshots],
        winner=comparison.winner,
        trade_opportunities=[_trade_opp_to_read(o) for o in comparison.trade_opportunities],
        analysis_blurb=blurb,
    )


# ---------------------------------------------------------------------------
# Endpoint 7 — League Power Rankings
# ---------------------------------------------------------------------------


@router.get(
    "/league-power/{league_id}",
    response_model=LeaguePowerResponse,
    summary="Full league power rankings: tiers, power scores, and top trade pairs",
)
def league_power_endpoint(
    league_id: str,
    db: Session = Depends(get_db),
    _limit: None = Depends(check_rate_limit("league-power")),
) -> LeaguePowerResponse:
    """Compute power rankings for every team in a league.

    Ranks all teams by total roster z-score, groups them into tiers
    (contender / middle / rebuilding), and surfaces the top 10 most
    complementary trade pairs across the league. An LLM-generated narrative
    summarises the power landscape.
    """
    from fantasai.models.league import League

    league = db.get(League, league_id)
    if not league:
        raise HTTPException(status_code=404, detail=f"League {league_id} not found.")

    categories = league.scoring_categories or DEFAULT_CATEGORIES
    league_type = league.league_type or "h2h_categories"

    lookback, predictive = _compute_rankings(db, categories)
    source = predictive or lookback
    if not source:
        raise HTTPException(status_code=404, detail="No player stats available for rankings.")

    ranking_map = {r.player_id: r for r in source}

    # Build team data for all teams in the league
    team_data: list[tuple[int, str, list[PlayerRanking]]] = []
    team_names: dict[int, str] = {}
    lp_roster_notes: dict[str, dict] = {}
    lp_roster_positions = league.roster_positions or []
    for team in league.teams or []:
        raw  = [ranking_map[pid] for pid in (team.roster or []) if pid in ranking_map]
        name = team.team_name or team.manager_name or f"Team {team.team_id}"
        if lp_roster_positions:
            t_il  = list(team.il_player_ids or [])
            t_inj = {int(k): v for k, v in (team.injured_player_statuses or {}).items()}
            w     = compute_roster_weights(raw, lp_roster_positions, t_il, t_inj)
            weighted = apply_weights(raw, w)
            lp_roster_notes[name] = build_roster_notes(raw, w, lp_roster_positions, t_il, t_inj)
        else:
            weighted = raw
        team_data.append((team.team_id, name, weighted))
        team_names[team.team_id] = name

    if not team_data:
        raise HTTPException(status_code=404, detail=f"No teams found for league {league_id}.")

    report = compute_league_power(
        team_data=team_data,
        categories=categories,
        league_type=league_type,
    )

    blurb = _generate_league_power_blurb(report, report.tiers, team_names, lp_roster_notes)

    return LeaguePowerResponse(
        power_rankings=[_snapshot_to_read(s) for s in report.power_rankings],
        tiers=report.tiers,
        trade_opportunities=[],   # trade surfacing moved to a dedicated feature
        analysis_blurb=blurb,
    )


# ---------------------------------------------------------------------------
# Endpoint 8 — Extract Players from Screenshot
# ---------------------------------------------------------------------------


@router.post(
    "/extract-players",
    response_model=ExtractPlayersResponse,
    summary="Extract player names from a screenshot using Claude vision",
)
def extract_players_endpoint(
    body: ExtractPlayersRequest,
    _limit: None = Depends(check_rate_limit("extract-players")),
) -> ExtractPlayersResponse:
    """Extract fantasy baseball player names from a screenshot image.

    Accepts a base64-encoded image (JPEG, PNG, GIF, or WEBP) and uses
    Claude's vision capability to identify player names visible in the image.
    Returns a list of extracted player name strings.
    """
    import base64

    client = _llm_client()
    if not client:
        raise HTTPException(
            status_code=503,
            detail="AI service not configured. Set ANTHROPIC_API_KEY.",
        )

    try:
        # Validate and decode base64
        image_data = body.image_base64
        # Strip data URL prefix if present
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]

        # Validate it's valid base64
        base64.b64decode(image_data)

        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=500,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": body.image_type,
                                "data": image_data,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                "This is a fantasy sports roster screenshot. "
                                "Extract all player names you can see. "
                                "Return ONLY a JSON array of strings with the player names, nothing else. "
                                'Example: ["Mike Trout", "Shohei Ohtani", "Aaron Judge"]. '
                                "Include only actual player names (First Last format). "
                                "Exclude team names, positions, stats, and column headers."
                            ),
                        },
                    ],
                }
            ],
        )

        text_blocks = [b for b in response.content if b.type == "text"]
        if not text_blocks:
            return ExtractPlayersResponse(player_names=[])

        raw = text_blocks[0].text.strip()

        # Parse JSON array from response
        import json
        import re
        # Find JSON array in the response
        match = re.search(r'\[.*?\]', raw, re.DOTALL)
        if match:
            names = json.loads(match.group())
            player_names = [str(n).strip() for n in names if n and str(n).strip()]
        else:
            player_names = []

        return ExtractPlayersResponse(player_names=player_names)

    except Exception as exc:
        logger.warning("Extract players failed: %s", exc)
        raise HTTPException(
            status_code=422,
            detail=f"Failed to extract players from image: {exc}",
        )
