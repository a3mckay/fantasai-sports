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


@router.get("/unseen", response_model=list[TransactionRead])
def list_unseen_transactions(
    since_id: Optional[int] = Query(default=None, description="Return transactions with id > since_id"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Return graded transactions newer than since_id (for the ticker)."""
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or not conn.league_key:
        return []

    q = (
        db.query(Transaction)
        .filter(
            Transaction.league_id == conn.league_key,
            Transaction.grade_letter.isnot(None),
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
    """Download the grade card PNG for a transaction."""
    txn = db.get(Transaction, transaction_id)
    if not txn:
        raise HTTPException(status_code=404, detail="Transaction not found")
    from fantasai.models.user import YahooConnection
    conn = db.query(YahooConnection).filter(YahooConnection.user_id == user.id).first()
    if not conn or conn.league_key != txn.league_id:
        raise HTTPException(status_code=403, detail="Access denied")
    if not txn.card_image_path or not os.path.exists(txn.card_image_path):
        raise HTTPException(status_code=404, detail="Grade card image not yet generated")
    filename = f"grade_{txn.transaction_type}_{txn.grade_letter}_{txn.share_token[:8]}.png"
    return FileResponse(
        txn.card_image_path,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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
