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
    """Look up the rest-of-season predictive rank for a player.

    Uses RankingSnapshot (horizon='season') so the Move Grader always uses
    season-long value rather than a week-specific rank.  Falls back to the
    Ranking table if no snapshot exists yet.
    """
    if not player_id:
        return None
    try:
        from fantasai.models.ranking import Ranking, RankingSnapshot

        # Prefer the most recent season-horizon snapshot — this is stable,
        # week-agnostic, and never confused with a short-term "hot week" rank.
        snapshot = (
            db.query(RankingSnapshot)
            .filter(
                RankingSnapshot.player_id == player_id,
                RankingSnapshot.ranking_type == "predictive",
                RankingSnapshot.horizon == "season",
            )
            .order_by(RankingSnapshot.snapshot_date.desc())
            .first()
        )
        if snapshot:
            return snapshot.overall_rank

        # Fall back to Ranking table (may be any horizon — still better than nothing)
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


def _get_player_facts(db: "Session", player_id: Optional[int], player_name: str) -> str:
    """Return a verified-facts string for a player from our live DB.

    Includes team, positions, injury status, and a few key current-season stats.
    This is injected verbatim into the prompt so Claude uses DB data instead
    of its training knowledge (which may have stale/wrong team assignments).
    """
    if not player_id:
        return f"{player_name}: (no DB record — use name only, state no team or stats)"
    try:
        from fantasai.models.player import Player, PlayerStats
        player = db.get(Player, player_id)
        if not player:
            return f"{player_name}: (no DB record — use name only, state no team or stats)"

        parts: list[str] = []
        if player.team:
            parts.append(f"team={player.team}")
        if player.positions:
            parts.append(f"positions={'/'.join(player.positions[:3])}")
        if player.status and player.status.upper() not in ("", "ACTIVE", "ACT"):
            parts.append(f"status={player.status}")

        # Fetch current-season stats (prefer actual over projection)
        stats_rows = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == player_id,
                PlayerStats.season == 2026,
                PlayerStats.week.is_(None),
            )
            .all()
        )
        # Pick actual first, fall back to projection
        stats_row = None
        for row in stats_rows:
            if row.data_source == "actual":
                stats_row = row
                break
        if stats_row is None and stats_rows:
            stats_row = stats_rows[0]

        if stats_row:
            rate = stats_row.rate_stats or {}
            adv = stats_row.advanced_stats or {}
            if stats_row.stat_type == "pitching":
                for k in ["ERA", "WHIP", "K/9", "K9", "SV"]:
                    v = rate.get(k)
                    if v is not None:
                        try:
                            parts.append(f"{k}={float(v):.2f}")
                        except (TypeError, ValueError):
                            pass
                for k in ["xERA", "xFIP", "SIERA"]:
                    v = adv.get(k)
                    if v is not None:
                        try:
                            parts.append(f"{k}={float(v):.2f}")
                        except (TypeError, ValueError):
                            pass
            else:
                for k in ["AVG", "OBP", "SLG"]:
                    v = rate.get(k)
                    if v is not None:
                        try:
                            parts.append(f"{k}={float(v):.3f}")
                        except (TypeError, ValueError):
                            pass
                for k in ["HR", "SB", "R", "RBI"]:
                    v = (stats_row.counting_stats or {}).get(k)
                    if v is not None:
                        try:
                            parts.append(f"{k}={int(float(v))}")
                        except (TypeError, ValueError):
                            pass

        return f"{player_name}: {', '.join(parts)}" if parts else player_name
    except Exception:
        return player_name


def _league_format_str(league: "League") -> str:
    """Return a human-readable league format string."""
    lt = (league.league_type or "").lower()
    if "h2h" in lt or "head" in lt:
        return "H2H categories"
    if "roto" in lt:
        return "rotisserie"
    if "point" in lt:
        return "points"
    return "H2H categories"  # safe default


def _build_prompt(txn: "Transaction", league: "League", db: "Session") -> str:
    """Build a Claude prompt for the move grade rationale."""
    categories = league.scoring_categories or []
    cat_str = ", ".join(str(c) for c in categories[:8]) if categories else "H/AB, R, HR, RBI, SB, AVG, OPS, IP"
    league_format = _league_format_str(league)

    txn_type = txn.transaction_type
    participants = txn.participants or []

    # ── Shared data header injected into every prompt ────────────────────────
    # Providing verified DB facts prevents Claude from hallucinating stale
    # team names, injuries, or league types from its training data.
    data_block_lines: list[str] = [
        f"LEAGUE FORMAT: {league_format}",
        f"SCORING CATEGORIES: {cat_str}",
        "PLAYER DATA (live DB — authoritative; ignore any conflicting training knowledge):",
    ]

    # Collect all relevant player IDs from this transaction
    players_to_lookup: list[tuple[Optional[int], str]] = []
    if txn_type in ("add", "drop"):
        for p in participants:
            players_to_lookup.append((p.get("player_id"), p.get("player_name", "?")))
    else:  # trade
        for side in participants:
            for p in side.get("players_added", []) + side.get("players_dropped", []):
                players_to_lookup.append((p.get("player_id"), p.get("player_name", "?")))

    seen_ids: set = set()
    for pid, pname in players_to_lookup:
        key = pid or pname
        if key in seen_ids:
            continue
        seen_ids.add(key)
        rank = _get_player_rank(db, pid, categories)
        rank_str = f" | rank=#{rank}" if rank else ""
        facts = _get_player_facts(db, pid, pname)
        data_block_lines.append(f"  - {facts}{rank_str}")

    data_block = "\n".join(data_block_lines)

    # ── Per-type prompt bodies ────────────────────────────────────────────────
    if txn_type == "add":
        adds = [p for p in participants if p.get("action") == "add"]
        drops = [p for p in participants if p.get("action") == "drop"]
        manager = adds[0].get("manager_name", "A manager") if adds else "A manager"
        added_names = ", ".join(p.get("player_name", "?") for p in adds)
        drop_line = ""
        if drops:
            drop_line = f"\nDropped: {', '.join(p.get('player_name', '?') for p in drops)}"

        prompt = (
            f"{data_block}\n\n"
            f"TRANSACTION: {manager} adds {added_names}{drop_line}\n"
            f"GRADE: {txn.grade_letter}\n\n"
            f"Write a 2-sentence verdict on this add. "
            f"Use ONLY the player data above — never cite team, stats, or injuries "
            f"not listed there. Direct, specific, no hedging."
        )

    elif txn_type == "drop":
        manager = participants[0].get("manager_name", "A manager") if participants else "A manager"
        drop_names = ", ".join(p.get("player_name", "?") for p in participants)

        prompt = (
            f"{data_block}\n\n"
            f"TRANSACTION: {manager} drops {drop_names}\n"
            f"GRADE: {txn.grade_letter}\n\n"
            f"Write a 2-sentence verdict on this drop. "
            f"Use ONLY the player data above — never cite team, stats, or injuries "
            f"not listed there. Direct, specific, no hedging."
        )

    else:  # trade
        side_lines: list[str] = []
        for side in participants:
            added = ", ".join(p.get("player_name", "?") for p in side.get("players_added", []))
            dropped = ", ".join(p.get("player_name", "?") for p in side.get("players_dropped", []))
            mgr = side.get("manager_name", "Manager")
            side_lines.append(f"{mgr} receives: {added} | gives up: {dropped}")

        prompt = (
            f"{data_block}\n\n"
            f"TRANSACTION (trade):\n" + "\n".join(side_lines) + "\n"
            f"OVERALL GRADE: {txn.grade_letter}\n\n"
            f"Write a 2-sentence verdict identifying who won this trade and why. "
            f"Use ONLY the player data above — never cite team, stats, or injuries "
            f"not listed there. Direct, specific, no hedging."
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
                    "transactions. No hedging. No filler.\n\n"
                    "CRITICAL RULES:\n"
                    "1. Use ONLY the player data, team names, stats, and league format provided "
                    "in the prompt. Your training knowledge about players is outdated — the "
                    "provided data is authoritative.\n"
                    "2. Never mention injuries, surgeries, or health history unless explicitly "
                    "listed in the provided player data.\n"
                    "3. Never reference a league format (points, roto, etc.) other than the one "
                    "stated in the prompt's LEAGUE FORMAT field.\n"
                    "4. If a player's team is listed, use that team. Never substitute a different team."
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
