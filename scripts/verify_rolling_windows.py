"""Rolling-window stat verification script.

Fetches real Baseball Reference data for four specific 2025 date windows and
prints a formatted report you can cross-reference directly against BRef.

USAGE
-----
    python scripts/verify_rolling_windows.py

    # Generate blurbs for the first few players (requires ANTHROPIC_API_KEY):
    ANTHROPIC_API_KEY=sk-... python scripts/verify_rolling_windows.py --blurbs

CROSS-REFERENCING ON BREF
--------------------------
For each window the script prints a direct BRef URL:
    https://www.baseball-reference.com/leagues/daily.fcgi
        ?user_team=&bust_cache=&type=b&lastn=0&dates=fromandto
        &fromandto=YYYY-MM-DD.YYYY-MM-DD&Sortby=...

Or use the "Batting Splits" finder (easier):
    https://www.baseball-reference.com/friv/dailysplits.fcgi
    → set "From" / "To" dates, choose Batting or Pitching, hit search.

The "G" column in the output is games played in that window — useful for
confirming you've found the right player in the right date range.

WINDOWS
-------
    7-day  : 2025-07-01 → 2025-07-07  (7 games into July, ~2.5-week All-Star break zone)
    14-day : 2025-06-24 → 2025-07-07  (two weeks straddling the pre-All-Star stretch)
    30-day : 2025-06-08 → 2025-07-07  (one calendar month)
    60-day : 2025-05-09 → 2025-07-07  (two calendar months, roughly May + June)
"""
from __future__ import annotations

import argparse
import os
import sys

# Make sure the package is importable when run from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fantasai.adapters.mlb import MLBAdapter

# ---------------------------------------------------------------------------
# Verification windows — edit these if you want a different reference point.
# All dates are inclusive.  "label" appears in output headers.
# ---------------------------------------------------------------------------
WINDOWS = [
    {"window_days": 7,  "start": "2025-07-01", "end": "2025-07-07", "label": " 7-day"},
    {"window_days": 14, "start": "2025-06-24", "end": "2025-07-07", "label": "14-day"},
    {"window_days": 30, "start": "2025-06-08", "end": "2025-07-07", "label": "30-day"},
    {"window_days": 60, "start": "2025-05-09", "end": "2025-07-07", "label": "60-day"},
]

TOP_BATTERS   = 12   # how many batters to show per window
TOP_PITCHERS  = 8    # how many pitchers to show per window
BLURBS_TOP_N  = 3    # how many blurbs to generate per window (if --blurbs)

BREF_DAILY_URL = (
    "https://www.baseball-reference.com/friv/dailysplits.fcgi"
    "?from={start}&to={end}&playerType={player_type}"
)

# Sorting key for batters: HR desc, then AVG desc
def _batter_sort(r: dict) -> tuple:
    cs, rs = r.get("counting_stats", {}), r.get("rate_stats", {})
    return (
        -cs.get("HR", 0),
        -cs.get("H", 0),
        -rs.get("AVG", 0.0),
    )

# Sorting key for pitchers: ERA asc, then K desc
def _pitcher_sort(r: dict) -> tuple:
    cs, rs = r.get("counting_stats", {}), r.get("rate_stats", {})
    return (
        rs.get("ERA", 99.0),
        -cs.get("K", 0),
    )


def _fmt(val: float | None, decimals: int = 0) -> str:
    if val is None:
        return "  —  "
    if decimals == 3:
        return f"{val:.3f}".lstrip("0") or ".000"  # strip leading zero → .310
    return f"{val:.{decimals}f}"


def _print_section(title: str) -> None:
    bar = "═" * 72
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar)


def _print_batters(records: list[dict], top_n: int) -> None:
    sorted_recs = sorted(records, key=_batter_sort)[:top_n]
    if not sorted_recs:
        print("  (no batters found for this window — check min PA threshold)")
        return

    header = (
        f"  {'#':>3}  {'Name':<22} {'Tm':>4}  {'G':>3}  {'PA':>4}  "
        f"{'H':>4}  {'HR':>4}  {'RBI':>4}  {'SB':>3}  "
        f"{'AVG':>6}  {'OBP':>6}  {'SLG':>6}"
    )
    print(header)
    print("  " + "-" * 70)

    for i, r in enumerate(sorted_recs, 1):
        cs, rs = r.get("counting_stats", {}), r.get("rate_stats", {})
        print(
            f"  {i:>3}.  {r['name']:<22} {r['team']:>4}  "
            f"{_fmt(cs.get('G')):>3}  {_fmt(cs.get('PA')):>4}  "
            f"{_fmt(cs.get('H')):>4}  {_fmt(cs.get('HR')):>4}  "
            f"{_fmt(cs.get('RBI')):>4}  {_fmt(cs.get('SB')):>3}  "
            f"{_fmt(rs.get('AVG'), 3):>6}  "
            f"{_fmt(rs.get('OBP'), 3):>6}  "
            f"{_fmt(rs.get('SLG'), 3):>6}"
        )


def _print_pitchers(records: list[dict], top_n: int) -> None:
    sorted_recs = sorted(records, key=_pitcher_sort)[:top_n]
    if not sorted_recs:
        print("  (no pitchers found for this window — check min IP threshold)")
        return

    header = (
        f"  {'#':>3}  {'Name':<22} {'Tm':>4}  {'G':>3}  {'GS':>3}  "
        f"{'IP':>6}  {'K':>4}  {'W':>3}  {'SV':>3}  "
        f"{'ERA':>6}  {'WHIP':>6}"
    )
    print(header)
    print("  " + "-" * 70)

    for i, r in enumerate(sorted_recs, 1):
        cs, rs = r.get("counting_stats", {}), r.get("rate_stats", {})
        print(
            f"  {i:>3}.  {r['name']:<22} {r['team']:>4}  "
            f"{_fmt(cs.get('G')):>3}  {_fmt(cs.get('GS')):>3}  "
            f"{_fmt(cs.get('IP'), 1):>6}  {_fmt(cs.get('K')):>4}  "
            f"{_fmt(cs.get('W')):>3}  {_fmt(cs.get('SV')):>3}  "
            f"{_fmt(rs.get('ERA'), 2):>6}  "
            f"{_fmt(rs.get('WHIP'), 3):>6}"
        )


def _generate_blurbs(
    batters: list[dict],
    pitchers: list[dict],
    window: dict,
    api_key: str,
    top_n: int = BLURBS_TOP_N,
) -> None:
    """Generate sample blurbs for the top batters and pitchers in a window."""
    from datetime import date
    from fantasai.brain.blurb_generator import BlurbGenerator
    from fantasai.engine.scoring import PlayerRanking, ScoringEngine

    CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "W", "SV", "K", "ERA", "WHIP"]
    engine = ScoringEngine(MLBAdapter(), CATEGORIES)

    # Build synthetic PlayerRanking objects from the BRef records
    # (no DB — just use the stats directly for blurb context)
    all_records = batters[:top_n] + pitchers[:top_n]

    window_label = f"Last {window['window_days']} days ({window['start']} – {window['end']})"
    rolling_windows_map: dict[int, dict] = {}

    rankings = []
    for idx, rec in enumerate(all_records):
        cs = rec.get("counting_stats", {})
        rs = rec.get("rate_stats", {})
        combined: dict[str, float] = {**cs, **rs}

        # Use idx as a synthetic player_id (just for this blurb run)
        pid = idx + 1
        rolling_windows_map[pid] = {window_label: combined}

        # Build minimal ranking object so the blurb generator has something to work with
        rank = PlayerRanking(
            player_id=pid,
            name=rec["name"],
            team=rec.get("team", ""),
            positions=rec.get("positions", []),
            stat_type=rec["stat_type"],
            overall_rank=idx + 1,
            position_rank=0,
            score=0.0,
            raw_score=0.0,
            category_contributions={},
        )
        rankings.append(rank)

    gen = BlurbGenerator(api_key=api_key)
    print(f"\n  Generating {len(rankings)} blurbs via claude-sonnet-4-6...")
    blurbs = gen.generate_blurbs_single_call(
        rankings,
        ranking_type="lookback",
        scoring_categories=CATEGORIES,
        rolling_windows_map=rolling_windows_map,
        top_n=0,  # all provided
    )

    print()
    for r in rankings:
        blurb = blurbs.get(r.player_id, "(blurb generation failed)")
        print(f"\n  [{r.stat_type.upper()}] {r.name} — {r.team}")
        print(f"  {blurb}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify rolling-window stats against BRef")
    parser.add_argument(
        "--blurbs",
        action="store_true",
        help="Generate LLM blurbs for top players in each window (requires ANTHROPIC_API_KEY)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if args.blurbs and not api_key:
        print("ERROR: --blurbs requires ANTHROPIC_API_KEY to be set in the environment.")
        sys.exit(1)

    adapter = MLBAdapter()

    for w in WINDOWS:
        bref_bat_url  = BREF_DAILY_URL.format(start=w["start"], end=w["end"], player_type="batting")
        bref_pit_url  = BREF_DAILY_URL.format(start=w["start"], end=w["end"], player_type="pitching")

        _print_section(
            f"WINDOW: {w['label']}  |  {w['start']}  →  {w['end']}  ({w['window_days']} calendar days)"
        )
        print(f"\n  Cross-reference (batting):  {bref_bat_url}")
        print(f"  Cross-reference (pitching): {bref_pit_url}\n")

        # ---- batters ----
        try:
            batters = adapter.fetch_rolling_batting_stats(w["start"], w["end"], w["window_days"])
            print(f"  BATTERS  (fetched {len(batters)}, showing top {TOP_BATTERS} by HR/H/AVG, "
                  f"min {10 if w['window_days'] == 7 else 20 if w['window_days'] == 14 else 40 if w['window_days'] == 30 else 80} PA)")
            _print_batters(batters, TOP_BATTERS)
        except Exception as exc:
            print(f"  ⚠ Failed to fetch batting stats: {exc}")
            batters = []

        print()

        # ---- pitchers ----
        try:
            pitchers = adapter.fetch_rolling_pitching_stats(w["start"], w["end"], w["window_days"])
            print(f"  PITCHERS  (fetched {len(pitchers)}, showing top {TOP_PITCHERS} by ERA, "
                  f"min {3.0 if w['window_days'] == 7 else 5.0 if w['window_days'] == 14 else 10.0 if w['window_days'] == 30 else 18.0} IP)")
            _print_pitchers(pitchers, TOP_PITCHERS)
        except Exception as exc:
            print(f"  ⚠ Failed to fetch pitching stats: {exc}")
            pitchers = []

        # ---- blurbs (optional) ----
        if args.blurbs and api_key and (batters or pitchers):
            _generate_blurbs(batters, pitchers, w, api_key)

    print("\n" + "═" * 72)
    print("  Done. To verify: use the BRef URLs above, enter dates exactly as shown,")
    print("  and match player rows by Name + Team + G (games played).")
    print("═" * 72 + "\n")


if __name__ == "__main__":
    main()
