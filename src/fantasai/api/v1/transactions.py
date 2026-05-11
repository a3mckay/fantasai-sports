"""Transactions API — move grades, feed, and grade card serving."""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasai.api.deps import get_current_user, get_db
from fantasai.models.league import League, Team
from fantasai.models.transaction import Transaction

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/transactions", tags=["transactions"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class TransactionRead(BaseModel):
    id: int
    yahoo_transaction_id: str
    league_id: str
    transaction_type: str          # add | drop | trade
    participants: list
    grade_letter: Optional[str]
    grade_score: Optional[float]
    grade_rationale: Optional[str]
    card_image_path: Optional[str]
    share_token: str
    yahoo_timestamp: Optional[str]
    graded_at: Optional[str]
    has_card: bool
    lookback_grade_letter: Optional[str]
    lookback_grade_score: Optional[float]
    lookback_grade_rationale: Optional[str]
    lookback_graded_at: Optional[str]

    model_config = {"from_attributes": True}


def _enrich_participants(
    participants: list,
    positions_map: dict[int, list[str]],
) -> list:
    """Inject player positions into each participant entry.

    For add/drop entries the player is at the top level (player_id, player_name).
    For trade entries players are nested under players_added / players_dropped.
    Returns a new list — originals are not mutated.
    """
    import copy
    enriched = []
    for p in participants:
        p = copy.copy(p)
        # add / drop
        pid = p.get("player_id")
        if pid and pid in positions_map:
            p["positions"] = positions_map[pid]
        # trade nested lists
        for key in ("players_added", "players_dropped"):
            if p.get(key):
                new_list = []
                for entry in p[key]:
                    entry = copy.copy(entry)
                    epid = entry.get("player_id")
                    if epid and epid in positions_map:
                        entry["positions"] = positions_map[epid]
                    new_list.append(entry)
                p[key] = new_list
        enriched.append(p)
    return enriched


def _txn_to_read(txn: Transaction, positions_map: Optional[dict[int, list[str]]] = None) -> TransactionRead:
    participants = txn.participants or []
    if positions_map:
        participants = _enrich_participants(participants, positions_map)
    return TransactionRead(
        id=txn.id,
        yahoo_transaction_id=txn.yahoo_transaction_id,
        league_id=txn.league_id,
        transaction_type=txn.transaction_type,
        participants=participants,
        grade_letter=txn.grade_letter,
        grade_score=txn.grade_score,
        grade_rationale=txn.grade_rationale,
        card_image_path=txn.card_image_path,
        share_token=txn.share_token,
        yahoo_timestamp=txn.yahoo_timestamp.isoformat() if txn.yahoo_timestamp else None,
        graded_at=txn.graded_at.isoformat() if txn.graded_at else None,
        has_card=bool(txn.card_image_path and os.path.exists(txn.card_image_path)),
        lookback_grade_letter=txn.lookback_grade_letter,
        lookback_grade_score=txn.lookback_grade_score,
        lookback_grade_rationale=txn.lookback_grade_rationale,
        lookback_graded_at=txn.lookback_graded_at.isoformat() if txn.lookback_graded_at else None,
    )


def _build_positions_map(txns: list[Transaction], db: Session) -> dict[int, list[str]]:
    """Batch-fetch player positions for all player_ids appearing in a list of transactions.

    Returns {player_id: [positions]} so callers can inject positions without N+1 queries.
    """
    from fantasai.models.player import Player

    player_ids: set[int] = set()
    for txn in txns:
        for p in txn.participants or []:
            if p.get("player_id"):
                player_ids.add(int(p["player_id"]))
            for key in ("players_added", "players_dropped"):
                for entry in p.get(key) or []:
                    if entry.get("player_id"):
                        player_ids.add(int(entry["player_id"]))

    if not player_ids:
        return {}

    rows = db.query(Player.player_id, Player.positions).filter(Player.player_id.in_(player_ids)).all()
    return {row.player_id: (row.positions or []) for row in rows}


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/debug-status")
def debug_status(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Diagnostic endpoint — returns raw DB state to help debug empty Move Grades.

    Shows total transaction count (all leagues), the user's current league_key,
    and the count of transactions for that league specifically.
    """
    from sqlalchemy import func, text
    from fantasai.models.user import YahooConnection

    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    league_key = conn.league_key if conn else None

    total_count = db.query(func.count(Transaction.id)).scalar()
    league_count = (
        db.query(func.count(Transaction.id))
        .filter(Transaction.league_id == league_key)
        .scalar()
        if league_key else 0
    )
    distinct_leagues = (
        db.query(Transaction.league_id)
        .distinct()
        .all()
    )

    return {
        "user_id": user.id,
        "conn_league_key": league_key,
        "total_transactions_in_db": total_count,
        "transactions_for_this_league": league_count,
        "distinct_league_ids_in_db": [r[0] for r in distinct_leagues],
    }


@router.get("", response_model=list[TransactionRead])
def list_transactions(
    transaction_type: Optional[str] = Query(default=None, pattern="^(add|drop|trade)$"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Return the transaction feed for the user's active league, newest first."""
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or not conn.league_key:
        return []

    q = db.query(Transaction).filter(Transaction.league_id == conn.league_key)
    if transaction_type:
        q = q.filter(Transaction.transaction_type == transaction_type)
    q = q.order_by(Transaction.yahoo_timestamp.desc().nullslast(), Transaction.created_at.desc())
    txns = q.offset(offset).limit(limit).all()
    pos_map = _build_positions_map(txns, db)
    return [_txn_to_read(t, pos_map) for t in txns]


@router.get("/watermark")
def get_ticker_watermark(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Return the current max graded transaction id for this league.

    The ticker calls this on first mount to initialize its since_id so that
    historical (backfilled) transactions never appear.
    """
    from sqlalchemy import func
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or not conn.league_key:
        return {"max_id": 0}

    max_id = (
        db.query(func.max(Transaction.id))
        .filter(Transaction.league_id == conn.league_key)
        .scalar()
    ) or 0
    return {"max_id": max_id}


@router.get("/unseen", response_model=list[TransactionRead])
def list_unseen_transactions(
    since_id: Optional[int] = Query(default=None, description="Return transactions with id > since_id"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Return graded, non-backfill transactions newer than since_id (for the ticker)."""
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or not conn.league_key:
        return []

    q = (
        db.query(Transaction)
        .filter(
            Transaction.league_id == conn.league_key,
            Transaction.grade_letter.isnot(None),
            Transaction.is_backfill.is_(False),
        )
    )
    if since_id:
        q = q.filter(Transaction.id > since_id)
    q = q.order_by(Transaction.id.desc()).limit(20)
    txns = q.all()
    pos_map = _build_positions_map(txns, db)
    return [_txn_to_read(t, pos_map) for t in txns]


@router.get("/{transaction_id}", response_model=TransactionRead)
def get_transaction(
    transaction_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Get a single transaction by ID."""
    txn = db.get(Transaction, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    # Verify user has access to this league
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or conn.league_key != txn.league_id:
        raise HTTPException(status_code=403, detail="Access denied")
    pos_map = _build_positions_map([txn], db)
    return _txn_to_read(txn, pos_map)


def _resolve_card_path(txn: Transaction, side: Optional[int], db: Session) -> Optional[str]:
    """Return the appropriate card image path, regenerating if missing.

    For trade transactions, `side` selects a per-side card (0 or 1).
    For non-trade or side=None, falls back to txn.card_image_path.
    """
    # Per-side trade card
    if side is not None and txn.transaction_type == "trade":
        participants = txn.participants or []
        if side < len(participants):
            path = participants[side].get("_card_image_path")
            if path and os.path.exists(path):
                return path
            # Regenerate both side cards
            try:
                from fantasai.brain.grade_card import render_trade_side_cards
                side_paths = render_trade_side_cards(txn, db)
                for i, p in enumerate(side_paths):
                    if i < len(participants):
                        participants[i]["_card_image_path"] = p
                txn.participants = participants
                if side_paths:
                    txn.card_image_path = side_paths[0]
                db.commit()
                return side_paths[side] if side < len(side_paths) else None
            except Exception:
                return None

    # Default: txn-level card
    if txn.card_image_path and os.path.exists(txn.card_image_path):
        return txn.card_image_path
    try:
        from fantasai.brain.grade_card import render_grade_card, render_trade_side_cards
        if txn.transaction_type == "trade":
            side_paths = render_trade_side_cards(txn, db)
            participants = txn.participants or []
            for i, p in enumerate(side_paths):
                if i < len(participants):
                    participants[i]["_card_image_path"] = p
            txn.participants = participants
            txn.card_image_path = side_paths[0] if side_paths else None
        else:
            card_path = render_grade_card(txn, db)
            txn.card_image_path = card_path
        db.commit()
    except Exception:
        pass
    return txn.card_image_path if txn.card_image_path and os.path.exists(txn.card_image_path) else None


@router.get("/{transaction_id}/card")
def get_grade_card_image(
    transaction_id: int,
    side: Optional[int] = Query(default=None, ge=0, le=1, description="Trade side (0 or 1); omit for default card"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Serve the grade card PNG for a transaction.

    For trade transactions, use ?side=0 or ?side=1 to get each team's individual card.
    """
    txn = db.get(Transaction, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or conn.league_key != txn.league_id:
        raise HTTPException(status_code=403, detail="Access denied")

    card_path = _resolve_card_path(txn, side, db)
    if not card_path:
        raise HTTPException(status_code=404, detail="Grade card could not be generated")

    side_suffix = f"_side{side}" if side is not None else ""
    filename = f"grade_{txn.transaction_type}_{txn.grade_letter}{side_suffix}_{txn.share_token[:8]}.png"
    return FileResponse(
        card_path,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/share/{share_token}")
def get_shared_card(
    share_token: str,
    side: Optional[int] = Query(default=None, ge=0, le=1, description="Trade side (0 or 1)"),
    db: Session = Depends(get_db),
):
    """Public endpoint — serves a grade card PNG by share token (no auth required).

    Used by the frontend Share button so recipients don't need to be logged in.
    For trades, use ?side=0 or ?side=1 to serve each team's card.
    """
    txn = (
        db.query(Transaction)
        .filter(Transaction.share_token == share_token)
        .first()
    )
    if not txn:
        raise HTTPException(status_code=404, detail="Grade card not found")

    card_path = _resolve_card_path(txn, side, db)
    if not card_path:
        raise HTTPException(status_code=404, detail="Grade card could not be generated")

    side_suffix = f"_side{side}" if side is not None else ""
    filename = f"grade_{txn.transaction_type}_{txn.grade_letter}{side_suffix}_{txn.share_token[:8]}.png"
    return FileResponse(
        card_path,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/{transaction_id}/regrade", status_code=200)
def regrade_single_transaction(
    transaction_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Re-grade a single transaction in-place and return the updated record."""
    txn = db.get(Transaction, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or conn.league_key != txn.league_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Reset grade + stored rank snapshots so grading runs fresh
    txn.grade_letter = None
    txn.grade_score = None
    txn.grade_rationale = None
    txn.graded_at = None
    txn.card_image_path = None
    # Clear stored rank snapshots so live rank is re-captured at this moment
    _parts = txn.participants or []
    for p in _parts:
        p.pop("_ros_rank_at_grade", None)
        for key in ("players_added", "players_dropped"):
            for entry in p.get(key, []):
                entry.pop("_ros_rank_at_grade", None)
        p.pop("_grade_rationale", None)
        p.pop("_card_image_path", None)
    txn.participants = _parts
    db.commit()

    try:
        from fantasai.config import settings
        from fantasai.models.league import League
        from fantasai.brain.move_grader import grade_transaction

        league = db.query(League).filter(League.league_id == txn.league_id).first()
        if not league:
            raise HTTPException(status_code=404, detail="League not found")
        grade_transaction(db, txn, league)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("regrade_single_transaction: failed for txn %d: %s", transaction_id, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Re-grade failed: {exc}")

    pos_map = _build_positions_map([txn], db)
    return _txn_to_read(txn, pos_map)


@router.post("/regrade", status_code=202)
def regrade_transactions(
    background_tasks: BackgroundTasks,
    transaction_type: Optional[str] = Query(
        default=None,
        description="Limit to a single transaction type (add | drop | trade). Omit to regrade all.",
    ),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Re-grade existing transactions for the user's league.

    Clears grade_letter/grade_rationale/card_image_path on matching transactions
    so the next poll cycle regenerates them with the current grader logic.
    Pass `transaction_type=trade` to backfill only trade rationales (e.g. after
    a per-side prompt change) without disturbing add/drop grades.
    Runs in the background — takes ~30s per 25 transactions.
    """
    from fantasai.models.user import YahooConnection

    if transaction_type and transaction_type not in ("add", "drop", "trade"):
        raise HTTPException(status_code=400, detail="transaction_type must be add, drop, or trade")

    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or not conn.league_key:
        return {"status": "no_league", "message": "No active league found"}

    league_id = conn.league_key

    def _do_regrade(lid: str, txn_type: Optional[str]) -> None:
        try:
            from fantasai.config import settings
            from fantasai.database import SessionLocal
            from fantasai.models.league import League
            from fantasai.models.transaction import Transaction
            from fantasai.services.yahoo_transactions import _grade_ungraded

            _db = SessionLocal()
            try:
                # Reset matching grades so _grade_ungraded picks them up
                reset_q = _db.query(Transaction).filter(
                    Transaction.league_id == lid,
                    Transaction.grade_letter.isnot(None),
                )
                if txn_type:
                    reset_q = reset_q.filter(Transaction.transaction_type == txn_type)
                reset_q.update(
                    {
                        "grade_letter": None,
                        "grade_score": None,
                        "grade_rationale": None,
                        "graded_at": None,
                        "card_image_path": None,
                    },
                    synchronize_session=False,
                )
                _db.commit()

                league = _db.query(League).filter(League.league_id == lid).first()
                if league:
                    # Grade in batches of 50 until done
                    while True:
                        before = (
                            _db.query(Transaction)
                            .filter(
                                Transaction.league_id == lid,
                                Transaction.grade_letter.is_(None),
                            )
                            .count()
                        )
                        if before == 0:
                            break
                        _grade_ungraded(_db, lid, league)
            finally:
                _db.close()
        except Exception:
            logger.error("regrade_transactions: failed for league %s", lid, exc_info=True)

    background_tasks.add_task(_do_regrade, league_id, transaction_type)
    scope = transaction_type or "all"
    return {"status": "regrading", "message": f"Re-grading {scope} transactions in background"}


@router.post("/poll")
def poll_transactions(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Manually trigger a transaction poll and return immediately with result count.

    Runs synchronously so the frontend knows whether the poll actually found
    transactions (new_count > 0) or failed (raises 502).  Grading still happens
    in-process but the stored-transaction count is returned right away so the
    UI can reload without guessing how long to wait.
    """
    from fantasai.services.yahoo_transactions import poll_all_leagues
    try:
        new_count = poll_all_leagues()
    except Exception as exc:
        logger.error("poll_transactions: poll failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail=f"Transaction poll failed — Yahoo connection may need re-authorising. ({exc})",
        )
    return {"status": "ok", "new_count": new_count}


@router.post("/force-reimport", status_code=202, tags=["admin"])
def force_reimport(
    background_tasks: BackgroundTasks,
    count: int = Query(default=200, ge=1, le=500),
) -> dict:
    """Admin: delete and re-import all transactions for all connected leagues.

    Runs the same logic as /backfill?force_reimport=true but requires no user auth,
    so it can be triggered from the command line after a parsing fix is deployed.
    """
    from fantasai.services.yahoo_transactions import poll_all_leagues
    background_tasks.add_task(poll_all_leagues, count, True, True)
    return {"status": "accepted", "message": f"Re-importing up to {count} transactions per league in background"}


@router.post("/backfill", status_code=202)
def backfill_transactions(
    background_tasks: BackgroundTasks,
    count: int = Query(default=200, ge=1, le=500, description="Number of historical transactions to import"),
    force_reimport: bool = Query(default=False, description="Delete and re-import existing transactions (use to fix previously mis-parsed add+drops)"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Import historical transactions and grade them without surfacing in the ticker.

    Fetches up to `count` recent transactions from Yahoo, stores them with
    is_backfill=True so they appear in the Move Grades feed but never trigger
    the site-wide ticker notification bar.

    Set force_reimport=true to delete and re-import existing transactions — useful
    when the parsing logic has been updated (e.g. combined add+drop detection).
    """
    from fantasai.services.yahoo_transactions import poll_all_leagues
    background_tasks.add_task(poll_all_leagues, count, True, force_reimport)
    msg = (
        f"Re-importing up to {count} historical transactions (deleting existing records first)"
        if force_reimport
        else f"Importing up to {count} historical transactions in background"
    )
    return {"status": "backfilling", "message": msg}
