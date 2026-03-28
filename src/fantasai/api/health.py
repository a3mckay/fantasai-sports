from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health_check() -> dict:
    return {"status": "ok", "version": "0.1.0"}


@router.get("/health/db")
def health_db() -> dict:
    """Lightweight DB diagnostic — migration status and row counts. No auth required."""
    result: dict = {"status": "ok"}
    try:
        from sqlalchemy import inspect, text
        from fantasai.database import SessionLocal
        db = SessionLocal()
        try:
            inspector = inspect(db.bind)
            txn_cols = [c["name"] for c in inspector.get_columns("transactions")]
            result["transactions_has_is_backfill"] = "is_backfill" in txn_cols
            result["matchup_analyses_table_exists"] = inspector.has_table("matchup_analyses")
            result["transaction_count"] = db.execute(text("SELECT COUNT(*) FROM transactions")).scalar()
            result["yahoo_connection_count"] = db.execute(
                text("SELECT COUNT(*) FROM yahoo_connections WHERE encrypted_access_token IS NOT NULL")
            ).scalar()
        finally:
            db.close()
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result
