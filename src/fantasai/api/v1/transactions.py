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


def _txn_to_read(txn: Transaction) -> TransactionRead:
    return TransactionRead(
        id=txn.id,
        yahoo_transaction_id=txn.yahoo_transaction_id,
        league_id=txn.league_id,
        transaction_type=txn.transaction_type,
        participants=txn.participants or [],
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


# ── Routes ────────────────────────────────────────────────────────────────────

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
    return [_txn_to_read(t) for t in q.offset(offset).limit(limit).all()]


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
    return [_txn_to_read(t) for t in q.all()]


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
    return _txn_to_read(txn)


@router.get("/{transaction_id}/card")
def get_grade_card_image(
    transaction_id: int,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Serve the grade card PNG for a transaction, regenerating if needed."""
    txn = db.get(Transaction, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or conn.league_key != txn.league_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Regenerate the card if the stored file is missing (e.g. after a deploy
    # that wiped /tmp) so sharing always works.
    if not txn.card_image_path or not os.path.exists(txn.card_image_path):
        try:
            from fantasai.brain.grade_card import render_grade_card
            card_path = render_grade_card(txn, db)
            if card_path:
                txn.card_image_path = card_path
                db.commit()
        except Exception:
            pass

    if not txn.card_image_path or not os.path.exists(txn.card_image_path):
        raise HTTPException(status_code=404, detail="Grade card could not be generated")

    filename = f"grade_{txn.transaction_type}_{txn.grade_letter}_{txn.share_token[:8]}.png"
    return FileResponse(
        txn.card_image_path,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/share/{share_token}")
def get_shared_card(
    share_token: str,
    db: Session = Depends(get_db),
):
    """Public endpoint — serves a grade card PNG by share token (no auth required).

    Used by the frontend Share button so recipients don't need to be logged in.
    """
    txn = (
        db.query(Transaction)
        .filter(Transaction.share_token == share_token)
        .first()
    )
    if not txn:
        raise HTTPException(status_code=404, detail="Grade card not found")

    # Regenerate if missing (ephemeral /tmp storage)
    if not txn.card_image_path or not os.path.exists(txn.card_image_path):
        try:
            from fantasai.brain.grade_card import render_grade_card
            card_path = render_grade_card(txn, db)
            if card_path:
                txn.card_image_path = card_path
                db.commit()
        except Exception:
            pass

    if not txn.card_image_path or not os.path.exists(txn.card_image_path):
        raise HTTPException(status_code=404, detail="Grade card could not be generated")

    filename = f"grade_{txn.transaction_type}_{txn.grade_letter}_{txn.share_token[:8]}.png"
    return FileResponse(
        txn.card_image_path,
        media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.post("/regrade", status_code=202)
def regrade_transactions(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Re-grade all existing transactions for the user's league.

    Clears grade_letter/grade_rationale/card_image_path on every transaction
    so the next poll cycle regenerates them with the current grader logic.
    Runs in the background — takes ~30s for 25 transactions.
    """
    from fantasai.models.user import YahooConnection

    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or not conn.league_key:
        return {"status": "no_league", "message": "No active league found"}

    league_id = conn.league_key

    def _do_regrade(lid: str) -> None:
        try:
            from fantasai.config import settings
            from fantasai.database import SessionLocal
            from fantasai.models.league import League
            from fantasai.models.transaction import Transaction
            from fantasai.services.yahoo_transactions import _grade_ungraded

            _db = SessionLocal()
            try:
                # Reset all grades so _grade_ungraded picks them up
                _db.query(Transaction).filter(
                    Transaction.league_id == lid,
                    Transaction.grade_letter.isnot(None),
                ).update(
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

    background_tasks.add_task(_do_regrade, league_id)
    return {"status": "regrading", "message": "Re-grading all transactions in background"}


@router.post("/poll", status_code=202)
def poll_transactions(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Manually trigger a transaction poll for the user's league."""
    from fantasai.services.yahoo_transactions import poll_all_leagues
    background_tasks.add_task(poll_all_leagues)
    return {"status": "polling", "message": "Transaction poll started in background"}


@router.post("/backfill", status_code=202)
def backfill_transactions(
    background_tasks: BackgroundTasks,
    count: int = Query(default=200, ge=1, le=500, description="Number of historical transactions to import"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Import historical transactions and grade them without surfacing in the ticker.

    Fetches up to `count` recent transactions from Yahoo, stores them with
    is_backfill=True so they appear in the Move Grades feed but never trigger
    the site-wide ticker notification bar.
    """
    from fantasai.services.yahoo_transactions import poll_all_leagues
    background_tasks.add_task(poll_all_leagues, count, True)
    return {"status": "backfilling", "message": f"Importing up to {count} historical transactions in background"}
