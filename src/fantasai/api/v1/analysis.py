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

from fantasai.api.deps import get_db
from fantasai.brain.comparator import CompareContext, compare_players
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
    _parse_pros_cons,
    evaluate_trade,
)
from fantasai.config import settings
from fantasai.engine.scoring import PlayerRanking
from fantasai.models.league import Team
from fantasai.models.player import Player
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

# Reuse the shared rankings helper from the recommendations module
from fantasai.api.v1.recommendations import (
    _compute_rankings,
    _fetch_team_and_league,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analysis", tags=["analysis"])

# Default scoring categories used when no league context is provided.
DEFAULT_CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]

# System prompt for analysis-type LLM calls (compare, trade verdict).
# Different persona from per-player blurbs — this is the "analyst making
# a call" rather than "writer describing a player's stats".
_ANALYSIS_SYSTEM_PROMPT = (
    "You are a sharp, concise fantasy baseball analyst. "
    "Write 3–5 sentences max. Be direct — give a verdict and reasoning. "
    "Only reference facts from the DATA BLOCK provided. "
    "Do not hedge excessively. Sound like a confident expert."
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
) -> str:
    """Generate a comparison blurb via Anthropic API.

    Returns empty string on failure — never raises.
    """
    client = _llm_client()
    if not client:
        return ""

    try:

        lines = ["━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━"]
        if context:
            lines.append(f"User context: {context}")
        lines.append(f"Scoring categories: {', '.join(categories)}")
        lines.append("")

        for p in ranked_players:
            signals = ", ".join(
                f"{cat}: {score:+.1f}"
                for cat, score in sorted(
                    p.category_scores.items(), key=lambda x: -abs(x[1])
                )[:5]
            )
            lines.append(
                f"#{p.rank} {p.player_name} ({'/'.join(p.positions)}, "
                f"{p.team}) — adjusted score: {p.composite_score:.2f} | {signals}"
            )

        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")
        lines.append(
            "Write a head-to-head comparison blurb. "
            "Clearly state who wins and why, referencing the stats."
            + (f" Address the user's context: {context}" if context else "")
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
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
) -> tuple[str, list[str], list[str]]:
    """Generate a trade verdict blurb with [PROS]/[CONS] via Anthropic API.

    Returns (blurb, pros, cons). Falls back to empty strings / algorithmic
    pros/cons on failure.
    """
    client = _llm_client()
    if not client:
        return "", evaluation.pros, evaluation.cons

    try:
        def _side_summary(players: list[PlayerRanking], picks: list[str]) -> str:
            parts = []
            for p in players:
                top_cats = ", ".join(
                    f"{cat}: {p.category_contributions.get(cat, 0):+.1f}"
                    for cat in sorted(categories, key=lambda c: -abs(p.category_contributions.get(c, 0)))[:4]
                )
                parts.append(f"  {p.name} ({'/'.join(p.positions)}, score {p.score:.1f}) | {top_cats}")
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
            max_tokens=400,
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
) -> str:
    """Generate a 'why now' framing blurb for a find-player suggestion."""
    from fantasai.brain.blurb_generator import get_blurb_generator

    if not settings.anthropic_api_key:
        return ""

    try:
        gen = get_blurb_generator(api_key=settings.anthropic_api_key)
        return gen.generate_blurb(
            ranking=ranking,
            ranking_type="predictive",
            scoring_categories=categories,
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

    lookback, predictive = _compute_rankings(db, categories)
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

    # Determine which categories were boosted by context (for transparency)
    context_applied: Optional[str] = None
    if body.context:
        from fantasai.brain.comparator import _parse_context_keywords
        boosted = _parse_context_keywords(body.context, categories)
        if boosted:
            context_applied = f"Boosted categories: {', '.join(sorted(boosted))}"

    # Generate LLM blurb
    blurb = _generate_compare_blurb(results, categories, body.context)

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

    lookback, predictive = _compute_rankings(db, categories)
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
    blurb, pros, cons = _generate_trade_blurb_and_pros_cons(
        evaluation=evaluation,
        giving_rankings=giving_rankings,
        receiving_rankings=receiving_rankings,
        giving_picks=body.giving.draft_picks,
        receiving_picks=body.receiving.draft_picks,
        categories=categories,
        has_keepers=has_keepers,
        context=body.context,
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
) -> FindPlayerResponse:
    """Find the best available player for a given roster slot.

    Tracks suggestion history for this team + position so that repeated
    calls always return a fresh (previously unseen) suggestion. History
    is stored in the Recommendation table with rec_type='find_player_{slot}'.

    Pass ``extra_exclude_ids`` to manually exclude additional players.
    """
    team, league = _fetch_team_and_league(body.team_id, db)
    categories = league.scoring_categories or DEFAULT_CATEGORIES

    rec_type = f"find_player_{body.position_slot}"

    # Load previous suggestions for this team + position
    prev_recs = (
        db.query(Recommendation)
        .filter(
            Recommendation.team_id == body.team_id,
            Recommendation.rec_type == rec_type,
        )
        .order_by(Recommendation.created_at.desc())
        .all()
    )
    seen_player_ids: set[int] = {r.player_id for r in prev_recs}

    # Build all-excluded set: league rostered + previously seen + manual excludes
    all_rostered: set[int] = set()
    for t in league.teams:
        all_rostered.update(t.roster or [])

    all_excluded = all_rostered | seen_player_ids | set(body.extra_exclude_ids)

    # Compute rankings
    lookback, predictive = _compute_rankings(db, categories)
    if not lookback:
        raise HTTPException(
            status_code=404,
            detail="No player stats available.",
        )

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
        all_rostered_ids=all_excluded,
        build_preferences=BuildPreferences(),
    )

    recommender = Recommender(categories, league_type=league.league_type)
    # Get more than 1 in case some don't match the target position
    recommendations = recommender.get_waiver_recommendations(ctx, limit=50)

    # Filter to players who can fill the target position slot
    position_recs = [
        r for r in recommendations
        if _player_eligible_for_slot(r.positions, body.position_slot)
    ]

    if not position_recs:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No available players found for position '{body.position_slot}'. "
                "All suggestions may have been exhausted — try resetting history "
                "or check that players are available."
            ),
        )

    best = position_recs[0]

    # Look up predictive ranking for blurb generation
    pred_map = {r.player_id: r for r in (predictive or lookback)}
    pred_ranking = pred_map.get(best.player_id)

    # Generate blurb
    blurb = ""
    if pred_ranking:
        blurb = _generate_find_player_blurb(pred_ranking, categories, body.position_slot)

    # Persist the new suggestion
    new_rec = Recommendation(
        team_id=body.team_id,
        rec_type=rec_type,
        player_id=best.player_id,
        action=best.action,
        rationale_blurb=blurb or best.rationale_blurb,
        category_impact=best.category_impact,
        priority_score=best.priority_score,
        created_at=datetime.now(timezone.utc),
        expires_at=None,
    )
    db.add(new_rec)
    db.commit()
    db.refresh(new_rec)

    # Build suggestion read objects
    def _to_suggestion(rec: Recommendation, positions: list[str], cat_impact: dict) -> FindPlayerSuggestionRead:
        return FindPlayerSuggestionRead(
            player_id=rec.player_id,
            player_name=_get_player_name(db, rec.player_id),
            positions=positions,
            priority_score=rec.priority_score,
            category_impact=cat_impact,
            blurb=rec.rationale_blurb,
            created_at=rec.created_at,
        )

    # Build a position lookup for historical recommendations
    # (we need positions for each previously seen player)
    all_recs = db.query(Recommendation).filter(
        Recommendation.team_id == body.team_id,
        Recommendation.rec_type == rec_type,
    ).order_by(Recommendation.created_at.desc()).all()

    lookback_map = {r.player_id: r for r in lookback}
    pred_map_full = {r.player_id: r for r in (predictive or lookback)}

    all_suggestions = []
    for rec in all_recs:
        ranking = pred_map_full.get(rec.player_id) or lookback_map.get(rec.player_id)
        positions = ranking.positions if ranking else []
        cat_impact = rec.category_impact or {}
        all_suggestions.append(_to_suggestion(rec, positions, cat_impact))

    # Current suggestion is the newly-persisted record
    current_suggestion = FindPlayerSuggestionRead(
        player_id=best.player_id,
        player_name=best.player_name,
        positions=best.positions,
        priority_score=best.priority_score,
        category_impact=best.category_impact,
        blurb=blurb or best.rationale_blurb,
        created_at=new_rec.created_at,
    )

    return FindPlayerResponse(
        suggestion=current_suggestion,
        all_suggestions=all_suggestions,
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
        lines.append(f"Strong categories: {', '.join(evaluation.strong_categories) or 'none'}")
        lines.append(f"Weak categories: {', '.join(evaluation.weak_categories) or 'none'}")
        lines.append("")
        lines.append("Position breakdown (score | assessment):")
        for g in evaluation.position_breakdown[:8]:
            lines.append(f"  {g.position}: {g.group_score:.2f} — {g.assessment} ({', '.join(g.players[:3])})")
        lines.append("")
        lines.append("Improvement suggestions:")
        for s in evaluation.improvement_suggestions[:4]:
            lines.append(f"  - {s}")
        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")
        lines.append(
            "Write a 3–5 sentence team evaluation. State the grade, what the team "
            "does well, where they're vulnerable, and one key improvement priority. "
            + (f"Address user context: {context}" if context else "")
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
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
        lines.append(f"Keepers ({len(evaluation.keepers)}): {', '.join(r.name for r in evaluation.keepers[:6])}")
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
                "Mention the strongest keeper(s), the biggest gap(s) to fill, and the #1 draft priority."
            )
        else:
            instruction = (
                "Write 3–5 sentences evaluating this keeper core's strengths, weaknesses, "
                "and most important draft target profiles."
            )
        if context:
            instruction += f" Address user context: {context}"

        lines.append(instruction)

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
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
) -> str:
    """Generate a multi-team comparison narrative via Anthropic API."""
    client = _llm_client()
    if not client:
        return ""

    try:
        lines = ["━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━"]
        if context:
            lines.append(f"User context: {context}")
        lines.append("Team comparison (ranked by power score):")
        for snap in comparison.snapshots:
            lines.append(
                f"  {snap.team_name} (id={snap.team_id}): power={snap.power_score:.2f} | "
                f"strong={', '.join(snap.strong_cats[:3])} | "
                f"weak={', '.join(snap.weak_cats[:3])} | "
                f"top players: {', '.join(snap.top_players[:2])}"
            )
        if comparison.trade_opportunities:
            lines.append("")
            lines.append("Trade opportunities:")
            for opp in comparison.trade_opportunities[:3]:
                lines.append(f"  {opp.rationale}")
        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")
        lines.append(
            "Write a 3–5 sentence comparison. Identify the strongest team and why, "
            "each team's key advantage/disadvantage, and the most interesting trade angle. "
            + (f"Address user context: {context}" if context else "")
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=350,
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
) -> str:
    """Generate a league power rankings narrative via Anthropic API."""
    client = _llm_client()
    if not client:
        return ""

    try:
        lines = ["━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━"]
        lines.append("League Power Rankings:")
        for i, snap in enumerate(report.power_rankings[:10], 1):
            lines.append(
                f"  #{i} {snap.team_name} (power={snap.power_score:.2f}) | "
                f"strong={', '.join(snap.strong_cats[:3])}"
            )
        lines.append("")
        lines.append("Tiers:")
        for tier, ids in tiers.items():
            names = [team_names.get(tid, str(tid)) for tid in ids]
            lines.append(f"  {tier.capitalize()}: {', '.join(names)}")
        if report.trade_opportunities:
            lines.append("")
            lines.append("Top trade opportunities:")
            for opp in report.trade_opportunities[:3]:
                lines.append(f"  {opp.rationale}")
        lines.append("━━━ END DATA BLOCK ━━━")
        lines.append("")
        lines.append(
            "Write a 4–6 sentence league power rankings summary. Comment on who's dominating, "
            "which teams are on the bubble, and highlight 1–2 interesting trade pairings."
        )

        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=400,
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

    ranking_map = {r.player_id: r for r in source}

    # Build roster rankings
    if body.team_id:
        team = db.get(Team, body.team_id)
        if not team:
            raise HTTPException(status_code=404, detail=f"Team {body.team_id} not found.")
        player_ids = team.roster or []
    else:
        player_ids = body.player_ids or []

    roster_rankings = [ranking_map[pid] for pid in player_ids if pid in ranking_map]

    # Optionally gather all teams' scores for league-relative grading
    league_team_scores: Optional[list[float]] = None
    if league:
        all_teams = league.teams or []
        league_team_scores = []
        for t in all_teams:
            t_rankings = [ranking_map[pid] for pid in (t.roster or []) if pid in ranking_map]
            if t_rankings:
                t_score = sum(r.score for r in t_rankings) / len(t_rankings)
                league_team_scores.append(t_score)

    evaluation = evaluate_team(
        roster_rankings=roster_rankings,
        categories=categories,
        roster_positions=roster_positions,
        league_type=league_type,
        league_team_scores=league_team_scores if league_team_scores else None,
        context=body.context,
    )

    blurb = _generate_team_eval_blurb(evaluation, categories, body.context)

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

    # Compute rankings
    lookback, predictive = _compute_rankings(db, categories)
    source = predictive or lookback
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

    # Run the appropriate brain function
    if body.mode == "plan_keepers":
        evaluation = plan_keepers(
            full_roster_rankings=input_rankings,
            n_keepers=body.n_keepers,
            categories=categories,
            roster_positions=roster_positions,
            league_type=league_type,
            available_pool=available_pool,
            player_ages={},  # future: populate from Player.birth_date
            context=body.context,
        )
    else:
        evaluation = evaluate_keepers(
            keeper_rankings=input_rankings,
            categories=categories,
            roster_positions=roster_positions,
            league_type=league_type,
            available_pool=available_pool,
            context=body.context,
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

    # Build team data tuples
    team_data: list[tuple[int, str, list[PlayerRanking]]] = []

    if body.manual_teams:
        # Build from manual teams using sequential negative fake IDs
        for i, mt in enumerate(body.manual_teams):
            fake_id = -(i + 1)
            roster_rankings = [ranking_map[pid] for pid in mt.player_ids if pid in ranking_map]
            team_data.append((fake_id, mt.name, roster_rankings))
    else:
        for tid in (body.team_ids or []):
            team = db.get(Team, tid)
            if not team:
                raise HTTPException(status_code=404, detail=f"Team {tid} not found.")
            roster_rankings = [ranking_map[pid] for pid in (team.roster or []) if pid in ranking_map]
            team_data.append((tid, team.name or f"Team {tid}", roster_rankings))

    if len(team_data) < 2:
        raise HTTPException(status_code=422, detail="At least 2 valid teams are required.")

    comparison = compare_teams(
        team_data=team_data,
        categories=categories,
        league_type=league_type,
        include_trades=body.include_trade_suggestions,
    )

    blurb = _generate_compare_teams_blurb(comparison, body.context)

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
    league_id: int,
    db: Session = Depends(get_db),
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
    for team in league.teams or []:
        roster_rankings = [ranking_map[pid] for pid in (team.roster or []) if pid in ranking_map]
        name = team.name or f"Team {team.id}"
        team_data.append((team.id, name, roster_rankings))
        team_names[team.id] = name

    if not team_data:
        raise HTTPException(status_code=404, detail=f"No teams found for league {league_id}.")

    report = compute_league_power(
        team_data=team_data,
        categories=categories,
        league_type=league_type,
    )

    blurb = _generate_league_power_blurb(report, report.tiers, team_names)

    return LeaguePowerResponse(
        power_rankings=[_snapshot_to_read(s) for s in report.power_rankings],
        tiers=report.tiers,
        trade_opportunities=[_trade_opp_to_read(o) for o in report.trade_opportunities],
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
