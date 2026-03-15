"""Tests for league and team CRUD API endpoints."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fantasai.models.league import League, Team

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LEAGUE_PAYLOAD = {
    "league_id": "yahoo-123",
    "platform": "yahoo",
    "sport": "mlb",
    "scoring_categories": ["R", "HR", "RBI", "SB", "AVG", "OPS"],
    "league_type": "h2h_categories",
    "settings": {"max_acquisitions_per_week": 4, "roster_size": 25},
    "roster_positions": ["C", "1B", "2B", "3B", "SS", "OF", "OF", "OF", "SP", "SP", "RP"],
}

_TEAM_PAYLOAD = {
    "league_id": "yahoo-123",
    "manager_name": "Test Manager",
    "roster": [],
}


def _seed_league(db: Session) -> League:
    league = League(**{k: v for k, v in _LEAGUE_PAYLOAD.items()})
    db.add(league)
    db.commit()
    return league


# ---------------------------------------------------------------------------
# POST /api/v1/leagues
# ---------------------------------------------------------------------------


class TestCreateLeague:
    def test_creates_league(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.post("/api/v1/leagues", json=_LEAGUE_PAYLOAD)
        assert resp.status_code == 201
        data = resp.json()
        assert data["league_id"] == "yahoo-123"
        assert data["platform"] == "yahoo"
        assert data["scoring_categories"] == ["R", "HR", "RBI", "SB", "AVG", "OPS"]

    def test_duplicate_league_id_returns_409(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_league(db)
        resp = client.post("/api/v1/leagues", json=_LEAGUE_PAYLOAD)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    def test_league_persisted_to_db(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        client.post("/api/v1/leagues", json=_LEAGUE_PAYLOAD)
        league = db.get(League, "yahoo-123")
        assert league is not None
        assert league.league_type == "h2h_categories"


# ---------------------------------------------------------------------------
# GET /api/v1/leagues
# ---------------------------------------------------------------------------


class TestListLeagues:
    def test_empty_returns_empty_list(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.get("/api/v1/leagues")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_returns_created_leagues(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_league(db)
        resp = client.get("/api/v1/leagues")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["league_id"] == "yahoo-123"


# ---------------------------------------------------------------------------
# GET /api/v1/leagues/{league_id}
# ---------------------------------------------------------------------------


class TestGetLeague:
    def test_returns_existing_league(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_league(db)
        resp = client.get("/api/v1/leagues/yahoo-123")
        assert resp.status_code == 200
        assert resp.json()["league_id"] == "yahoo-123"

    def test_unknown_league_returns_404(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.get("/api/v1/leagues/nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/v1/leagues/{league_id}
# ---------------------------------------------------------------------------


class TestUpdateLeague:
    def test_updates_scoring_categories(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_league(db)
        updated = dict(_LEAGUE_PAYLOAD)
        updated["scoring_categories"] = ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"]
        resp = client.put("/api/v1/leagues/yahoo-123", json=updated)
        assert resp.status_code == 200
        assert len(resp.json()["scoring_categories"]) == 12

    def test_update_unknown_league_returns_404(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.put("/api/v1/leagues/ghost", json=_LEAGUE_PAYLOAD)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/leagues/{league_id}/teams
# ---------------------------------------------------------------------------


class TestCreateTeam:
    def test_creates_team_in_league(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_league(db)
        resp = client.post("/api/v1/leagues/yahoo-123/teams", json=_TEAM_PAYLOAD)
        assert resp.status_code == 201
        data = resp.json()
        assert data["manager_name"] == "Test Manager"
        assert data["league_id"] == "yahoo-123"
        assert isinstance(data["team_id"], int)

    def test_team_with_roster(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_league(db)
        payload = dict(_TEAM_PAYLOAD)
        payload["roster"] = [12345, 23456, 34567]
        resp = client.post("/api/v1/leagues/yahoo-123/teams", json=payload)
        assert resp.status_code == 201
        assert resp.json()["roster"] == [12345, 23456, 34567]

    def test_create_team_unknown_league_returns_404(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.post("/api/v1/leagues/ghost/teams", json=_TEAM_PAYLOAD)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/leagues/{league_id}/teams
# ---------------------------------------------------------------------------


class TestListTeams:
    def test_lists_teams_in_league(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_league(db)
        db.add(Team(league_id="yahoo-123", manager_name="Alice", roster=[]))
        db.add(Team(league_id="yahoo-123", manager_name="Bob", roster=[]))
        db.commit()
        resp = client.get("/api/v1/leagues/yahoo-123/teams")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_list_teams_unknown_league_returns_404(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.get("/api/v1/leagues/ghost/teams")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/v1/leagues/{league_id}/teams/{team_id}/roster
# ---------------------------------------------------------------------------


class TestUpdateRoster:
    def _seed_team(self, db: Session) -> int:
        _seed_league(db)
        team = Team(league_id="yahoo-123", manager_name="Alice", roster=[])
        db.add(team)
        db.commit()
        db.refresh(team)
        return team.team_id

    def test_replaces_roster(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        team_id = self._seed_team(db)
        resp = client.put(
            f"/api/v1/leagues/yahoo-123/teams/{team_id}/roster",
            json=[11111, 22222, 33333],
        )
        assert resp.status_code == 200
        assert resp.json()["roster"] == [11111, 22222, 33333]

    def test_update_roster_wrong_league_returns_404(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        team_id = self._seed_team(db)
        resp = client.put(
            f"/api/v1/leagues/wrong-league/teams/{team_id}/roster",
            json=[11111],
        )
        assert resp.status_code == 404
