from __future__ import annotations

from collections.abc import Generator
from typing import Any

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from fantasai.config import settings


def _build_engine() -> Engine:
    """Create SQLAlchemy engine with appropriate pooling for the backend."""
    connect_args: dict[str, Any] = {}
    kwargs: dict[str, Any] = {}

    if settings.is_sqlite:
        # SQLite doesn't support pool_size/max_overflow
        connect_args["check_same_thread"] = False
    else:
        # PostgreSQL connection pooling
        kwargs.update(
            pool_size=10,
            max_overflow=20,
            pool_timeout=30,
            pool_recycle=1800,
            pool_pre_ping=True,  # verify connections are alive before use
        )

    return create_engine(
        settings.database_url,
        echo=(settings.env == "development"),
        connect_args=connect_args,
        **kwargs,
    )


engine = _build_engine()
SessionLocal = sessionmaker(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that provides a database session per request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
