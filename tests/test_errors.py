"""Tests for API error handling middleware."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def test_health_still_works(client: TestClient) -> None:
    """Health check should be unaffected by error handlers."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_404_on_unknown_route(client: TestClient) -> None:
    """Unknown routes should return 404 (FastAPI default)."""
    resp = client.get("/api/v1/nonexistent")
    assert resp.status_code in (404, 405)


def test_player_not_found_returns_404(db_client: tuple[TestClient, Session]) -> None:
    """GET /players/{id} for a missing player should return 404."""
    client, _ = db_client
    resp = client.get("/api/v1/players/99999")
    assert resp.status_code == 404
