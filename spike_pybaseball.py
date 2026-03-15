"""
pybaseball Data Exploration Spike
=================================
Goal: Validate that pybaseball gives us the data we need for FantasAI Sports.

What we're checking:
1. Season batting stats — columns available, player count, gaps
2. Season pitching stats — same
3. Statcast data — xwOBA, xBA, Barrel%, Hard Hit%, etc.
4. Data structure and quality

Uses 2025 data if available, falls back to 2024.
Saves sample CSVs for manual inspection.
"""

import os
import sys
import warnings
from datetime import datetime

import pandas as pd

# Suppress noisy warnings from pybaseball
warnings.filterwarnings("ignore")

# pybaseball caches data locally — enable it to avoid repeated downloads
from pybaseball import cache

cache.enable()

from pybaseball import (
    batting_stats,
    pitching_stats,
    statcast,
    playerid_lookup,
    statcast_batter,
    statcast_pitcher,
)

OUTPUT_DIR = "spike_output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Stats we need per the PRD
NEEDED_HITTING_STATS = [
    "PA", "AB", "R", "H", "HR", "RBI", "SB", "BB", "SO",  # counting
    "AVG", "OBP", "SLG", "OPS",  # rate
    "wOBA", "wRC+",  # advanced
]

NEEDED_PITCHING_STATS = [
    "IP", "W", "L", "SV", "HLD", "SO", "BB",  # counting
    "ERA", "WHIP", "FIP", "xFIP",  # rate/advanced
    "K/9", "BB/9", "K/BB", "HR/9",  # rate
]

NEEDED_STATCAST_HITTING = [
    "xwoba", "xba", "xslg",  # expected stats
    "barrel_batted_rate", "hard_hit_percent",  # contact quality
    "sprint_speed",  # speed
    "pull_percent", "straightaway_percent", "opposite_percent",  # spray
    "groundballs_percent", "flyballs_percent", "linedrives_percent",  # batted ball
    "bat_speed", "swing_length",  # swing metrics
    "whiff_percent", "chase_rate",  # plate discipline
]

NEEDED_STATCAST_PITCHING = [
    "xera", "xfip",  # expected stats
    "xwoba",  # expected woba against
    "barrel_batted_rate", "hard_hit_percent",  # contact quality allowed
    "k_percent", "bb_percent",  # rate stats
    "groundballs_percent", "flyballs_percent", "linedrives_percent",  # batted ball
    "whiff_percent", "chase_rate",  # swing-and-miss
]


def section(title):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}\n")


def check_columns(df, needed, label):
    """Check which needed columns exist in a DataFrame."""
    available = set(df.columns)
    found = [c for c in needed if c in available]
    missing = [c for c in needed if c not in available]

    print(f"  {label}:")
    print(f"    Found {len(found)}/{len(needed)} needed columns")
    if missing:
        print(f"    MISSING: {missing}")
    else:
        print(f"    All needed columns present!")
    return found, missing


def try_season(year):
    """Try to pull season-level stats for a given year."""
    try:
        print(f"  Trying {year} batting stats...")
        bat = batting_stats(year, qual=50)
        print(f"    Success: {len(bat)} players, {len(bat.columns)} columns")
        return bat, year
    except Exception as e:
        print(f"    Failed for {year}: {e}")
        return None, year


# ============================================================
# 1. Season Batting Stats (FanGraphs via pybaseball)
# ============================================================
section("1. SEASON BATTING STATS")

batting_df = None
season_year = None

for year in [2025, 2024]:
    batting_df, season_year = try_season(year)
    if batting_df is not None:
        break

if batting_df is not None:
    print(f"\n  Using {season_year} season data")
    print(f"  Shape: {batting_df.shape}")
    print(f"\n  All columns ({len(batting_df.columns)}):")
    for i, col in enumerate(sorted(batting_df.columns)):
        print(f"    {col}", end="  ")
        if (i + 1) % 5 == 0:
            print()
    print()

    found, missing = check_columns(batting_df, NEEDED_HITTING_STATS, "Hitting stats check")

    # Save sample
    csv_path = os.path.join(OUTPUT_DIR, f"batting_{season_year}_sample.csv")
    batting_df.head(50).to_csv(csv_path, index=False)
    print(f"\n  Saved top 50 batters to {csv_path}")
else:
    print("  ERROR: Could not pull batting stats for any year")


# ============================================================
# 2. Season Pitching Stats (FanGraphs via pybaseball)
# ============================================================
section("2. SEASON PITCHING STATS")

pitching_df = None
for year in [2025, 2024]:
    try:
        print(f"  Trying {year} pitching stats...")
        pitching_df = pitching_stats(year, qual=30)
        print(f"    Success: {len(pitching_df)} players, {len(pitching_df.columns)} columns")
        season_year_p = year
        break
    except Exception as e:
        print(f"    Failed for {year}: {e}")

if pitching_df is not None:
    print(f"\n  Using {season_year_p} season data")
    print(f"  Shape: {pitching_df.shape}")
    print(f"\n  All columns ({len(pitching_df.columns)}):")
    for i, col in enumerate(sorted(pitching_df.columns)):
        print(f"    {col}", end="  ")
        if (i + 1) % 5 == 0:
            print()
    print()

    found, missing = check_columns(pitching_df, NEEDED_PITCHING_STATS, "Pitching stats check")

    csv_path = os.path.join(OUTPUT_DIR, f"pitching_{season_year_p}_sample.csv")
    pitching_df.head(50).to_csv(csv_path, index=False)
    print(f"\n  Saved top 50 pitchers to {csv_path}")


# ============================================================
# 3. Statcast Data (Baseball Savant)
# ============================================================
section("3. STATCAST PLAY-BY-PLAY DATA")

print("  Pulling a small Statcast sample (1 week of data)...")
try:
    # Pull ~1 week of recent data to see column structure
    # Use a known date range with games
    sc_df = statcast(start_dt="2024-09-01", end_dt="2024-09-07")
    print(f"  Success: {len(sc_df)} pitches/events, {len(sc_df.columns)} columns")
    print(f"\n  All Statcast columns ({len(sc_df.columns)}):")
    for i, col in enumerate(sorted(sc_df.columns)):
        print(f"    {col}", end="  ")
        if (i + 1) % 4 == 0:
            print()
    print()

    # Check for key Statcast metrics
    key_statcast_cols = [
        "launch_speed", "launch_angle", "estimated_ba_using_speedangle",
        "estimated_woba_using_speedangle", "barrel",
        "release_speed", "release_spin_rate", "pfx_x", "pfx_z",
        "bat_speed", "swing_length",
    ]
    found_sc = [c for c in key_statcast_cols if c in sc_df.columns]
    missing_sc = [c for c in key_statcast_cols if c not in sc_df.columns]
    print(f"\n  Key Statcast columns:")
    print(f"    Found: {found_sc}")
    if missing_sc:
        print(f"    Missing: {missing_sc}")

    csv_path = os.path.join(OUTPUT_DIR, "statcast_sample.csv")
    sc_df.head(200).to_csv(csv_path, index=False)
    print(f"\n  Saved 200 events to {csv_path}")

except Exception as e:
    print(f"  ERROR pulling Statcast data: {e}")


# ============================================================
# 4. Statcast Season-Level Aggregates (Expected Stats)
# ============================================================
section("4. STATCAST EXPECTED STATS (Season Aggregates)")

print("  Checking if pybaseball provides season-level Statcast aggregates...")
print("  (xwOBA, xBA, xSLG, Barrel%, Hard Hit% per player)")

try:
    from pybaseball import statcast_batter_expected_stats

    xstats = statcast_batter_expected_stats(2024, minPA=50)
    print(f"  Success: {len(xstats)} players, {len(xstats.columns)} columns")
    print(f"\n  Columns: {list(xstats.columns)}")

    check_columns(
        xstats,
        ["est_woba", "est_ba", "est_slg", "brl_percent"],
        "Expected stats check",
    )

    csv_path = os.path.join(OUTPUT_DIR, "expected_stats_2024.csv")
    xstats.head(50).to_csv(csv_path, index=False)
    print(f"\n  Saved top 50 to {csv_path}")

except ImportError:
    print("  statcast_batter_expected_stats not available in this version")
except Exception as e:
    print(f"  Error: {e}")

# Also check pitcher expected stats
try:
    from pybaseball import statcast_pitcher_expected_stats

    xstats_p = statcast_pitcher_expected_stats(2024, minPA=100)
    print(f"\n  Pitcher expected stats: {len(xstats_p)} players, {len(xstats_p.columns)} columns")
    print(f"  Columns: {list(xstats_p.columns)}")

    csv_path = os.path.join(OUTPUT_DIR, "expected_stats_pitchers_2024.csv")
    xstats_p.head(50).to_csv(csv_path, index=False)
    print(f"  Saved top 50 to {csv_path}")

except ImportError:
    print("  statcast_pitcher_expected_stats not available")
except Exception as e:
    print(f"  Error: {e}")


# ============================================================
# 5. Player ID Lookup
# ============================================================
section("5. PLAYER ID LOOKUP")

print("  Testing player ID lookup (Shohei Ohtani)...")
try:
    ohtani = playerid_lookup("ohtani", "shohei")
    print(f"  Result:\n{ohtani.to_string()}")
    print(f"\n  Columns available: {list(ohtani.columns)}")
    print("  This gives us cross-reference IDs (MLB, FanGraphs, etc.)")
except Exception as e:
    print(f"  Error: {e}")


# ============================================================
# 6. Summary & Recommendations
# ============================================================
section("SUMMARY & DATA AVAILABILITY ASSESSMENT")

print("""
  DATA SOURCE ASSESSMENT FOR FANTASAI SPORTS MVP:

  FanGraphs (via batting_stats/pitching_stats):
  - Season-level batting and pitching stats: comprehensive
  - Includes advanced metrics (wOBA, wRC+, FIP, xFIP, etc.)
  - Good for: lookback rankings, season-to-date analysis
  - Qualifier filters available (min PA/IP)

  Statcast (via statcast):
  - Pitch-by-pitch play-by-play data: very detailed
  - Includes: exit velocity, launch angle, expected stats per event
  - Need to aggregate ourselves for season-level predictive metrics
  - Good for: building predictive models from raw data

  Statcast Expected Stats (season-level):
  - Pre-aggregated xwOBA, xBA, xSLG, Barrel% per player
  - Critical for predictive rankings
  - Available for both batters and pitchers

  Player IDs:
  - Cross-reference lookup available (MLB ID, FanGraphs ID, etc.)
  - Needed for joining data across sources

  GAPS TO INVESTIGATE:
  - Sprint speed: may need separate Statcast aggregation
  - Stuff+ for pitchers: check if available via FanGraphs stats
  - Schedule data: pybaseball may have this, or use MLB API directly
  - Real-time update cadence during season: unknown until season starts
""")

print("Done! Check the spike_output/ directory for CSV files.")
