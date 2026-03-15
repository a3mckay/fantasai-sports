"""Tests for adapter data validation and edge cases."""
from __future__ import annotations

import pandas as pd
import pytest

from fantasai.adapters.mlb import (
    MLBAdapter, _safe_float, _extract_stats, _parse_positions, _infer_pitcher_position,
)


class TestSafeFloat:
    def test_normal_float(self) -> None:
        assert _safe_float(3.14) == 3.14

    def test_int_converts(self) -> None:
        assert _safe_float(42) == 42.0

    def test_string_number(self) -> None:
        assert _safe_float("2.5") == 2.5

    def test_none_returns_none(self) -> None:
        assert _safe_float(None) is None

    def test_nan_returns_none(self) -> None:
        assert _safe_float(float("nan")) is None

    def test_empty_string_returns_none(self) -> None:
        assert _safe_float("") is None

    def test_non_numeric_string_returns_none(self) -> None:
        assert _safe_float("N/A") is None


class TestExtractStats:
    def test_extracts_present_columns(self) -> None:
        row = pd.Series({"HR": 30, "RBI": 90, "SB": 15})
        result = _extract_stats(row, ["HR", "RBI", "SB"])
        assert result == {"HR": 30.0, "RBI": 90.0, "SB": 15.0}

    def test_skips_missing_columns(self) -> None:
        row = pd.Series({"HR": 30})
        result = _extract_stats(row, ["HR", "RBI"])
        assert result == {"HR": 30.0}
        assert "RBI" not in result

    def test_omits_nan_values(self) -> None:
        row = pd.Series({"HR": 30, "SB": float("nan")})
        result = _extract_stats(row, ["HR", "SB"])
        assert result == {"HR": 30.0}
        assert "SB" not in result


class TestParsePositions:
    def test_slash_separated(self) -> None:
        assert _parse_positions("3B/SS") == ["3B", "SS"]

    def test_single_position(self) -> None:
        assert _parse_positions("SP") == ["SP"]

    def test_none_returns_empty(self) -> None:
        assert _parse_positions(None) == []

    def test_nan_returns_empty(self) -> None:
        assert _parse_positions(float("nan")) == []

    def test_empty_string_returns_empty(self) -> None:
        assert _parse_positions("") == []


class TestInferPitcherPosition:
    def test_mostly_starts_is_sp(self) -> None:
        row = pd.Series({"GS": 30, "G": 32})
        assert _infer_pitcher_position(row) == ["SP"]

    def test_no_starts_is_rp(self) -> None:
        row = pd.Series({"GS": 0, "G": 60})
        assert _infer_pitcher_position(row) == ["RP"]

    def test_opener_with_few_starts_is_rp(self) -> None:
        """A pitcher with 10 starts out of 50 games (20%) is a reliever."""
        row = pd.Series({"GS": 10, "G": 50})
        assert _infer_pitcher_position(row) == ["RP"]

    def test_swingman_at_threshold_is_sp(self) -> None:
        """A pitcher at exactly 40% starts is classified SP."""
        row = pd.Series({"GS": 20, "G": 50})
        assert _infer_pitcher_position(row) == ["SP"]

    def test_zero_games_returns_empty(self) -> None:
        row = pd.Series({"GS": 0, "G": 0})
        assert _infer_pitcher_position(row) == []

    def test_missing_columns_returns_empty(self) -> None:
        row = pd.Series({"Name": "Test"})
        assert _infer_pitcher_position(row) == []


class TestNormalizePitchingStats:
    def test_pitcher_gets_inferred_position(self) -> None:
        adapter = MLBAdapter()
        df = pd.DataFrame([{
            "IDfg": 99999,
            "Name": "Test Pitcher",
            "Team": "NYY",
            "GS": 30,
            "G": 32,
            "IP": 180.0,
            "W": 12,
            "SO": 200,
            "ERA": 3.00,
        }])
        result = adapter.normalize_stats(df, stat_type="pitching")
        assert len(result) == 1
        assert result[0].positions == ["SP"]

    def test_reliever_gets_rp(self) -> None:
        adapter = MLBAdapter()
        df = pd.DataFrame([{
            "IDfg": 88888,
            "Name": "Test Closer",
            "Team": "NYM",
            "GS": 0,
            "G": 65,
            "IP": 60.0,
            "SV": 35,
            "SO": 80,
            "ERA": 2.50,
        }])
        result = adapter.normalize_stats(df, stat_type="pitching")
        assert len(result) == 1
        assert result[0].positions == ["RP"]


class TestNormalizeStats:
    def test_empty_dataframe_returns_empty(self) -> None:
        adapter = MLBAdapter()
        df = pd.DataFrame()
        result = adapter.normalize_stats(df, stat_type="batting")
        assert result == []

    def test_skips_rows_without_idfg(self) -> None:
        adapter = MLBAdapter()
        df = pd.DataFrame([{"Name": "Nobody", "Team": "NYY", "IDfg": 0}])
        result = adapter.normalize_stats(df, stat_type="batting")
        assert result == []

    def test_non_dataframe_raises_type_error(self) -> None:
        adapter = MLBAdapter()
        with pytest.raises(TypeError, match="Expected DataFrame"):
            adapter.normalize_stats({"not": "a dataframe"}, stat_type="batting")

    def test_normalizes_valid_batting_row(self) -> None:
        adapter = MLBAdapter()
        df = pd.DataFrame(
            [
                {
                    "IDfg": 12345,
                    "Name": "Test Player",
                    "Team": "NYY",
                    "Pos": "OF/DH",
                    "PA": 500,
                    "HR": 30,
                    "AVG": 0.280,
                    "xwOBA": 0.370,
                }
            ]
        )
        result = adapter.normalize_stats(df, stat_type="batting")
        assert len(result) == 1
        p = result[0]
        assert p.player_id == 12345
        assert p.name == "Test Player"
        assert p.positions == ["OF", "DH"]
        assert p.counting_stats["HR"] == 30.0
        assert p.rate_stats["AVG"] == 0.280
        assert p.advanced_stats["xwOBA"] == 0.370
