from __future__ import annotations

from fantasai.adapters.mlb import MLBAdapter


def test_get_positions() -> None:
    adapter = MLBAdapter()
    positions = adapter.get_positions()
    assert "C" in positions
    assert "SS" in positions
    assert "SP" in positions
    assert "RP" in positions
    assert len(positions) >= 9


def test_get_available_stats() -> None:
    adapter = MLBAdapter()
    stats = adapter.get_available_stats()
    assert "HR" in stats
    assert "ERA" in stats
    assert "AVG" in stats


def test_get_predictive_stats() -> None:
    adapter = MLBAdapter()
    stats = adapter.get_predictive_stats()
    assert "xwOBA" in stats
    assert "xERA" in stats
    assert "Stuff+" in stats
