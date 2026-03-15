"""Shared test fixtures."""
from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from fantasai.main import app
from fantasai.models import Base

# SQLite in-memory engine shared across all fixtures.
# StaticPool: all sessions share the same underlying connection so they see
#   the same in-memory database.  This is the standard pattern for SQLite
#   in-memory + SQLAlchemy test fixtures.
# check_same_thread=False: TestClient runs in a thread different from the
#   test thread, so we need this or SQLite raises a threading error.
_TEST_ENGINE = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(bind=_TEST_ENGINE)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def db() -> Generator[Session, None, None]:
    """Provide a clean in-memory SQLite session for each test."""
    Base.metadata.create_all(_TEST_ENGINE)
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(_TEST_ENGINE)


@pytest.fixture
def db_client(db: Session) -> Generator[tuple[TestClient, Session], None, None]:
    """TestClient with get_db wired to the in-memory test DB.

    Yields (client, db) so tests can seed data before making requests.
    """
    from fantasai.database import get_db

    def override_get_db() -> Generator[Session, None, None]:
        try:
            yield db
        finally:
            pass  # session lifecycle managed by the db fixture

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app), db
    app.dependency_overrides.clear()
