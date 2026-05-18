"""Visual League Data — pre-computed datasets for all chart visualizations."""
from __future__ import annotations

import logging
import random
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from fantasai.api.deps import get_current_user, get_db
from fantasai.api.v1.scoring_grid import _LOWER_IS_BETTER, _get_conn_and_token
from fantasai.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/visual-data", tags=["visual-data"])

_IGNORE_CATS = {"H/AB", "Batting", "Pitching", "H"}


@router.get("/league")
def get_league_visual_data(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return all pre-computed datasets for league visualizations.

    Computes from ScoringGridSnapshots (no extra Yahoo calls):
    - weekly_allplay   : per-team, per-week all-play W-L-T vs every other team
    - weekly_stats     : raw category stats per team per week
    - cat_allplay      : cumulative all-play W-L-T per team per category
    - h2h_results      : cumulative head-to-head W-L-T between every team pair
    - actual_record    : each team's actual matchup W-L record from the schedule
    """
    from fantasai.models.league import League, Team
    from fantasai.models.matchup import MatchupAnalysis
    from fantasai.models.scoring_grid import ScoringGridSnapshot
    from fantasai.services.scoring_grid_service import _SEASON

    conn, access_token = _get_conn_and_token(user, db)
    league_key = conn.league_key

    league = db.get(League, league_key)
    all_cats = league.scoring_categories if league else []
    lower_is_better = set(_LOWER_IS_BETTER)
    active_cats = [c for c in all_cats if c not in _IGNORE_CATS]

    my_team = (
        db.query(Team)
        .filter(Team.league_id == league_key, Team.owner_user_id == user.id)
        .first()
    )
    my_team_key = my_team.yahoo_team_key if my_team else None

    snapshots = (
        db.query(ScoringGridSnapshot)
        .filter(
            ScoringGridSnapshot.league_id == league_key,
            ScoringGridSnapshot.season == _SEASON,
        )
        .order_by(ScoringGridSnapshot.week)
        .all()
    )

    # ── Build team metadata from all snapshots ──────────────────────────────
    teams_meta: list[dict] = []
    seen_keys: set[str] = set()
    for snap in snapshots:
        for tm in snap.teams_meta or []:
            tk = tm.get("team_key")
            if tk and tk not in seen_keys:
                seen_keys.add(tk)
                teams_meta.append({
                    "team_key": tk,
                    "team_name": tm.get("team_name", tk),
                    "manager_name": tm.get("manager_name"),
                    "is_mine": tk == my_team_key,
                })

    current_week = max((s.week for s in snapshots), default=0)

    # ── Main computation loop ────────────────────────────────────────────────
    weekly_allplay: dict = {}  # {team_key: {week_str: {wins,losses,ties}}}
    weekly_stats: dict = {}    # {team_key: {week_str: {cat: value}}}
    cat_allplay: dict = {}     # {team_key: {cat: {wins,losses,ties}}}
    h2h: dict = {}             # {team_a: {team_b: {wins,losses,ties}}}

    for snap in snapshots:
        week_str = str(snap.week)
        team_stats = snap.team_stats or {}
        team_keys = list(team_stats.keys())

        # Raw stats per team
        for tk, stats in team_stats.items():
            weekly_stats.setdefault(tk, {})[week_str] = {
                cat: stats.get(cat)
                for cat in active_cats
                if stats.get(cat) is not None
            }

        # All-play: every team pair
        for i, t1 in enumerate(team_keys):
            for t2 in team_keys[i + 1:]:
                s1 = team_stats.get(t1, {})
                s2 = team_stats.get(t2, {})
                if not s1 or not s2:
                    continue

                w1 = l1 = tie1 = w2 = l2 = tie2 = 0

                for cat in active_cats:
                    v1 = s1.get(cat)
                    v2 = s2.get(cat)
                    if v1 is None or v2 is None:
                        continue
                    invert = cat in lower_is_better

                    # Per-category accumulators
                    cat_allplay.setdefault(t1, {}).setdefault(cat, {"wins": 0, "losses": 0, "ties": 0})
                    cat_allplay.setdefault(t2, {}).setdefault(cat, {"wins": 0, "losses": 0, "ties": 0})

                    if v1 == v2:
                        cat_allplay[t1][cat]["ties"] += 1
                        cat_allplay[t2][cat]["ties"] += 1
                        tie1 += 1; tie2 += 1
                    elif (v1 > v2) != invert:
                        cat_allplay[t1][cat]["wins"] += 1
                        cat_allplay[t2][cat]["losses"] += 1
                        w1 += 1; l2 += 1
                    else:
                        cat_allplay[t2][cat]["wins"] += 1
                        cat_allplay[t1][cat]["losses"] += 1
                        l1 += 1; w2 += 1

                # Weekly all-play aggregates
                wa = weekly_allplay.setdefault(t1, {}).setdefault(week_str, {"wins": 0, "losses": 0, "ties": 0})
                wa["wins"] += w1; wa["losses"] += l1; wa["ties"] += tie1

                wb = weekly_allplay.setdefault(t2, {}).setdefault(week_str, {"wins": 0, "losses": 0, "ties": 0})
                wb["wins"] += w2; wb["losses"] += l2; wb["ties"] += tie2

                # H2H between this pair (cumulative)
                h2h.setdefault(t1, {}).setdefault(t2, {"wins": 0, "losses": 0, "ties": 0})
                h2h[t1][t2]["wins"] += w1; h2h[t1][t2]["losses"] += l1; h2h[t1][t2]["ties"] += tie1

                h2h.setdefault(t2, {}).setdefault(t1, {"wins": 0, "losses": 0, "ties": 0})
                h2h[t2][t1]["wins"] += w2; h2h[t2][t1]["losses"] += l2; h2h[t2][t1]["ties"] += tie2

    # ── Actual matchup record + weekly actual + per-category actual ──────────
    actual_record: dict = {tm["team_key"]: {"wins": 0, "losses": 0, "ties": 0} for tm in teams_meta}
    weekly_actual: dict = {}  # {team_key: {week_str: {wins,losses,ties}}} — cat W-L-T in real matchup
    cat_actual: dict = {}     # {team_key: {cat: {wins,losses,ties}}}       — cat W-L-T vs real opponent

    stored_matchups = (
        db.query(MatchupAnalysis)
        .filter(MatchupAnalysis.league_id == league_key)
        .all()
    )
    pairings_by_week: dict[int, list] = {}
    for ma in stored_matchups:
        pairings_by_week.setdefault(ma.week, []).append((ma.team1_key, ma.team2_key))

    # ── Back-fill any weeks with snapshot data but no stored pairings ───────────
    # MatchupAnalysis is only written when the Matchup Analyzer is used, so early
    # weeks of the season are often missing.  Fetch those weeks directly from Yahoo.
    from fantasai.services.matchup_service import fetch_league_scoreboard as _fetch_sb

    all_snapshot_weeks = {s.week for s in snapshots}
    missing_weeks = sorted(all_snapshot_weeks - set(pairings_by_week.keys()))
    if missing_weeks and access_token:
        for w in missing_weeks:
            raw = _fetch_sb(access_token, league_key, week=w)
            for m in raw:
                t1 = m.get("team1_key", "")
                t2 = m.get("team2_key", "")
                if t1 and t2:
                    pairings_by_week.setdefault(w, []).append((t1, t2))

    snap_by_week = {s.week: s for s in snapshots}
    for week, pairs in pairings_by_week.items():
        snap = snap_by_week.get(week)
        if not snap:
            continue
        ts = snap.team_stats or {}
        week_str = str(week)
        for t1, t2 in pairs:
            s1 = ts.get(t1, {}); s2 = ts.get(t2, {})
            if not s1 or not s2:
                continue
            w1 = l1 = tie1 = 0
            w2 = l2 = tie2 = 0
            for cat in active_cats:
                v1 = s1.get(cat); v2 = s2.get(cat)
                if v1 is None or v2 is None:
                    continue
                invert = cat in lower_is_better

                cat_actual.setdefault(t1, {}).setdefault(cat, {"wins": 0, "losses": 0, "ties": 0})
                cat_actual.setdefault(t2, {}).setdefault(cat, {"wins": 0, "losses": 0, "ties": 0})

                if v1 == v2:
                    cat_actual[t1][cat]["ties"] += 1
                    cat_actual[t2][cat]["ties"] += 1
                    tie1 += 1; tie2 += 1
                elif (v1 > v2) != invert:
                    cat_actual[t1][cat]["wins"] += 1
                    cat_actual[t2][cat]["losses"] += 1
                    w1 += 1; l2 += 1
                else:
                    cat_actual[t2][cat]["wins"] += 1
                    cat_actual[t1][cat]["losses"] += 1
                    l1 += 1; w2 += 1

            # Weekly actual aggregates (category wins in the real matchup that week)
            wa = weekly_actual.setdefault(t1, {}).setdefault(week_str, {"wins": 0, "losses": 0, "ties": 0})
            wa["wins"] += w1; wa["losses"] += l1; wa["ties"] += tie1

            wb = weekly_actual.setdefault(t2, {}).setdefault(week_str, {"wins": 0, "losses": 0, "ties": 0})
            wb["wins"] += w2; wb["losses"] += l2; wb["ties"] += tie2

            # Overall actual record (who won the matchup)
            if w1 > l1:
                if t1 in actual_record: actual_record[t1]["wins"] += 1
                if t2 in actual_record: actual_record[t2]["losses"] += 1
            elif l1 > w1:
                if t1 in actual_record: actual_record[t1]["losses"] += 1
                if t2 in actual_record: actual_record[t2]["wins"] += 1
            else:
                if t1 in actual_record: actual_record[t1]["ties"] += 1
                if t2 in actual_record: actual_record[t2]["ties"] += 1

    return {
        "teams": teams_meta,
        "current_week": current_week,
        "active_cats": active_cats,
        "weekly_allplay": weekly_allplay,
        "weekly_actual": weekly_actual,
        "weekly_stats": weekly_stats,
        "cat_allplay": cat_allplay,
        "cat_actual": cat_actual,
        "h2h_results": h2h,
        "actual_record": actual_record,
        "my_team_key": my_team_key,
    }


@router.get("/monte-carlo")
def get_monte_carlo(
    season_weeks: int = 22,
    simulations: int = 1000,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Simulate remaining season N times using current all-play win rates as team strength.

    Returns probability distribution of final standings for each team.
    """
    from fantasai.models.league import League, Team
    from fantasai.models.scoring_grid import ScoringGridSnapshot
    from fantasai.services.scoring_grid_service import _SEASON

    conn, _ = _get_conn_and_token(user, db)
    league_key = conn.league_key

    league = db.get(League, league_key)
    all_cats = league.scoring_categories if league else []
    lower_is_better = set(_LOWER_IS_BETTER)
    active_cats = [c for c in all_cats if c not in _IGNORE_CATS]
    n_cats = max(len(active_cats), 1)

    my_team = (
        db.query(Team)
        .filter(Team.league_id == league_key, Team.owner_user_id == user.id)
        .first()
    )
    my_team_key = my_team.yahoo_team_key if my_team else None

    snapshots = (
        db.query(ScoringGridSnapshot)
        .filter(
            ScoringGridSnapshot.league_id == league_key,
            ScoringGridSnapshot.season == _SEASON,
        )
        .order_by(ScoringGridSnapshot.week)
        .all()
    )

    current_week = max((s.week for s in snapshots), default=0)

    # Collect team metadata + all-play totals
    teams_meta: dict[str, dict] = {}
    for snap in snapshots:
        for tm in snap.teams_meta or []:
            tk = tm.get("team_key")
            if tk and tk not in teams_meta:
                teams_meta[tk] = {
                    "team_key": tk,
                    "team_name": tm.get("team_name", tk),
                    "is_mine": tk == my_team_key,
                    "ap_wins": 0, "ap_losses": 0, "ap_ties": 0,
                }

    for snap in snapshots:
        ts = snap.team_stats or {}
        team_keys = list(ts.keys())
        for i, t1 in enumerate(team_keys):
            for t2 in team_keys[i + 1:]:
                s1 = ts.get(t1, {}); s2 = ts.get(t2, {})
                for cat in active_cats:
                    v1 = s1.get(cat); v2 = s2.get(cat)
                    if v1 is None or v2 is None:
                        continue
                    invert = cat in lower_is_better
                    if v1 == v2:
                        if t1 in teams_meta: teams_meta[t1]["ap_ties"] += 1
                        if t2 in teams_meta: teams_meta[t2]["ap_ties"] += 1
                    elif (v1 > v2) != invert:
                        if t1 in teams_meta: teams_meta[t1]["ap_wins"] += 1
                        if t2 in teams_meta: teams_meta[t2]["ap_losses"] += 1
                    else:
                        if t2 in teams_meta: teams_meta[t2]["ap_wins"] += 1
                        if t1 in teams_meta: teams_meta[t1]["ap_losses"] += 1

    all_keys = list(teams_meta.keys())
    n_teams = len(all_keys)
    if n_teams < 2:
        return {"teams": [], "finish_probs": {}, "current_week": current_week,
                "season_weeks": season_weeks, "remaining_weeks": 0}

    remaining = max(0, season_weeks - current_week)

    def strength(tk: str) -> float:
        tm = teams_meta.get(tk, {})
        total = tm["ap_wins"] + tm["ap_losses"] + tm["ap_ties"]
        return (tm["ap_wins"] + 0.5 * tm["ap_ties"]) / total if total else 0.5

    strengths = {tk: strength(tk) for tk in all_keys}

    # finish_counts[team_key][rank_0based] = number of simulations finishing there
    finish_counts: dict[str, list[int]] = {tk: [0] * n_teams for tk in all_keys}
    rng = random.Random(42)  # deterministic seed for reproducibility

    for _ in range(simulations):
        sim_wins = {tk: teams_meta[tk]["ap_wins"] for tk in all_keys}

        for _wk in range(remaining):
            order = list(all_keys)
            rng.shuffle(order)
            # Pair teams: [0,1], [2,3], ...
            for p in range(0, len(order) - 1, 2):
                ta, tb = order[p], order[p + 1]
                sa, sb = strengths[ta], strengths[tb]
                denom = sa + sb or 1.0
                # Simulate each category
                for _ in range(n_cats):
                    r = rng.random()
                    if r < sa / denom:
                        sim_wins[ta] += 1
                    else:
                        sim_wins[tb] += 1

        ranked = sorted(all_keys, key=lambda k: -sim_wins[k])
        for rank, tk in enumerate(ranked):
            finish_counts[tk][rank] += 1

    finish_probs = {
        tk: [round(c / simulations, 4) for c in finish_counts[tk]]
        for tk in all_keys
    }

    team_list = [
        {
            "team_key": tk,
            "team_name": teams_meta[tk]["team_name"],
            "is_mine": teams_meta[tk]["is_mine"],
            "strength": round(strengths[tk], 4),
        }
        for tk in sorted(all_keys, key=lambda k: -strengths[k])
    ]

    return {
        "teams": team_list,
        "finish_probs": finish_probs,
        "current_week": current_week,
        "season_weeks": season_weeks,
        "remaining_weeks": remaining,
        "simulations": simulations,
        "my_team_key": my_team_key,
    }
