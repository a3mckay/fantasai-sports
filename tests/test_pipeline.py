"""Tests for the data pipeline."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session

from fantasai.adapters.base import NormalizedPlayerData
from fantasai.engine.pipeline import (
    PipelineError,
    sync_players,
    sync_rolling_windows,
    _upsert_player,
    _upsert_player_stats,
    _upsert_rolling_stats,
    _normalise_name,
    _resolve_player_id,
)
from fantasai.models.player import Player, PlayerRollingStats, PlayerStats


def _make_player_data(player_id: int = 1, name: str = "Test Player") -> NormalizedPlayerData:
    return NormalizedPlayerData(
        player_id=player_id,
        name=name,
        team="NYY",
        positions=["SS", "2B"],
        stat_type="batting",
        counting_stats={"R": 50, "HR": 15},
        rate_stats={"AVG": 0.280, "OPS": 0.820},
        advanced_stats={"xwOBA": 0.350},
    )


class TestUpsertPlayer:
    def test_insert_new_player(self, db: Session) -> None:
        data = _make_player_data()
        _upsert_player(db, data)
        db.commit()

        player = db.get(Player, 1)
        assert player is not None
        assert player.name == "Test Player"
        assert player.team == "NYY"

    def test_update_existing_player(self, db: Session) -> None:
        data = _make_player_data()
        _upsert_player(db, data)
        db.commit()

        data.team = "LAD"
        _upsert_player(db, data)
        db.commit()

        player = db.get(Player, 1)
        assert player is not None
        assert player.team == "LAD"


class TestUpsertPlayerStats:
    def test_insert_new_stats(self, db: Session) -> None:
        data = _make_player_data()
        _upsert_player(db, data)
        _upsert_player_stats(db, data, season=2025, week=None)
        db.commit()

        stats = db.query(PlayerStats).filter_by(player_id=1).first()
        assert stats is not None
        assert stats.counting_stats["HR"] == 15
        assert stats.rate_stats["AVG"] == 0.280

    def test_update_existing_stats(self, db: Session) -> None:
        data = _make_player_data()
        _upsert_player(db, data)
        _upsert_player_stats(db, data, season=2025, week=None)
        db.commit()

        data.counting_stats["HR"] = 20
        _upsert_player_stats(db, data, season=2025, week=None)
        db.commit()

        stats = db.query(PlayerStats).filter_by(player_id=1).first()
        assert stats is not None
        assert stats.counting_stats["HR"] == 20

    def test_different_weeks_create_separate_records(self, db: Session) -> None:
        data = _make_player_data()
        _upsert_player(db, data)
        _upsert_player_stats(db, data, season=2025, week=1)
        _upsert_player_stats(db, data, season=2025, week=2)
        db.commit()

        count = db.query(PlayerStats).filter_by(player_id=1).count()
        assert count == 2


class TestSyncPlayers:
    def test_sync_with_mock_adapter(self, db: Session) -> None:
        """Test that sync_players persists data from adapter to DB."""
        adapter = MagicMock()
        adapter.fetch_player_data.return_value = [
            _make_player_data(1, "Player A"),
            _make_player_data(2, "Player B"),
        ]

        result = sync_players(db, adapter, season=2025)

        assert len(result) == 2
        assert db.query(Player).count() == 2
        assert db.query(PlayerStats).count() == 2

    def test_sync_empty_adapter_returns_empty(self, db: Session) -> None:
        """Adapter returning no players should not crash."""
        adapter = MagicMock()
        adapter.fetch_player_data.return_value = []

        result = sync_players(db, adapter, season=2025)

        assert result == []
        assert db.query(Player).count() == 0

    def test_sync_retries_on_transient_error(self, db: Session) -> None:
        """Transient network errors should be retried."""
        adapter = MagicMock()
        adapter.fetch_player_data.side_effect = [
            ConnectionError("timeout"),
            [_make_player_data(1, "Player A")],
        ]

        result = sync_players(db, adapter, season=2025)

        assert len(result) == 1
        assert adapter.fetch_player_data.call_count == 2

    def test_sync_raises_after_max_retries(self, db: Session) -> None:
        """Should raise PipelineError after exhausting retries."""
        adapter = MagicMock()
        adapter.fetch_player_data.side_effect = ConnectionError("down")

        with pytest.raises(PipelineError, match="failed after"):
            sync_players(db, adapter, season=2025)

    def test_sync_non_retryable_error_raises_immediately(self, db: Session) -> None:
        """Non-transient errors (ValueError etc.) should not be retried."""
        adapter = MagicMock()
        adapter.fetch_player_data.side_effect = ValueError("bad data")

        with pytest.raises(PipelineError, match="bad data"):
            sync_players(db, adapter, season=2025)

        assert adapter.fetch_player_data.call_count == 1

    def test_sync_partial_batch_failure_persists_good_records(self, db: Session) -> None:
        """If one batch fails, other batches should still be committed."""
        # Create 3 players, but make the 2nd one cause an error during upsert
        players = [
            _make_player_data(1, "Good A"),
            _make_player_data(2, "Good B"),
            _make_player_data(3, "Good C"),
        ]

        adapter = MagicMock()
        adapter.fetch_player_data.return_value = players

        # With batch_size=3, all go in one batch — all succeed
        result = sync_players(db, adapter, season=2025, batch_size=3)
        assert len(result) == 3
        assert db.query(Player).count() == 3


# ---------------------------------------------------------------------------
# Tests: _normalise_name
# ---------------------------------------------------------------------------


class TestNormaliseName:
    def test_lowercases_and_strips_whitespace(self) -> None:
        assert _normalise_name("  Juan  Soto  ") == "juan soto"

    def test_strips_diacritics(self) -> None:
        assert _normalise_name("Javier Báez") == "javier baez"

    def test_handles_multiple_accents(self) -> None:
        assert _normalise_name("José Ramírez") == "jose ramirez"

    def test_plain_ascii_unchanged(self) -> None:
        assert _normalise_name("Mike Trout") == "mike trout"


# ---------------------------------------------------------------------------
# Tests: _resolve_player_id
# ---------------------------------------------------------------------------


class TestResolvePlayerId:
    def _make_indexes(
        self, players: list[tuple[int, str, str]]
    ) -> tuple[dict, dict]:
        """Build name_team_index and name_index from (player_id, name, team) tuples."""
        name_team_index: dict[tuple[str, str], int] = {}
        name_index: dict[str, list[int]] = {}
        for pid, name, team in players:
            norm_name = _normalise_name(name)
            norm_team = team.upper()
            name_team_index[(norm_name, norm_team)] = pid
            name_index.setdefault(norm_name, []).append(pid)
        return name_team_index, name_index

    def test_exact_name_team_match(self) -> None:
        nt, n = self._make_indexes([(42, "Mike Trout", "LAA")])
        assert _resolve_player_id("Mike Trout", "LAA", nt, n) == 42

    def test_name_only_match_unambiguous(self) -> None:
        nt, n = self._make_indexes([(99, "Juan Soto", "NYY")])
        # Team mismatch but only one player with this name
        assert _resolve_player_id("Juan Soto", "WSH", nt, n) == 99

    def test_ambiguous_name_returns_none(self) -> None:
        nt, n = self._make_indexes([
            (1, "John Smith", "NYY"),
            (2, "John Smith", "LAD"),
        ])
        # Ambiguous — neither team is provided
        assert _resolve_player_id("John Smith", "BOS", nt, n) is None

    def test_no_match_returns_none(self) -> None:
        nt, n = self._make_indexes([(1, "Mike Trout", "LAA")])
        assert _resolve_player_id("Nobody Famous", "WSH", nt, n) is None

    def test_diacritic_normalisation_matches(self) -> None:
        nt, n = self._make_indexes([(7, "José Ramírez", "CLE")])
        assert _resolve_player_id("Jose Ramirez", "CLE", nt, n) == 7


# ---------------------------------------------------------------------------
# Tests: _upsert_rolling_stats
# ---------------------------------------------------------------------------


class TestUpsertRollingStats:
    def _seed_player(self, db: Session) -> None:
        player = Player(player_id=1, name="Test Player", team="NYY", positions=["1B"])
        db.add(player)
        db.commit()

    def test_insert_new_rolling_stats(self, db: Session) -> None:
        self._seed_player(db)
        rec = {
            "stat_type": "batting",
            "counting_stats": {"HR": 3, "R": 8},
            "rate_stats": {"AVG": 0.310},
        }
        _upsert_rolling_stats(
            db, player_id=1, season=2025, window_days=14,
            start_date=date(2025, 4, 1), end_date=date(2025, 4, 15), rec=rec,
        )
        db.commit()

        row = db.query(PlayerRollingStats).filter_by(player_id=1).first()
        assert row is not None
        assert row.window_days == 14
        assert row.counting_stats["HR"] == 3
        assert row.rate_stats["AVG"] == 0.310

    def test_upsert_updates_existing(self, db: Session) -> None:
        self._seed_player(db)
        rec = {
            "stat_type": "batting",
            "counting_stats": {"HR": 2},
            "rate_stats": {"AVG": 0.280},
        }
        _upsert_rolling_stats(
            db, player_id=1, season=2025, window_days=14,
            start_date=date(2025, 4, 1), end_date=date(2025, 4, 15), rec=rec,
        )
        db.commit()

        # Update — same window, different stats
        rec["counting_stats"]["HR"] = 5
        rec["rate_stats"]["AVG"] = 0.350
        _upsert_rolling_stats(
            db, player_id=1, season=2025, window_days=14,
            start_date=date(2025, 4, 2), end_date=date(2025, 4, 16), rec=rec,
        )
        db.commit()

        rows = db.query(PlayerRollingStats).filter_by(player_id=1).all()
        assert len(rows) == 1  # upsert, not insert
        assert rows[0].counting_stats["HR"] == 5
        assert rows[0].rate_stats["AVG"] == 0.350


# ---------------------------------------------------------------------------
# Tests: sync_rolling_windows
# ---------------------------------------------------------------------------


def _make_batting_record(name: str, team: str) -> dict:
    return {
        "name": name,
        "team": team,
        "stat_type": "batting",
        "counting_stats": {"HR": 4, "R": 9, "RBI": 10, "SB": 1, "H": 20},
        "rate_stats": {"AVG": 0.295, "OBP": 0.365, "SLG": 0.490},
    }


def _make_pitching_record(name: str, team: str) -> dict:
    return {
        "name": name,
        "team": team,
        "stat_type": "pitching",
        "counting_stats": {"W": 1, "SV": 0, "K": 18, "IP": 14.0},
        "rate_stats": {"ERA": 2.57, "WHIP": 1.10},
    }


class TestSyncRollingWindows:
    def _seed_players(self, db: Session) -> None:
        db.add(Player(player_id=1, name="Batter One", team="NYY", positions=["1B"]))
        db.add(Player(player_id=2, name="Pitcher One", team="BOS", positions=["SP"]))
        db.commit()

    def test_upserts_matched_players(self, db: Session) -> None:
        """Players matched by name+team get rolling stats upserted."""
        self._seed_players(db)
        adapter = MagicMock()
        adapter.fetch_rolling_batting_stats.return_value = [
            _make_batting_record("Batter One", "NYY"),
        ]
        adapter.fetch_rolling_pitching_stats.return_value = [
            _make_pitching_record("Pitcher One", "BOS"),
        ]

        result = sync_rolling_windows(
            db, adapter, season=2025, as_of_date=date(2025, 5, 1), windows=[14]
        )

        assert result[14] == 2  # both records matched and upserted
        assert db.query(PlayerRollingStats).count() == 2

    def test_unmatched_records_are_skipped(self, db: Session) -> None:
        """Records with no player match are silently skipped."""
        self._seed_players(db)
        adapter = MagicMock()
        adapter.fetch_rolling_batting_stats.return_value = [
            _make_batting_record("Unknown Player", "TOR"),
        ]
        adapter.fetch_rolling_pitching_stats.return_value = []

        result = sync_rolling_windows(
            db, adapter, season=2025, as_of_date=date(2025, 5, 1), windows=[14]
        )

        assert result[14] == 0
        assert db.query(PlayerRollingStats).count() == 0

    def test_syncs_multiple_windows(self, db: Session) -> None:
        """Each window gets its own rolling stats row (separate window_days)."""
        self._seed_players(db)
        adapter = MagicMock()
        adapter.fetch_rolling_batting_stats.return_value = [
            _make_batting_record("Batter One", "NYY"),
        ]
        adapter.fetch_rolling_pitching_stats.return_value = []

        result = sync_rolling_windows(
            db, adapter, season=2025, as_of_date=date(2025, 5, 1), windows=[14, 30]
        )

        assert result[14] == 1
        assert result[30] == 1
        # Separate rows for each window
        assert db.query(PlayerRollingStats).filter_by(player_id=1).count() == 2

    def test_adapter_failure_recorded_as_zero(self, db: Session) -> None:
        """If the adapter fails for a window, that window returns 0 (no crash)."""
        self._seed_players(db)
        adapter = MagicMock()
        adapter.fetch_rolling_batting_stats.side_effect = ConnectionError("timeout")
        adapter.fetch_rolling_pitching_stats.side_effect = ConnectionError("timeout")

        result = sync_rolling_windows(
            db, adapter, season=2025, as_of_date=date(2025, 5, 1), windows=[14]
        )

        assert result[14] == 0

    def test_name_fuzzy_match_with_diacritics(self, db: Session) -> None:
        """BRef accent-stripped names match players stored with diacritics."""
        db.add(Player(player_id=3, name="José Ramírez", team="CLE", positions=["3B"]))
        db.commit()
        adapter = MagicMock()
        adapter.fetch_rolling_batting_stats.return_value = [
            _make_batting_record("Jose Ramirez", "CLE"),
        ]
        adapter.fetch_rolling_pitching_stats.return_value = []

        result = sync_rolling_windows(
            db, adapter, season=2025, as_of_date=date(2025, 5, 1), windows=[14]
        )

        assert result[14] == 1
