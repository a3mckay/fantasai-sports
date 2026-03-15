"""Tests for the players API endpoint."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fantasai.models.player import Player


def _seed_players(db: Session) -> None:
    db.add(Player(player_id=1, name="Aaron Judge", team="NYY", positions=["OF", "DH"]))
    db.add(Player(player_id=2, name="Juan Soto", team="NYM", positions=["OF"]))
    db.add(Player(player_id=3, name="Manny Machado", team="SD", positions=["3B"]))
    db.add(Player(player_id=10, name="Zack Wheeler", team="PHI", positions=["SP"]))
    db.commit()


class TestListPlayers:
    def test_empty_db_returns_empty_list(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.get("/api/v1/players")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_all_players(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players(db)
        resp = client.get("/api/v1/players")
        assert resp.status_code == 200
        assert len(resp.json()) == 4

    def test_filter_by_team(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players(db)
        resp = client.get("/api/v1/players?team=NYY")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Aaron Judge"

    def test_filter_by_team_case_insensitive(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players(db)
        resp = client.get("/api/v1/players?team=nym")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["name"] == "Juan Soto"

    def test_filter_by_position(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players(db)
        resp = client.get("/api/v1/players?position=OF")
        assert resp.status_code == 200
        data = resp.json()
        # Judge (OF/DH) and Soto (OF) match; Machado (3B) and Wheeler (SP) don't
        names = {p["name"] for p in data}
        assert "Aaron Judge" in names
        assert "Juan Soto" in names
        assert "Manny Machado" not in names

    def test_search_by_name(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players(db)
        resp = client.get("/api/v1/players?search=judge")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Aaron Judge"

    def test_pagination_limit(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players(db)
        resp = client.get("/api/v1/players?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_pagination_offset(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players(db)
        all_resp = client.get("/api/v1/players?limit=100")
        page2 = client.get("/api/v1/players?limit=2&offset=2")
        assert page2.status_code == 200
        # Second page should be different players than first two
        all_names = [p["name"] for p in all_resp.json()]
        page2_names = [p["name"] for p in page2.json()]
        assert page2_names == all_names[2:4]


class TestGetPlayer:
    def test_returns_player(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players(db)
        resp = client.get("/api/v1/players/1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["player_id"] == 1
        assert data["name"] == "Aaron Judge"
        assert data["team"] == "NYY"
        assert "OF" in data["positions"]

    def test_unknown_player_returns_404(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.get("/api/v1/players/99999")
        assert resp.status_code == 404
