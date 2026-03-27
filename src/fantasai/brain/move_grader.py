"""Move Grader — grades fantasy baseball transactions A+ through F.

Uses player ranking data and league context to evaluate the quality of
adds, drops, and trades. Calls Claude Haiku for a 2-sentence rationale.
Grade card images are generated separately by grade_card.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from fantasai.models.transaction import Transaction
    from fantasai.models.league import League

_log = logging.getLogger(__name__)

# Grade → numeric score mapping (4.3-scale GPA)
_GRADE_SCORES: dict[str, float] = {
    "A+": 4.3, "A": 4.0, "A-": 3.7,
    "B+": 3.3, "B": 3.0, "B-": 2.7,
    "C+": 2.3, "C": 2.0, "C-": 1.7,
    "D+": 1.3, "D": 1.0, "D-": 0.7,
    "F":  0.0,
}

# Score thresholds for grade boundaries
_SCORE_GRADES = [
    (3.85, "A+"), (3.50, "A"), (3.15, "A-"),
    (2.85, "B+"), (2.50, "B"), (2.15, "B-"),
    (1.85, "C+"), (1.50, "C"), (1.15, "C-"),
    (0.85, "D+"), (0.50, "D"), (0.15, "D-"),
    (-999, "F"),
]


def _score_to_letter(score: float) -> str:
    for threshold, letter in _SCORE_GRADES:
        if score >= threshold:
            return letter
    return "F"


def _get_player_rank(db: "Session", player_id: Optional[int], league_categories: list[str]) -> Optional[int]:
    """Look up the current predictive season rank for a player."""
    if not player_id:
        return None
    try:
        from fantasai.models.ranking import Ranking
        row = (
            db.query(Ranking)
            .filter(
                Ranking.player_id == player_id,
                Ranking.ranking_type == "predictive",
                Ranking.league_id.is_(None),
            )
            .order_by(Ranking.overall_rank)
            .first()
        )
        return row.overall_rank if row else None
    except Exception:
        return None


def _rank_to_value_tier(rank: Optional[int]) -> str:
    """Convert a rank to a descriptive tier string."""
    if rank is None:
        return "unranked"
    if rank <= 30:
        return "elite (top 30)"
    if rank <= 75:
        return "strong (top 75)"
    if rank <= 150:
        return "solid (top 150)"
    if rank <= 250:
        return "fringe (top 250)"
    return "deep/speculative"


def _compute_add_score(
    player_id: Optional[int],
    db: "Session",
    league_categories: list[str],
) -> float:
    """Score an add transaction 0–4.3 based on player's rank."""
    rank = _get_player_rank(db, player_id, league_categories)
    if rank is None:
        return 1.0  # D — unknown player
    if rank <= 20:
        return 4.3   # A+
    if rank <= 40:
        return 4.0   # A
    if rank <= 70:
        return 3.7   # A-
    if rank <= 100:
        return 3.3   # B+
    if rank <= 140:
        return 3.0   # B
    if rank <= 180:
        return 2.7   # B-
    if rank <= 220:
        return 2.3   # C+
    if rank <= 260:
        return 2.0   # C
    if rank <= 300:
        return 1.7   # C-
    if rank <= 350:
        return 1.3   # D+
    return 1.0       # D


def _compute_drop_score(
    player_id: Optional[int],
    db: "Session",
    league_categories: list[str],
) -> float:
    """Score a drop — dropping high-value players is a worse decision."""
    rank = _get_player_rank(db, player_id, league_categories)
    if rank is None:
        return 3.0  # B — dropping unknown player, probably fine
    if rank <= 50:
        return 0.0   # F — never drop a top 50 player
    if rank <= 100:
        return 0.7   # D-
    if rank <= 150:
        return 1.7   # C-
    if rank <= 200:
        return 2.3   # C+
    if rank <= 250:
        return 3.0   # B
    if rank <= 300:
        return 3.7   # A-
    return 4.0       # A — good to drop deep roster filler


def _compute_trade_score(
    participants: list[dict],
    db: "Session",
    league_categories: list[str],
) -> tuple[float, float]:
    """Score both sides of a trade. Returns (side_a_score, side_b_score)."""
    if len(participants) < 2:
        return 2.0, 2.0

    def _side_score(side: dict) -> float:
        gained = side.get("players_added", [])
        lost = side.get("players_dropped", [])
        if not gained and not lost:
            return 2.0

        gain_ranks = [
            _get_player_rank(db, p.get("player_id"), league_categories)
            for p in gained
        ]
        loss_ranks = [
            _get_player_rank(db, p.get("player_id"), league_categories)
            for p in lost
        ]

        avg_gain = (
            sum(r for r in gain_ranks if r is not None) / max(1, sum(1 for r in gain_ranks if r is not None))
            if any(r is not None for r in gain_ranks) else 300
        )
        avg_loss = (
            sum(r for r in loss_ranks if r is not None) / max(1, sum(1 for r in loss_ranks if r is not None))
            if any(r is not None for r in loss_ranks) else 300
        )

        # Better deal = gained better players than you gave up
        delta = avg_loss - avg_gain  # positive = gained better rank (lower number)
        if delta >= 100:
            return 4.3  # A+
        if delta >= 60:
            return 4.0  # A
        if delta >= 30:
            return 3.7  # A-
        if delta >= 10:
            return 3.3  # B+
        if delta >= -10:
            return 3.0  # B — roughly even
        if delta >= -30:
            return 2.3  # C+
        if delta >= -60:
            return 1.7  # C-
        if delta >= -100:
            return 1.0  # D
        return 0.0      # F

    return _side_score(participants[0]), _side_score(participants[1])


def _build_prompt(txn: "Transaction", league: "League", db: "Session") -> str:
    """Build a Claude prompt for the move grade rationale."""
    categories = league.scoring_categories or []
    cat_str = ", ".join(str(c) for c in categories[:8]) if categories else "standard H2H categories"

    txn_type = txn.transaction_type
    participants = txn.participants or []

    if txn_type == "add":
        adds = [p for p in participants if p.get("action") == "add"]
        drops = [p for p in participants if p.get("action") == "drop"]
        player_names = [p.get("player_name", "?") for p in adds]
        drop_names = [p.get("player_name", "?") for p in drops]
        manager = adds[0].get("manager_name", "A manager") if adds else "A manager"

        rank_info = ""
        for p in adds:
            pid = p.get("player_id")
            rank = _get_player_rank(db, pid, categories)
            if rank:
                rank_info += f"\n- {p.get('player_name', '?')}: ranked #{rank} overall"

        prompt = (
            f"Grade this fantasy baseball add in the context of a {cat_str} league.\n\n"
            f"Manager: {manager}\n"
            f"Added: {', '.join(player_names)}"
        )
        if drop_names:
            prompt += f"\nDropped: {', '.join(drop_names)}"
        if rank_info:
            prompt += f"\n\nCurrent rankings:{rank_info}"
        prompt += f"\n\nGrade: {txn.grade_letter}\n\nWrite a 2-sentence verdict on this move. Direct, specific, no hedging."

    elif txn_type == "drop":
        drops = participants
        player_names = [p.get("player_name", "?") for p in drops]
        manager = drops[0].get("manager_name", "A manager") if drops else "A manager"

        rank_info = ""
        for p in drops:
            pid = p.get("player_id")
            rank = _get_player_rank(db, pid, categories)
            if rank:
                rank_info += f"\n- {p.get('player_name', '?')}: ranked #{rank} overall"

        prompt = (
            f"Grade this fantasy baseball drop in the context of a {cat_str} league.\n\n"
            f"Manager: {manager}\n"
            f"Dropped: {', '.join(player_names)}"
        )
        if rank_info:
            prompt += f"\n\nCurrent rankings:{rank_info}"
        prompt += f"\n\nGrade: {txn.grade_letter}\n\nWrite a 2-sentence verdict on this drop. Direct, specific, no hedging."

    else:  # trade
        side_descriptions = []
        for side in participants:
            added = [p.get("player_name", "?") for p in side.get("players_added", [])]
            dropped = [p.get("player_name", "?") for p in side.get("players_dropped", [])]
            mgr = side.get("manager_name", "Manager")
            rank_info = ""
            for p in side.get("players_added", []):
                pid = p.get("player_id")
                rank = _get_player_rank(db, pid, categories)
                if rank:
                    rank_info += f" ({p.get('player_name', '?')} = #{rank})"
            side_descriptions.append(
                f"{mgr} receives: {', '.join(added)}{rank_info} | gives up: {', '.join(dropped)}"
            )

        prompt = (
            f"Grade this fantasy baseball trade in the context of a {cat_str} league.\n\n"
            + "\n".join(side_descriptions)
            + f"\n\nOverall grade: {txn.grade_letter}\n\nWrite a 2-sentence verdict identifying who won this trade and why. Direct, specific, no hedging."
        )

    return prompt


def grade_transaction(
    db: "Session",
    txn: "Transaction",
    league: "League",
) -> None:
    """Grade a transaction in-place: sets grade_letter, grade_score, grade_rationale, graded_at.

    Does NOT commit — caller is responsible for db.commit().
    """
    from fantasai.config import settings

    categories: list[str] = []
    if league.scoring_categories:
        categories = [
            c.get("display_name", c.get("name", "")) if isinstance(c, dict) else str(c)
            for c in league.scoring_categories
        ]

    participants = txn.participants or []
    txn_type = txn.transaction_type

    # Compute grade score
    if txn_type == "add":
        adds = [p for p in participants if p.get("action") == "add"]
        drops = [p for p in participants if p.get("action") == "drop"]
        add_scores = [_compute_add_score(p.get("player_id"), db, categories) for p in adds]
        drop_scores = [_compute_drop_score(p.get("player_id"), db, categories) for p in drops]
        all_scores = add_scores + drop_scores
        grade_score = sum(all_scores) / len(all_scores) if all_scores else 2.0

    elif txn_type == "drop":
        all_scores = [_compute_drop_score(p.get("player_id"), db, categories) for p in participants]
        grade_score = sum(all_scores) / len(all_scores) if all_scores else 2.0

    else:  # trade — grade the first side (combined card shows both)
        score_a, score_b = _compute_trade_score(participants, db, categories)
        # Store the average as the overall grade
        grade_score = (score_a + score_b) / 2
        # Attach per-side grades to participants for the card renderer
        if len(participants) >= 1:
            participants[0]["_grade_score"] = score_a
            participants[0]["_grade_letter"] = _score_to_letter(score_a)
        if len(participants) >= 2:
            participants[1]["_grade_score"] = score_b
            participants[1]["_grade_letter"] = _score_to_letter(score_b)
        txn.participants = participants

    grade_letter = _score_to_letter(grade_score)
    txn.grade_letter = grade_letter
    txn.grade_score = grade_score
    txn.graded_at = datetime.now(tz=timezone.utc)

    # Generate rationale via Claude Haiku
    if settings.anthropic_api_key:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            prompt = _build_prompt(txn, league, db)
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=120,
                system=(
                    "You are a sharp fantasy baseball analyst. Write brief, direct verdicts on "
                    "transactions. No hedging. No filler. Sound like someone who actually knows "
                    "what they're talking about."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
            txn.grade_rationale = response.content[0].text.strip()
        except Exception:
            _log.error("grade_transaction: Claude call failed for txn %s", txn.yahoo_transaction_id, exc_info=True)
            txn.grade_rationale = f"Grade: {grade_letter}. Analysis unavailable."

    # Generate grade card image
    try:
        from fantasai.brain.grade_card import render_grade_card
        card_path = render_grade_card(txn, db)
        if card_path:
            txn.card_image_path = card_path
    except Exception:
        _log.warning("grade_transaction: card render failed for %s", txn.yahoo_transaction_id, exc_info=True)

    _log.info(
        "grade_transaction: %s → %s (%.2f) for %s",
        txn.yahoo_transaction_id, grade_letter, grade_score, txn.transaction_type,
    )
