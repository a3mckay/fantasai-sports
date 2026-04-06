"""Waiver recommendation API endpoints.

Fetches team/league data from DB, builds WaiverContext, calls the
Recommender, and returns results. The Recommender itself is pure
(no DB dependency).
"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.brain.blurb_generator import get_blurb_generator
from fantasai.brain.recommender import BuildPreferences, Recommender, WaiverContext
from fantasai.brain.strategy import suggest_strategy
from fantasai.config import settings
from fantasai.engine.projection import ProjectionHorizon
from fantasai.engine.scoring import (
    ScoringEngine,
)
from fantasai.models.league import Team
from fantasai.models.player import PlayerRollingStats, PlayerStats
from fantasai.schemas.recommendation import (
    RecommendationRead,
    StrategySuggestionRead,
    WaiverRecommendationRead,
)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

# ---------------------------------------------------------------------------
# Schedule cache — weekly schedule data for This Week rankings
# ---------------------------------------------------------------------------

_SCHEDULE_CACHE: dict[str, tuple[float, dict]] = {}  # key=week_start_iso, value=(ts, overrides)
_SCHEDULE_TTL = 3600  # 1 hour

# ---------------------------------------------------------------------------
# Rankings cache — avoid re-querying 935 PlayerStats on every request
# ---------------------------------------------------------------------------

_RANKINGS_CACHE: dict[str, tuple[float, tuple]] = {}
_RANKINGS_RAW_CACHE: dict[str, tuple[float, tuple]] = {}  # non-deduped, for rankings display
_RANKINGS_TTL = 1800  # 30 minutes — rankings change at most once per pipeline run


def _rankings_cache_key(categories: list[str], horizon: ProjectionHorizon) -> str:
    return f"{','.join(sorted(categories))}|{horizon.value}"


def _current_rankings_cache_key(categories: list[str]) -> str:
    return f"current|{','.join(sorted(categories))}"


def _get_cached_rankings(
    categories: list[str], horizon: ProjectionHorizon
) -> tuple | None:
    key = _rankings_cache_key(categories, horizon)
    entry = _RANKINGS_CACHE.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _RANKINGS_TTL:
        del _RANKINGS_CACHE[key]
        return None
    return value


def _set_cached_rankings(
    categories: list[str], horizon: ProjectionHorizon, value: tuple
) -> None:
    _RANKINGS_CACHE[_rankings_cache_key(categories, horizon)] = (time.monotonic(), value)


def _get_cached_raw_rankings(
    categories: list[str], horizon: ProjectionHorizon
) -> tuple | None:
    """Return the non-deduped (raw) rankings from cache, used by the rankings display endpoint."""
    key = _rankings_cache_key(categories, horizon)
    entry = _RANKINGS_RAW_CACHE.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _RANKINGS_TTL:
        del _RANKINGS_RAW_CACHE[key]
        return None
    return value


def _get_cached_current_rankings(categories: list[str]) -> tuple | None:
    """Return cached current (YTD) rankings."""
    key = _current_rankings_cache_key(categories)
    entry = _RANKINGS_CACHE.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _RANKINGS_TTL:
        del _RANKINGS_CACHE[key]
        return None
    return value


def _set_cached_current_rankings(categories: list[str], value: tuple) -> None:
    _RANKINGS_CACHE[_current_rankings_cache_key(categories)] = (time.monotonic(), value)


# ---------------------------------------------------------------------------
# Helpers — shared DB → rankings pipeline
# ---------------------------------------------------------------------------


def _fetch_team_and_league(team_id: int, db: Session) -> tuple:
    """Fetch Team + League from DB or raise 404."""
    team = db.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail=f"Team {team_id} not found")

    league = team.league
    if not league:
        raise HTTPException(status_code=404, detail="Team has no associated league")

    return team, league


def _compute_rankings(
    db: Session,
    categories: list[str],
    horizon: ProjectionHorizon = ProjectionHorizon.SEASON,
    ranking_type: str = "predictive",
) -> tuple:
    """Compute lookback + predictive rankings from stored PlayerStats.

    Results are cached in-process for 5 minutes, keyed by both category set
    and horizon so that different horizon requests are cached independently.

    When ranking_type == "current", computes YTD (current-season) lookback
    rankings using 2026 actual stats and returns (current_rankings, []).

    Returns (lookback, predictive) or ([], []) if no data.
    """
    if ranking_type == "current":
        cached = _get_cached_current_rankings(categories)
        if cached is not None:
            return cached

        from fantasai.adapters.base import NormalizedPlayerData
        from fantasai.adapters.mlb import MLBAdapter
        from fantasai.models.player import Player

        stats_rows = db.query(PlayerStats).filter(
            PlayerStats.season == 2026,
            PlayerStats.stat_type.in_(["batting", "pitching"]),
            PlayerStats.week.is_(None),
            PlayerStats.data_source == "actual",
        ).all()

        if not stats_rows:
            return [], []

        stat_player_ids = {s.player_id for s in stats_rows}
        player_map = {
            p.player_id: p
            for p in db.query(Player).filter(Player.player_id.in_(stat_player_ids)).all()
        }

        # Minimum sample thresholds for Current Season rankings.
        # A light floor to filter out truly micro-samples (e.g. a guy who went
        # 2-for-3 with a HR in his first game ranking #1 on 3 PA).
        # Intentionally conservative — early-season hot starts on real volume
        # (e.g. 3 HR in 25 PA) are legitimate accumulated stats and should rank.
        # 20 PA ≈ 5–6 games; 5 IP ≈ one quality start.
        _MIN_BATTER_PA = 20
        _MIN_PITCHER_IP = 5.0

        players = []
        for stats in stats_rows:
            player = player_map.get(stats.player_id)
            if not player:
                continue
            counting = stats.counting_stats or {}
            # Filter out players with no meaningful stats (all zeros / empty)
            has_stats = any(
                v is not None and float(v) > 0
                for v in counting.values()
                if v is not None
            )
            if not has_stats:
                continue
            # Enforce minimum sample — tiny samples produce misleading z-scores
            if stats.stat_type == "batting":
                pa = float(counting.get("PA") or 0)
                if pa < _MIN_BATTER_PA:
                    continue
            else:
                ip = float(counting.get("IP") or 0)
                if ip < _MIN_PITCHER_IP:
                    continue
            players.append(
                NormalizedPlayerData(
                    player_id=stats.player_id,
                    name=player.name,
                    team=player.team,
                    positions=player.positions or [],
                    stat_type=stats.stat_type,
                    counting_stats=counting,
                    rate_stats=stats.rate_stats or {},
                    advanced_stats=stats.advanced_stats or {},
                )
            )

        if not players:
            return [], []

        adapter = MLBAdapter()
        engine = ScoringEngine(adapter, categories)
        current_rankings = engine.compute_lookback_rankings(2026, players=players)

        # Deduplicate two-way players (keep higher-scoring entry)
        seen: dict = {}
        for r in current_rankings:
            if r.player_id not in seen or r.score > seen[r.player_id].score:
                seen[r.player_id] = r
        deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        for i, r in enumerate(deduped):
            r.overall_rank = i + 1

        result = (deduped, [])
        _set_cached_current_rankings(categories, result)
        return result

    cached = _get_cached_rankings(categories, horizon)
    if cached is not None:
        return cached

    from fantasai.adapters.base import NormalizedPlayerData
    from fantasai.adapters.mlb import MLBAdapter
    from fantasai.models.player import Player

    # ── Load 2026 YTD actual stats (lookback signal) ───────────────────────
    actual_rows = db.query(PlayerStats).filter(
        PlayerStats.season == 2026,
        PlayerStats.stat_type.in_(["batting", "pitching"]),
        PlayerStats.data_source == "actual",
    ).all()

    # ── Load 2026 Steamer projections (forward-looking talent signal) ──────
    # Kept separate from actuals so the two can coexist.
    # Steamer-only players (prospects / MiLB) who have no actual rows are
    # included in the predictive player pool.
    steamer_rows = db.query(PlayerStats).filter(
        PlayerStats.season == 2026,
        PlayerStats.stat_type.in_(["batting", "pitching"]),
        PlayerStats.data_source == "projection",
    ).all()

    if not actual_rows and not steamer_rows:
        return [], []

    # Batch-load all referenced players
    all_stat_player_ids = {s.player_id for s in actual_rows} | {s.player_id for s in steamer_rows}
    player_map = {
        p.player_id: p
        for p in db.query(Player).filter(Player.player_id.in_(all_stat_player_ids)).all()
    }

    # Build player list from actual rows (lookback) first
    players = []
    ytd_player_ids: set[int] = set()
    for stats in actual_rows:
        player = player_map.get(stats.player_id)
        if not player:
            continue
        players.append(
            NormalizedPlayerData(
                player_id=stats.player_id,
                name=player.name,
                team=player.team,
                positions=player.positions or [],
                stat_type=stats.stat_type,
                counting_stats=stats.counting_stats or {},
                rate_stats=stats.rate_stats or {},
                advanced_stats=stats.advanced_stats or {},
            )
        )
        ytd_player_ids.add(stats.player_id)

    # Early in the season before any actuals are synced, fall back to projection rows
    if not players:
        for stats in steamer_rows:
            player = player_map.get(stats.player_id)
            if not player:
                continue
            players.append(
                NormalizedPlayerData(
                    player_id=stats.player_id,
                    name=player.name,
                    team=player.team,
                    positions=player.positions or [],
                    stat_type=stats.stat_type,
                    counting_stats=stats.counting_stats or {},
                    rate_stats=stats.rate_stats or {},
                    advanced_stats=stats.advanced_stats or {},
                )
            )
            ytd_player_ids.add(stats.player_id)

    if not players:
        return [], []

    full_player_map = player_map  # already contains all players

    steamer_lookup: dict[int, NormalizedPlayerData] = {}
    _steamer_only_ids: set[int] = set()  # players with Steamer projection but no 2026 actuals
    for stats in steamer_rows:
        player = full_player_map.get(stats.player_id)
        if not player:
            continue
        nd = NormalizedPlayerData(
            player_id=stats.player_id,
            name=player.name,
            team=player.team,
            positions=player.positions or [],
            stat_type=stats.stat_type,
            counting_stats=stats.counting_stats or {},
            rate_stats=stats.rate_stats or {},
            advanced_stats=stats.advanced_stats or {},
        )
        steamer_lookup[stats.player_id] = nd
        # Add Steamer-only players (prospects) to the predictive player pool.
        # They have no 2026 YTD actuals — the projection functions will fall
        # back entirely to the Steamer talent signal.
        # Weekly horizon: defer the decision to after we have the schedule
        # (we only add them then if they have confirmed MLB games this week).
        if stats.player_id not in ytd_player_ids:
            players.append(nd)
            _steamer_only_ids.add(stats.player_id)

    # ── Merge injury / risk-flag data into NormalizedPlayerData ────────────
    # InjuryRecord holds current IL status; Player.risk_flag holds chronic
    # risk profiles.  Both are set via POST /rankings/set-injury|set-risk-flag.
    from fantasai.models.player import InjuryRecord
    injury_records: dict[int, InjuryRecord] = {
        ir.player_id: ir
        for ir in db.query(InjuryRecord).all()
    }
    for nd in players:
        db_player = full_player_map.get(nd.player_id)
        if db_player:
            nd.risk_flag = db_player.risk_flag
            nd.risk_note = db_player.risk_note
        ir = injury_records.get(nd.player_id)
        if ir:
            nd.injury_status = ir.status
            nd.injury_return_date = ir.return_date

    import copy
    from fantasai.engine.projection import HORIZON_CONFIGS
    from fantasai.engine.schedule import (
        fetch_weekly_schedule,
        build_week_configs,
        get_current_week_bounds,
    )

    # ── Build schedule overrides for This Week horizon ──────────────────────
    week_configs: dict | None = None
    _week_schedule: dict | None = None  # raw PlayerSchedule map for factor injection
    if horizon == ProjectionHorizon.WEEK:
        try:
            week_start, week_end = get_current_week_bounds()
            cache_key_sched = week_start.isoformat()
            sched_entry = _SCHEDULE_CACHE.get(cache_key_sched)
            if sched_entry is not None:
                sched_ts, _cached_pair = sched_entry
                if time.monotonic() - sched_ts <= _SCHEDULE_TTL:
                    week_configs, _week_schedule = _cached_pair
            if week_configs is None:
                _week_schedule = fetch_weekly_schedule(
                    week_start, week_end, db,
                    vegas_api_key=settings.the_odds_api_key or None,
                )
                week_configs = build_week_configs(_week_schedule, HORIZON_CONFIGS[ProjectionHorizon.WEEK])
                _SCHEDULE_CACHE[cache_key_sched] = (time.monotonic(), (week_configs, _week_schedule))
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Schedule fetch failed, ranking without overrides: %s", exc
            )
            week_configs = None
            _week_schedule = None

        # Ensure every pitcher in the player pool has an explicit week_configs
        # entry so none fall through to the base config (sp_ip=6.0).
        #
        # Case 1: schedule came back completely empty (preseason / API outage) →
        #   zero ALL SP innings.
        # Case 2: schedule has data but some pitchers weren't matched (team
        #   abbreviation mismatch, call-up not yet in our DB, etc.) →
        #   zero just those uncovered pitchers.
        #
        # Either way, every pitcher who doesn't have a confirmed probable start
        # this week should project for 0 SP IP.
        if week_configs is not None:
            import logging as _logging
            from dataclasses import replace as _replace
            _base = HORIZON_CONFIGS[ProjectionHorizon.WEEK]
            _zero_sp = _replace(_base, sp_ip=0.0)
            # Pitchers not covered by week_configs AND not in the schedule at all
            # get zeroed out — they have no confirmed start this week.
            #
            # IMPORTANT: a pitcher whose config exactly matches the base config
            # (e.g. 1 start × 6 IP, 6 team_games) is intentionally omitted from
            # build_week_configs() as an optimization.  Such players ARE in the
            # schedule and should NOT be zeroed — they'll use the base config
            # (sp_ip=6.0) which is already correct for a 1-start week.
            _in_schedule: set[int] = set(_week_schedule.keys()) if _week_schedule else set()
            uncovered = [
                nd.player_id
                for nd in players
                if nd.stat_type == "pitching"
                and nd.player_id not in week_configs
                and nd.player_id not in _in_schedule
            ]
            if uncovered:
                if not _week_schedule:
                    _logging.getLogger(__name__).info(
                        "Week schedule is empty — zeroing SP ip for all %d pitchers",
                        len(uncovered),
                    )
                else:
                    _logging.getLogger(__name__).debug(
                        "Zeroing SP ip for %d pitchers not found in schedule",
                        len(uncovered),
                    )
                for pid in uncovered:
                    week_configs[pid] = _zero_sp

    # ── Exclude MiLB-only players from This Week rankings ───────────────────
    # Steamer-only players (no 2026 actual stats) are MiLB / not yet on an
    # MLB roster.  They cannot contribute to H2H weekly categories so ranking
    # them alongside MLB players is misleading (e.g. Domínguez at #61 because
    # his Steamer talent signal is elite).
    # For month/season horizons they stay in (call-up upside matters there).
    if horizon == ProjectionHorizon.WEEK and _steamer_only_ids:
        if _week_schedule:
            # Keep a Steamer-only player only if they actually appear in the
            # MLB schedule this week — that confirms they're on a roster.
            players = [
                nd for nd in players
                if nd.player_id not in _steamer_only_ids
                or nd.player_id in _week_schedule
            ]
        else:
            # No schedule data (API outage / preseason): exclude all MiLB-only
            players = [nd for nd in players if nd.player_id not in _steamer_only_ids]

    # ── Inject weather/Vegas factors into NormalizedPlayerData ──────────────
    if horizon == ProjectionHorizon.WEEK and _week_schedule:
        for nd in players:
            ps = _week_schedule.get(nd.player_id)
            if ps is not None:
                nd.week_hr_factor = ps.weather_hr_factor
                nd.week_run_factor = ps.vegas_run_factor

    adapter = MLBAdapter()
    engine = ScoringEngine(adapter, categories)
    lookback = engine.compute_lookback_rankings(2026, players=players)
    predictive = engine.compute_predictive_rankings(
        2026, players=players, horizon=horizon, steamer_lookup=steamer_lookup,
        schedule_overrides=week_configs or None,
    )

    # Store non-deduped (raw) rankings before deduplication so the rankings
    # display endpoint can show two-way players (e.g. Ohtani) as both a batter
    # and a pitcher. Use shallow copies so _dedup's overall_rank mutations on
    # the originals don't affect the raw copies.
    def _rerank_raw(rnks: list) -> list:
        raw = sorted([copy.copy(r) for r in rnks], key=lambda r: r.score, reverse=True)
        for i, r in enumerate(raw):
            r.overall_rank = i + 1
        return raw

    lb_raw   = _rerank_raw(lookback)
    pred_raw = _rerank_raw(predictive)
    _key = _rankings_cache_key(categories, horizon)
    _RANKINGS_RAW_CACHE[_key] = (time.monotonic(), (lb_raw, pred_raw))

    # Deduplicate: two-way players (e.g. Ohtani) have both batting and pitching
    # rows, producing two ranking entries with the same player_id. Keep the
    # higher-scoring entry and re-assign overall ranks.
    def _dedup(rnks: list) -> list:
        seen: dict = {}
        for r in rnks:
            if r.player_id not in seen or r.score > seen[r.player_id].score:
                seen[r.player_id] = r
        deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        for i, r in enumerate(deduped):
            r.overall_rank = i + 1
        return deduped

    result = (_dedup(lookback), _dedup(predictive))
    _set_cached_rankings(categories, horizon, result)
    return result


def _compute_projection_rankings(
    db: Session,
    categories: list[str],
    projection_season: int = 2026,
) -> list:
    """Compute predictive rankings from consensus projection rows (season=2026).

    Used by keeper evaluation to produce forward-looking scores. Uses the same
    ``compute_predictive_rankings`` path as the main rankings so that:

    - The ``effective_pa`` cap prevents part-time players with high per-PA rates
      (e.g. a 60-PA speedster) from inflating the pool mean with phantom counting
      stats when scaled to a 540-PA season.
    - SP/RP IP volume bounds are respected (62 IP for RP, 170 IP for SP).
    - Rate-to-counting-stat projection formulas match the Projected rankings tab.

    The 2026 consensus rows serve as BOTH the player pool and the steamer_lookup
    (talent signal) so the blend reduces to the projection values directly — there
    are no 2026 YTD actuals to mix in.

    Falls back to an empty list if no projection rows are found for the given
    season (caller should then fall back to _compute_rankings predictive).

    Results are cached for 30 minutes under a separate key.
    """
    cache_key = f"proj|{','.join(sorted(categories))}|{projection_season}"
    entry = _RANKINGS_CACHE.get(cache_key)
    if entry is not None:
        ts, value = entry
        if time.monotonic() - ts <= _RANKINGS_TTL:
            return value

    from fantasai.adapters.base import NormalizedPlayerData
    from fantasai.adapters.mlb import MLBAdapter
    from fantasai.models.player import Player

    stats_rows = db.query(PlayerStats).filter(
        PlayerStats.season == projection_season,
        PlayerStats.stat_type.in_(["batting", "pitching"]),
        PlayerStats.week.is_(None),
    ).all()

    if not stats_rows:
        return []

    stat_player_ids = [s.player_id for s in stats_rows]
    player_map = {
        p.player_id: p
        for p in db.query(Player).filter(Player.player_id.in_(stat_player_ids)).all()
    }

    players = []
    for stats in stats_rows:
        player = player_map.get(stats.player_id)
        if not player:
            continue
        players.append(
            NormalizedPlayerData(
                player_id=stats.player_id,
                name=player.name,
                team=player.team,
                positions=player.positions or [],
                stat_type=stats.stat_type,
                counting_stats=stats.counting_stats or {},
                rate_stats=stats.rate_stats or {},
                advanced_stats=stats.advanced_stats or {},
            )
        )

    if not players:
        return []

    # ── Merge injury / risk-flag data (same logic as _compute_rankings) ─────
    # Without this, _availability_multiplier() always sees risk_flag=None and
    # injury_status="active", so Glasnow's fragile flag and current IL players
    # have zero effect on projection-based rankings.
    from fantasai.models.player import InjuryRecord
    injury_records_proj: dict[int, InjuryRecord] = {
        ir.player_id: ir
        for ir in db.query(InjuryRecord).all()
    }
    for nd in players:
        db_player = player_map.get(nd.player_id)
        if db_player:
            nd.risk_flag = db_player.risk_flag
            nd.risk_note = db_player.risk_note
        ir = injury_records_proj.get(nd.player_id)
        if ir:
            nd.injury_status = ir.status
            nd.injury_return_date = ir.return_date

    # Build steamer_lookup from the same rows so the projection engine's
    # talent signal == the projection data (no separate YTD actuals to blend).
    steamer_lookup = {p.player_id: p for p in players}

    adapter = MLBAdapter()
    engine = ScoringEngine(adapter, categories)
    # Use compute_predictive_rankings so effective_pa capping, SP/RP IP
    # volume bounds, and rate-to-counting projection formulas all apply —
    # matching the behaviour of the Projected rankings tab.
    rankings = engine.compute_predictive_rankings(
        projection_season,
        players=players,
        horizon=ProjectionHorizon.SEASON,
        steamer_lookup=steamer_lookup,
    )

    seen: dict = {}
    for r in rankings:
        if r.player_id not in seen or r.score > seen[r.player_id].score:
            seen[r.player_id] = r
    deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)
    for i, r in enumerate(deduped):
        r.overall_rank = i + 1

    _RANKINGS_CACHE[cache_key] = (time.monotonic(), deduped)
    return deduped


def _inject_prospect_rankings(
    rankings: list,
    db: Session,
) -> list:
    """Load ProspectProfile records and insert MiLB prospects into the MLB ranking list.

    Prospects are positioned at their PAV proxy rank (e.g. Griffin PAV 84 → rank ~72)
    regardless of whether their FanGraphs projection placed them higher or lower.
    After insertion the list is sorted by overall_rank and renumbered sequentially.

    IMPORTANT: This function returns a NEW list built from shallow copies of the
    incoming PlayerRanking objects so the in-process rankings cache is never mutated.
    """
    import dataclasses as _dc
    from fantasai.models.player import Player, PlayerStats
    from fantasai.models.prospect import ProspectProfile
    from fantasai.engine.scoring import PlayerRanking

    # Work on shallow copies of the cached objects so that sort(), append(), and
    # field mutations here don't corrupt the shared _RANKINGS_CACHE entries.
    working: list = [_dc.replace(r, is_prospect=False, pav_score=None) for r in rankings]

    # Build a set of player_ids that have 2026 ACTUAL stats in our DB.
    # True MiLB prospects only have 2026 Steamer projection data (season=2026, week=None via steamer).
    # Established MLB players (Henderson, Soto, etc.) will have 2026 YTD actuals once synced.
    players_with_2026_stats: set[int] = {
        pid for (pid,) in db.query(PlayerStats.player_id)
        .filter(PlayerStats.season == 2026, PlayerStats.week.is_(None))
        .distinct()
        .all()
    }

    # Index working copies by player_id
    existing_by_id: dict[int, object] = {r.player_id: r for r in working}

    profiles = (
        db.query(ProspectProfile, Player)
        .join(Player, Player.player_id == ProspectProfile.player_id)
        .filter(ProspectProfile.pav_score.isnot(None))
        .all()
    )

    for pp, player in profiles:
        proxy = pp.proxy_mlb_rank or 999
        if player.player_id in existing_by_id:
            # Already in MLB rankings (has FanGraphs projections).
            # Only tag as a prospect if they lack 2026 actual stats (i.e. they
            # haven't played meaningful MLB time yet) and PAV is substantive.
            if (
                player.player_id not in players_with_2026_stats
                and (pp.pav_score or 0) >= 30.0
            ):
                existing = existing_by_id[player.player_id]
                existing.is_prospect = True
                existing.pav_score = pp.pav_score
                # Override the FanGraphs-derived rank with the PAV proxy rank so
                # prospects appear where their talent warrants rather than where
                # FanGraphs' conservative projection places them.
                existing.overall_rank = proxy
            continue

        # Pure MiLB player — inject a new entry at their PAV-equivalent rank.
        pr = PlayerRanking(
            player_id=player.player_id,
            name=player.name,
            team=player.team,
            positions=list(player.positions or []),
            stat_type=pp.stat_type,
            overall_rank=proxy,
            position_rank=0,
            score=round((pp.pav_score or 0) / 10.0, 3),
            raw_score=round((pp.pav_score or 0) / 10.0, 3),
            category_contributions={},
            injury_status="active",
            risk_flag=player.risk_flag,
            risk_note=player.risk_note,
            is_prospect=True,
            pav_score=pp.pav_score,
        )
        working.append(pr)

    # Sort by PAV/FanGraphs rank (prospects win ties) then renumber sequentially.
    working.sort(key=lambda r: (r.overall_rank, 0 if r.is_prospect else 1))
    for i, r in enumerate(working):
        r.overall_rank = i + 1

    return working


def _fetch_rolling_windows_map(
    db: Session,
    player_ids: list[int],
    season: int = 2026,
    windows: list[int] | None = None,
) -> dict[int, dict[str, dict[str, float]]]:
    """Build the rolling_windows_map for a set of player IDs.

    Returns {player_id: {"Last 14 days": {"HR": 4, "AVG": .301, ...}, ...}}
    Only includes windows where data exists for the player.
    """
    if windows is None:
        windows = [14, 30]  # most useful for blurb context; 7-day is noisy

    window_label = {14: "Last 14 days", 30: "Last 30 days", 60: "Last 60 days"}

    rows = (
        db.query(PlayerRollingStats)
        .filter(
            PlayerRollingStats.player_id.in_(player_ids),
            PlayerRollingStats.season == season,
            PlayerRollingStats.window_days.in_(windows),
        )
        .all()
    )

    result: dict[int, dict[str, dict[str, float]]] = {}
    for row in rows:
        label = window_label.get(row.window_days, f"Last {row.window_days} days")
        stats = {**row.counting_stats, **row.rate_stats}
        if row.window_rank:
            stats["rank"] = float(row.window_rank)
        result.setdefault(row.player_id, {})[label] = stats

    return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{team_id}", response_model=list[RecommendationRead])
def get_recommendations(team_id: int) -> list:
    """Legacy endpoint — returns persisted recommendations."""
    return []


@router.get(
    "/{team_id}/waivers",
    response_model=list[WaiverRecommendationRead],
    summary="Get waiver wire recommendations for a team",
)
def get_waiver_recommendations(
    team_id: int,
    limit: int = Query(default=15, ge=1, le=50),
    pitcher_strategy: str = Query(default="balanced", pattern="^(balanced|rp_heavy|sp_heavy)$"),
    punt_positions: Optional[str] = Query(default=None, description="Comma-separated positions to punt, e.g. 'C,2B'"),
    punt_categories: Optional[str] = Query(default=None, description="Comma-separated categories to punt, e.g. 'SB,AVG'"),
    priority_targets: Optional[str] = Query(default=None, description="Comma-separated priority categories, e.g. 'SV,K'"),
    db: Session = Depends(get_db),
) -> list[WaiverRecommendationRead]:
    """Generate real-time waiver recommendations for a team.

    Fetches the team's roster and league config from the DB,
    computes fresh rankings, and runs the recommender algorithm.
    Optionally accepts build preferences via query parameters.
    """
    team, league = _fetch_team_and_league(team_id, db)

    # Gather all rostered player IDs across the league
    all_rostered: set[int] = set()
    for t in league.teams:
        all_rostered.update(t.roster or [])

    categories = league.scoring_categories or []

    # Build rankings from stored PlayerStats
    # TODO: cache rankings or compute on data refresh instead of per-request
    lookback, predictive = _compute_rankings(db, categories)
    if not lookback:
        return []

    # Parse build preferences from query params
    preferences = BuildPreferences(
        pitcher_strategy=pitcher_strategy,
        punt_positions=[p.strip() for p in punt_positions.split(",")] if punt_positions else [],
        punt_categories=[c.strip() for c in punt_categories.split(",")] if punt_categories else [],
        priority_targets=[t.strip() for t in priority_targets.split(",")] if priority_targets else [],
    )

    # Build pitcher IP map from the 14-day rolling window (most relevant for
    # weekly IP floor checks).  Falls back to an empty dict if no rolling data.
    roster_ids = team.roster or []
    team_pitcher_ip: dict[int, float] = {}
    if roster_ids:
        rolling_map = _fetch_rolling_windows_map(db, roster_ids, windows=[14])
        for pid, windows in rolling_map.items():
            ip = windows.get("Last 14 days", {}).get("IP")
            if ip is not None:
                team_pitcher_ip[pid] = float(ip)

    # Build context
    max_acq = (league.settings or {}).get("max_acquisitions_per_week", 4)
    context = WaiverContext(
        team_id=team_id,
        roster_player_ids=roster_ids,
        league_type=league.league_type,
        scoring_categories=categories,
        roster_positions=league.roster_positions or [],
        max_acquisitions_remaining=max_acq,
        all_rankings=lookback,
        predictive_rankings=predictive,
        all_rostered_ids=all_rostered,
        build_preferences=preferences,
        team_pitcher_ip=team_pitcher_ip,
    )

    # Run recommender
    recommender = Recommender(categories, league_type=league.league_type)
    recommendations = recommender.get_waiver_recommendations(context, limit=limit)

    # Generate LLM blurbs for each recommendation if an API key is configured.
    # Uses parallel requests so all blurbs complete in ~1–2 seconds total.
    blurbs: dict[int, str] = {}
    if settings.anthropic_api_key:
        try:
            # Build synthetic PlayerRanking objects for blurb generation
            # from the predictive rankings (forward-looking is more useful here).
            pred_by_id = {r.player_id: r for r in predictive}
            rec_rankings = [
                pred_by_id[r.player_id]
                for r in recommendations
                if r.player_id in pred_by_id
            ]
            if rec_rankings:
                gen = get_blurb_generator(api_key=settings.anthropic_api_key)
                # Fetch rolling window data for richer blurb context
                rec_ids = [r.player_id for r in rec_rankings]
                rolling_map = _fetch_rolling_windows_map(db, rec_ids)
                # Single-call: all blurbs in one request so the model
                # can vary language across the set (no repeated phrases).
                blurbs = gen.generate_blurbs_single_call(
                    rec_rankings,
                    ranking_type="predictive",
                    scoring_categories=categories,
                    rolling_windows_map=rolling_map or None,
                    top_n=0,  # 0 = generate for all provided
                )
        except Exception as exc:  # never block the response on blurb failure
            import logging
            logging.getLogger(__name__).warning("Blurb generation failed: %s", exc)

    # Convert to response schema
    return [
        WaiverRecommendationRead(
            player_id=r.player_id,
            player_name=r.player_name,
            team=r.team,
            positions=r.positions,
            priority_score=r.priority_score,
            category_impact=r.category_impact,
            fills_positions=r.fills_positions,
            weak_categories_addressed=r.weak_categories_addressed,
            drop_candidates=[
                {
                    "player_id": d.player_id,
                    "player_name": d.player_name,
                    "positions": d.positions,
                    "current_score": d.current_score,
                    "category_contributions": d.category_contributions,
                    "net_impact": d.net_impact,
                    "ip_warning": d.ip_warning,
                }
                for d in r.drop_candidates
            ],
            action=r.action,
            # LLM blurb takes priority; fall back to algorithmic rationale if unavailable.
            rationale_blurb=blurbs.get(r.player_id) or r.rationale_blurb,
        )
        for r in recommendations
    ]


@router.get(
    "/{team_id}/strategy",
    response_model=StrategySuggestionRead,
    summary="Get auto-detected build strategy suggestion for a team",
)
def get_strategy_suggestion(
    team_id: int,
    db: Session = Depends(get_db),
) -> StrategySuggestionRead:
    """Analyze a team's roster and suggest an optimal build strategy.

    Examines roster composition and category strengths to infer
    what build the manager appears to be running, then suggests
    preferences that align with and optimize that build.
    """
    team, league = _fetch_team_and_league(team_id, db)
    categories = league.scoring_categories or []

    lookback, _ = _compute_rankings(db, categories)
    if not lookback:
        raise HTTPException(
            status_code=404,
            detail="No player stats available to analyze strategy",
        )

    # Filter to just this team's players
    roster_ids = set(team.roster or [])
    roster_rankings = [r for r in lookback if r.player_id in roster_ids]

    if not roster_rankings:
        raise HTTPException(
            status_code=404,
            detail="No ranked players found on this team's roster",
        )

    # Run strategy suggester
    suggestion = suggest_strategy(
        roster_rankings=roster_rankings,
        scoring_categories=categories,
        roster_positions=league.roster_positions or [],
        league_type=league.league_type,
    )

    return StrategySuggestionRead(
        pitcher_strategy=suggestion.preferences.pitcher_strategy,
        punt_positions=suggestion.preferences.punt_positions,
        punt_categories=suggestion.preferences.punt_categories,
        priority_targets=suggestion.preferences.priority_targets,
        reasoning=suggestion.reasoning,
        confidence=suggestion.confidence,
    )
