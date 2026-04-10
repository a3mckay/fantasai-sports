"""Tests for the rankings API endpoint."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fantasai.models.player import Player, PlayerStats


def _seed_players_and_stats(db: Session) -> None:
    """Seed a minimal set of players + stats for ranking tests."""
    db.add(Player(player_id=1, name="Aaron Judge", team="NYY", positions=["OF"]))
    db.add(Player(player_id=2, name="Juan Soto", team="NYM", positions=["OF"]))
    db.add(Player(player_id=10, name="Zack Wheeler", team="PHI", positions=["SP"]))
    db.commit()

    db.add(PlayerStats(
        player_id=1, season=2026, week=None, stat_type="batting", data_source="actual",
        counting_stats={"PA": 550, "R": 90, "HR": 40, "RBI": 100, "SB": 5},
        rate_stats={"AVG": 0.290, "OPS": 0.950},
        advanced_stats={"xwOBA": 0.400, "Barrel%": 20.0},
    ))
    db.add(PlayerStats(
        player_id=2, season=2026, week=None, stat_type="batting", data_source="actual",
        counting_stats={"PA": 520, "R": 95, "HR": 35, "RBI": 90, "SB": 8},
        rate_stats={"AVG": 0.280, "OPS": 0.920},
        advanced_stats={"xwOBA": 0.390, "Barrel%": 17.0},
    ))
    db.add(PlayerStats(
        player_id=10, season=2026, week=None, stat_type="pitching", data_source="actual",
        counting_stats={"IP": 180.0, "W": 14, "SV": 0, "SO": 200},
        rate_stats={"ERA": 2.90, "WHIP": 1.05},
        advanced_stats={"xERA": 3.10, "Stuff+": 115},
    ))
    db.commit()


class TestListRankings:
    def test_empty_db_returns_empty_list(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.get("/api/v1/rankings")
        assert resp.status_code == 200
        assert resp.json()["rankings"] == []

    def test_returns_rankings_with_data(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings")
        assert resp.status_code == 200
        data = resp.json()["rankings"]
        assert len(data) == 3

    def test_rankings_have_required_fields(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings")
        r = resp.json()["rankings"][0]
        assert "player_id" in r
        assert "name" in r
        assert "team" in r
        assert "positions" in r
        assert "stat_type" in r
        assert "overall_rank" in r
        assert "score" in r
        assert "raw_score" in r
        assert "category_contributions" in r

    def test_ranks_are_contiguous_from_one(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings")
        rankings = resp.json()["rankings"]
        ranks = sorted(r["overall_rank"] for r in rankings)
        assert ranks == list(range(1, len(ranks) + 1))

    def test_filter_by_stat_type_batting(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings?stat_type=batting")
        assert resp.status_code == 200
        data = resp.json()["rankings"]
        assert len(data) == 2
        assert all(r["stat_type"] == "batting" for r in data)

    def test_filter_by_stat_type_pitching(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings?stat_type=pitching")
        assert resp.status_code == 200
        data = resp.json()["rankings"]
        assert len(data) == 1
        assert data[0]["name"] == "Zack Wheeler"

    def test_filter_by_position(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings?position=OF")
        assert resp.status_code == 200
        data = resp.json()["rankings"]
        assert len(data) == 2
        names = {r["name"] for r in data}
        assert "Aaron Judge" in names
        assert "Juan Soto" in names

    def test_lookback_ranking_type(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings?ranking_type=lookback")
        assert resp.status_code == 200
        assert len(resp.json()["rankings"]) == 3

    def test_predictive_ranking_type(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings?ranking_type=predictive")
        assert resp.status_code == 200
        assert len(resp.json()["rankings"]) == 3

    def test_invalid_ranking_type_returns_422(self, db_client: tuple[TestClient, Session]) -> None:
        client, _ = db_client
        resp = client.get("/api/v1/rankings?ranking_type=invalid")
        assert resp.status_code == 422

    def test_pagination_limit(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()["rankings"]) == 2

    def test_pagination_offset(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        all_data = client.get("/api/v1/rankings?limit=100").json()["rankings"]
        page2 = client.get("/api/v1/rankings?limit=1&offset=1").json()["rankings"]
        assert len(page2) == 1
        assert page2[0]["player_id"] == all_data[1]["player_id"]

    def test_no_data_for_season_returns_empty(self, db_client: tuple[TestClient, Session]) -> None:
        client, db = db_client
        _seed_players_and_stats(db)
        resp = client.get("/api/v1/rankings?season=2020")
        assert resp.status_code == 200
        assert resp.json()["rankings"] == []
