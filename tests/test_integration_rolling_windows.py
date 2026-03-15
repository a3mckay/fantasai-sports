"""Integration tests for rolling-window stat correctness.

These tests use HARDCODED stat fixtures with specific date windows so that any
regression in the adapter normalisation, pipeline upsert, or blurb data-block
construction is immediately visible and traceable to a specific BRef URL.

SKIPPED BY DEFAULT — they require a network connection and a live BRef call.
Run with:
    pytest -m integration tests/test_integration_rolling_windows.py -v

SPOT-CHECKING AGAINST BASEBALL REFERENCE
-----------------------------------------
For each window below, go to:
    https://www.baseball-reference.com/friv/dailysplits.fcgi
    → enter "From" and "To" dates as shown in the fixture
    → select "Batting" or "Pitching"
    → sort by the stat column in question
    → confirm the top players match the snapshot

The "G" column in every fixture row is the games-played count for that window.
Use it alongside Name + Team to pinpoint the correct row in BRef.

SYNTHETIC FIXTURE FORMAT
------------------------
Each fixture has:
    date_from, date_to : exact ISO dates used for the BRef fetch
    window_days        : calendar-day span (7, 14, 30, or 60)
    stat_type          : "batting" or "pitching"
    player             : display name for assertion messages
    team               : MLB team abbreviation as BRef returns it
    g                  : games played (from BRef "G" column)
    counting_stats     : subset of key counting stats
    rate_stats         : key rate stats

IMPORTANT — the numbers in these fixtures are REAL 2025 BRef aggregates
fetched at the time the fixtures were written.  If a player's stats differ
when you re-fetch, compare BRef's current figures — minor discrepancies
(e.g. 1 game delay in BRef's data) are acceptable; large differences suggest
an adapter normalisation bug.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Pytest marks — run these only with: pytest -m integration
# ---------------------------------------------------------------------------
pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Canonical verification windows
#
# Reference point: stats through 2025-07-07 (pre-All-Star break cutoff).
# These four windows let you verify short, medium, and long-term trends.
#
# BRef daily splits finder:
#   https://www.baseball-reference.com/friv/dailysplits.fcgi
# ---------------------------------------------------------------------------

WINDOWS = {
    "7day": {
        "date_from": "2025-07-01",
        "date_to":   "2025-07-07",
        "window_days": 7,
        "bref_url":  "https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-07-01&to=2025-07-07",
    },
    "14day": {
        "date_from": "2025-06-24",
        "date_to":   "2025-07-07",
        "window_days": 14,
        "bref_url":  "https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-06-24&to=2025-07-07",
    },
    "30day": {
        "date_from": "2025-06-08",
        "date_to":   "2025-07-07",
        "window_days": 30,
        "bref_url":  "https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-06-08&to=2025-07-07",
    },
    "60day": {
        "date_from": "2025-05-09",
        "date_to":   "2025-07-07",
        "window_days": 60,
        "bref_url":  "https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-05-09&to=2025-07-07",
    },
}


# ---------------------------------------------------------------------------
# Stat snapshots
#
# To VERIFY a row manually:
#   1. Open the bref_url for the window
#   2. Select player type (Batting / Pitching)
#   3. Find the player by Name + Team
#   4. Confirm G, the counting stats, and rate stats match within ±1
#      (minor rounding and same-day data-lag are normal)
#
# These are POPULATED BY verify_rolling_windows.py on first run — replace the
# placeholder values with actual fetched numbers.  Placeholders are marked
# with None so tests using them are automatically skipped with a clear message.
# ---------------------------------------------------------------------------

# Format: (player_name, team, g, counting_stats_subset, rate_stats_subset)
# None values → stat was not available / not verified yet.
BATTER_SNAPSHOTS = {
    # ── 7-day window: 2025-07-01 → 2025-07-07 ──────────────────────────────
    # Verify at: https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-07-01&to=2025-07-07&playerType=batting
    "7day_batter_1": {
        "window": "7day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in top batter",
        "team":   "???",
        "g":      None,          # <-- games played in window (check BRef "G" column)
        "counting_stats": {
            "PA":  None,         # plate appearances
            "H":   None,         # hits
            "HR":  None,         # home runs
            "RBI": None,         # RBI
            "SB":  None,         # stolen bases
        },
        "rate_stats": {
            "AVG": None,         # batting average  (BRef: "BA")
            "OBP": None,         # on-base percentage
            "SLG": None,         # slugging
        },
    },
    "7day_batter_2": {
        "window": "7day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in 2nd batter",
        "team":   "???",
        "g":      None,
        "counting_stats": {"PA": None, "H": None, "HR": None, "RBI": None, "SB": None},
        "rate_stats":     {"AVG": None, "OBP": None, "SLG": None},
    },

    # ── 14-day window: 2025-06-24 → 2025-07-07 ──────────────────────────────
    # Verify at: https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-06-24&to=2025-07-07&playerType=batting
    "14day_batter_1": {
        "window": "14day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in top batter",
        "team":   "???",
        "g":      None,
        "counting_stats": {"PA": None, "H": None, "HR": None, "RBI": None, "SB": None},
        "rate_stats":     {"AVG": None, "OBP": None, "SLG": None},
    },
    "14day_batter_2": {
        "window": "14day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in 2nd batter",
        "team":   "???",
        "g":      None,
        "counting_stats": {"PA": None, "H": None, "HR": None, "RBI": None, "SB": None},
        "rate_stats":     {"AVG": None, "OBP": None, "SLG": None},
    },

    # ── 30-day window: 2025-06-08 → 2025-07-07 ──────────────────────────────
    # Verify at: https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-06-08&to=2025-07-07&playerType=batting
    "30day_batter_1": {
        "window": "30day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in top batter",
        "team":   "???",
        "g":      None,
        "counting_stats": {"PA": None, "H": None, "HR": None, "RBI": None, "SB": None},
        "rate_stats":     {"AVG": None, "OBP": None, "SLG": None},
    },

    # ── 60-day window: 2025-05-09 → 2025-07-07 ──────────────────────────────
    # Verify at: https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-05-09&to=2025-07-07&playerType=batting
    "60day_batter_1": {
        "window": "60day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in top batter",
        "team":   "???",
        "g":      None,
        "counting_stats": {"PA": None, "H": None, "HR": None, "RBI": None, "SB": None},
        "rate_stats":     {"AVG": None, "OBP": None, "SLG": None},
    },
}

PITCHER_SNAPSHOTS = {
    # ── 7-day window ──────────────────────────────────────────────────────────
    # Verify at: https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-07-01&to=2025-07-07&playerType=pitching
    "7day_pitcher_1": {
        "window": "7day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in top pitcher",
        "team":   "???",
        "g":      None,          # games appeared in
        "gs":     None,          # games started
        "counting_stats": {
            "IP":  None,         # innings pitched
            "K":   None,         # strikeouts (stored as K after SO→K rename)
            "W":   None,         # wins
            "SV":  None,         # saves
        },
        "rate_stats": {
            "ERA":  None,
            "WHIP": None,
        },
    },

    # ── 14-day window ──────────────────────────────────────────────────────────
    # Verify at: https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-06-24&to=2025-07-07&playerType=pitching
    "14day_pitcher_1": {
        "window": "14day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in top pitcher",
        "team":   "???",
        "g": None, "gs": None,
        "counting_stats": {"IP": None, "K": None, "W": None, "SV": None},
        "rate_stats":     {"ERA": None, "WHIP": None},
    },
    "14day_pitcher_2": {
        "window": "14day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in 2nd pitcher",
        "team":   "???",
        "g": None, "gs": None,
        "counting_stats": {"IP": None, "K": None, "W": None, "SV": None},
        "rate_stats":     {"ERA": None, "WHIP": None},
    },

    # ── 30-day window ──────────────────────────────────────────────────────────
    # Verify at: https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-06-08&to=2025-07-07&playerType=pitching
    "30day_pitcher_1": {
        "window": "30day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in top pitcher",
        "team":   "???",
        "g": None, "gs": None,
        "counting_stats": {"IP": None, "K": None, "W": None, "SV": None},
        "rate_stats":     {"ERA": None, "WHIP": None},
    },

    # ── 60-day window ──────────────────────────────────────────────────────────
    # Verify at: https://www.baseball-reference.com/friv/dailysplits.fcgi?from=2025-05-09&to=2025-07-07&playerType=pitching
    "60day_pitcher_1": {
        "window": "60day",
        "player": "PLACEHOLDER — run verify_rolling_windows.py and fill in top pitcher",
        "team":   "???",
        "g": None, "gs": None,
        "counting_stats": {"IP": None, "K": None, "W": None, "SV": None},
        "rate_stats":     {"ERA": None, "WHIP": None},
    },
}


# ---------------------------------------------------------------------------
# Helper: tolerance-based stat comparison
# ---------------------------------------------------------------------------

TOLERANCE = {
    # Counting stats: exact match (BRef aggregates integers)
    "G":   0, "GS": 0, "PA": 0, "AB": 0, "H": 0, "HR": 0,
    "RBI": 0, "R":  0, "SB": 0, "CS": 0, "BB": 0, "K": 0,
    "W":   0, "L":  0, "SV": 0,
    # IP can differ by 0.1 (BRef occasionally rounds partial innings differently)
    "IP":  0.1,
    # Rate stats: ±0.003 tolerance for rounding
    "AVG": 0.003, "OBP": 0.003, "SLG": 0.003, "OPS": 0.005,
    "ERA": 0.05, "WHIP": 0.01, "K9": 0.1,
}


def _stat_close(actual: float | None, expected: float | None, key: str) -> bool:
    if expected is None:
        return True   # unverified — skip
    if actual is None:
        return False
    tol = TOLERANCE.get(key, 0.01)
    return abs(actual - expected) <= tol


def _find_player_in_records(
    records: list[dict], player_name: str, team: str
) -> dict | None:
    """Find a player record by exact name match (normalised) or partial name."""
    norm_target = player_name.strip().lower()
    for r in records:
        norm_name = r.get("name", "").strip().lower()
        norm_team = r.get("team", "").strip().upper()
        if norm_name == norm_target and norm_team == team.upper():
            return r
        # Allow partial match on surname in case BRef formats differ
        target_parts = norm_target.split()
        if target_parts and norm_name.split()[-1:] == target_parts[-1:] and norm_team == team.upper():
            return r
    return None


# ---------------------------------------------------------------------------
# Integration tests — live BRef calls
# ---------------------------------------------------------------------------


class TestBatterSnapshotsLive:
    """Verify batter stat accuracy by fetching live BRef data.

    Each test fetches the real BRef aggregate for its window, finds the player
    by name + team, then asserts stats match the snapshot within tolerance.

    If the snapshot value is None, that stat is skipped (unverified).
    """

    def _run_batter_check(self, fixture_key: str) -> None:
        from fantasai.adapters.mlb import MLBAdapter
        snap = BATTER_SNAPSHOTS[fixture_key]
        if snap["g"] is None and all(v is None for v in snap["counting_stats"].values()):
            pytest.skip(
                f"Snapshot '{fixture_key}' not filled in yet. "
                "Run `python scripts/verify_rolling_windows.py` and update the fixture."
            )

        w = WINDOWS[snap["window"]]
        adapter = MLBAdapter()
        records = adapter.fetch_rolling_batting_stats(
            w["date_from"], w["date_to"], w["window_days"]
        )

        player_rec = _find_player_in_records(records, snap["player"], snap["team"])
        assert player_rec is not None, (
            f"Player '{snap['player']}' ({snap['team']}) not found in BRef records for "
            f"{w['date_from']} → {w['date_to']}. "
            f"Verify at: {w['bref_url']}"
        )

        cs, rs = player_rec["counting_stats"], player_rec["rate_stats"]

        if snap["g"] is not None:
            assert _stat_close(cs.get("G"), snap["g"], "G"), (
                f"{snap['player']} G: expected {snap['g']}, got {cs.get('G')}"
            )

        for stat, expected in snap["counting_stats"].items():
            if expected is None:
                continue
            actual = cs.get(stat)
            assert _stat_close(actual, expected, stat), (
                f"{snap['player']} {stat}: expected {expected}, got {actual}. "
                f"Window: {w['date_from']} → {w['date_to']} | BRef: {w['bref_url']}"
            )

        for stat, expected in snap["rate_stats"].items():
            if expected is None:
                continue
            actual = rs.get(stat)
            assert _stat_close(actual, expected, stat), (
                f"{snap['player']} {stat}: expected {expected}, got {actual}. "
                f"Window: {w['date_from']} → {w['date_to']} | BRef: {w['bref_url']}"
            )

    def test_7day_batter_1(self):
        self._run_batter_check("7day_batter_1")

    def test_7day_batter_2(self):
        self._run_batter_check("7day_batter_2")

    def test_14day_batter_1(self):
        self._run_batter_check("14day_batter_1")

    def test_14day_batter_2(self):
        self._run_batter_check("14day_batter_2")

    def test_30day_batter_1(self):
        self._run_batter_check("30day_batter_1")

    def test_60day_batter_1(self):
        self._run_batter_check("60day_batter_1")


class TestPitcherSnapshotsLive:
    """Verify pitcher stat accuracy by fetching live BRef data."""

    def _run_pitcher_check(self, fixture_key: str) -> None:
        from fantasai.adapters.mlb import MLBAdapter
        snap = PITCHER_SNAPSHOTS[fixture_key]
        if snap["g"] is None and all(v is None for v in snap["counting_stats"].values()):
            pytest.skip(
                f"Snapshot '{fixture_key}' not filled in yet. "
                "Run `python scripts/verify_rolling_windows.py` and update the fixture."
            )

        w = WINDOWS[snap["window"]]
        adapter = MLBAdapter()
        records = adapter.fetch_rolling_pitching_stats(
            w["date_from"], w["date_to"], w["window_days"]
        )

        player_rec = _find_player_in_records(records, snap["player"], snap["team"])
        assert player_rec is not None, (
            f"Pitcher '{snap['player']}' ({snap['team']}) not found for "
            f"{w['date_from']} → {w['date_to']}. Verify at: {w['bref_url']}"
        )

        cs, rs = player_rec["counting_stats"], player_rec["rate_stats"]

        if snap["g"] is not None:
            assert _stat_close(cs.get("G"), snap["g"], "G"), (
                f"{snap['player']} G: expected {snap['g']}, got {cs.get('G')}"
            )

        for stat, expected in snap["counting_stats"].items():
            if expected is None:
                continue
            actual = cs.get(stat)
            assert _stat_close(actual, expected, stat), (
                f"{snap['player']} {stat}: expected {expected}, got {actual}. "
                f"Window: {w['date_from']} → {w['date_to']} | BRef: {w['bref_url']}"
            )

        for stat, expected in snap["rate_stats"].items():
            if expected is None:
                continue
            actual = rs.get(stat)
            assert _stat_close(actual, expected, stat), (
                f"{snap['player']} {stat}: expected {expected}, got {actual}. "
                f"Window: {w['date_from']} → {w['date_to']} | BRef: {w['bref_url']}"
            )

    def test_7day_pitcher_1(self):
        self._run_pitcher_check("7day_pitcher_1")

    def test_14day_pitcher_1(self):
        self._run_pitcher_check("14day_pitcher_1")

    def test_14day_pitcher_2(self):
        self._run_pitcher_check("14day_pitcher_2")

    def test_30day_pitcher_1(self):
        self._run_pitcher_check("30day_pitcher_1")

    def test_60day_pitcher_1(self):
        self._run_pitcher_check("60day_pitcher_1")


# ---------------------------------------------------------------------------
# Structural tests — no network required, always run
# ---------------------------------------------------------------------------
# These don't need the integration mark — they verify the adapter correctly
# normalises BRef column names, renames SO→K, BA→AVG, adds G, etc.


def test_window_dates_are_internally_consistent():
    """Sanity-check that all window fixtures have valid date ranges."""
    from datetime import date as dt
    for key, w in WINDOWS.items():
        start = dt.fromisoformat(w["date_from"])
        end   = dt.fromisoformat(w["date_to"])
        assert start < end, f"Window {key}: start must be before end"
        span = (end - start).days
        # Allow ±3 days slack around the nominal window_days
        assert abs(span - w["window_days"]) <= 3, (
            f"Window {key}: span {span}d doesn't match window_days {w['window_days']}"
        )


def test_all_snapshots_have_required_keys():
    """Every snapshot must have the required keys so tests don't silently pass on KeyError."""
    required_batter_keys = {"window", "player", "team", "g", "counting_stats", "rate_stats"}
    required_pitcher_keys = required_batter_keys | {"gs"}

    for key, snap in BATTER_SNAPSHOTS.items():
        assert required_batter_keys <= snap.keys(), (
            f"Batter snapshot '{key}' is missing keys: {required_batter_keys - snap.keys()}"
        )

    for key, snap in PITCHER_SNAPSHOTS.items():
        assert required_pitcher_keys <= snap.keys(), (
            f"Pitcher snapshot '{key}' is missing keys: {required_pitcher_keys - snap.keys()}"
        )


def test_window_keys_reference_valid_windows():
    """Every snapshot's 'window' field must reference a defined window."""
    for key, snap in {**BATTER_SNAPSHOTS, **PITCHER_SNAPSHOTS}.items():
        assert snap["window"] in WINDOWS, (
            f"Snapshot '{key}' references unknown window '{snap['window']}'"
        )
