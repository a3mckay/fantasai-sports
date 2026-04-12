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
from fantasai.schemas.team_analysis import (
    RosterAnalysisResponse,
    RosterPlayerRead,
    RosterSlotRead,
    TradeTargetRead,
    WaiverUpgradeRead,
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


def get_cached_week_schedule() -> dict:
    """Return the cached {player_id: PlayerSchedule} for the current week.

    Returns {} if the cache is cold — callers should handle gracefully.
    Does not fetch; read-only access to the warm rankings cache.
    """
    import time as _time
    from fantasai.engine.schedule import get_current_week_bounds
    week_start, _ = get_current_week_bounds()
    cache_key = week_start.isoformat()
    entry = _SCHEDULE_CACHE.get(cache_key)
    if entry is not None:
        ts, cached_pair = entry
        if _time.monotonic() - ts <= _SCHEDULE_TTL:
            _, week_schedule = cached_pair
            return week_schedule or {}
    return {}


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

    # Pre-compute each player's primary stat type from their positions.
    # FanGraphs ingests batting rows for ALL players including pitchers (and
    # occasionally pitching rows for position players).  Without this guard,
    # Kyle Freeland-style pitchers end up with a stat_type='batting' ranking
    # entry that passes the "Batters" position filter on the rankings page.
    _pitcher_pos = {"SP", "RP", "P"}
    def _primary_stat_type(player) -> str:
        positions = player.positions or []
        return "pitching" if any(p.upper() in _pitcher_pos for p in positions) else "batting"

    # Build player list from actual rows (lookback) first
    players = []
    ytd_player_ids: set[int] = set()
    for stats in actual_rows:
        player = player_map.get(stats.player_id)
        if not player:
            continue
        # Skip rows whose stat_type doesn't match the player's primary type.
        if stats.stat_type != _primary_stat_type(player):
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
    # True MiLB prospects only have Steamer projection data; established MLB
    # players have actual YTD rows once the stats pipeline runs.  Filtering
    # to data_source="actual" ensures Steamer-only players (e.g. Walker Jenkins
    # with proj PA=46 but no real MLB games) are correctly flagged is_prospect.
    players_with_2026_stats: set[int] = {
        pid for (pid,) in db.query(PlayerStats.player_id)
        .filter(
            PlayerStats.season == 2026,
            PlayerStats.week.is_(None),
            PlayerStats.data_source == "actual",
        )
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


def _fetch_raw_stats_map(
    db: Session,
    player_ids: list[int],
    season: int = 2026,
) -> dict[int, dict[str, float]]:
    """Fetch season stats for players and return as flat dicts for blurb grounding.

    Returns {player_id: {stat_label: value, ...}} where stat_label includes
    a sample-size prefix like "[2026 actual — 42G, 156PA]" so the LLM knows
    whether it's reading real season data or a Steamer projection.

    This prevents blurb hallucinations: the model can only cite numbers that
    appear here. If a stat is not in this dict, the writer persona prohibits
    citing it.
    """
    if not player_ids:
        return {}

    _pitcher_positions = {"SP", "RP", "P"}

    rows = (
        db.query(PlayerStats)
        .filter(
            PlayerStats.player_id.in_(player_ids),
            PlayerStats.season == season,
            PlayerStats.week.is_(None),
        )
        .all()
    )

    # Group by player_id, prefer actual over projection, pick stat_type by position
    from fantasai.models.player import Player as _Player
    position_map: dict[int, list[str]] = {}
    try:
        player_rows = db.query(_Player.player_id, _Player.positions).filter(
            _Player.player_id.in_(player_ids)
        ).all()
        for pid, positions in player_rows:
            position_map[pid] = positions or []
    except Exception:
        pass

    rows_by_player: dict[int, list] = {}
    for row in rows:
        rows_by_player.setdefault(row.player_id, []).append(row)

    result: dict[int, dict[str, float]] = {}
    for pid in player_ids:
        player_rows_list = rows_by_player.get(pid, [])
        if not player_rows_list:
            continue

        positions = position_map.get(pid, [])
        is_pitcher = any(p.upper() in _pitcher_positions for p in positions)
        primary_stat_type = "pitching" if is_pitcher else "batting"

        # Prefer actual rows, then fall back to projection
        actual_rows = [r for r in player_rows_list if r.data_source == "actual" and r.stat_type == primary_stat_type]
        proj_rows = [r for r in player_rows_list if r.data_source == "projection" and r.stat_type == primary_stat_type]

        if not actual_rows and not proj_rows:
            # Try any stat_type
            actual_rows = [r for r in player_rows_list if r.data_source == "actual"]
            proj_rows = [r for r in player_rows_list if r.data_source == "projection"]

        stats_row = actual_rows[0] if actual_rows else (proj_rows[0] if proj_rows else None)
        if not stats_row:
            continue

        is_actual = stats_row.data_source == "actual"
        counting = stats_row.counting_stats or {}
        rate = stats_row.rate_stats or {}
        advanced = stats_row.advanced_stats or {}

        flat: dict[str, float] = {}

        if primary_stat_type == "pitching":
            ip = float(counting.get("IP", 0) or 0)
            gs = int(float(counting.get("GS", 0) or 0))
            if is_actual:
                flat[f"[2026 actual — {gs} GS, {ip:.1f} IP]"] = 0.0
            else:
                flat["[2026 Steamer projection — full-season]"] = 0.0

            for k in ["ERA", "WHIP", "K9", "K/9"]:
                v = rate.get(k)
                if v is not None:
                    try:
                        flat[k] = round(float(v), 2)
                    except (TypeError, ValueError):
                        pass
            for k in ["xERA", "xFIP", "SIERA", "FIP"]:
                v = advanced.get(k)
                if v is not None:
                    try:
                        flat[k] = round(float(v), 2)
                    except (TypeError, ValueError):
                        pass
            prefix = "" if is_actual else "proj-"
            for k in ["W", "SV", "K", "IP", "GS"]:
                v = counting.get(k)
                if v is not None:
                    try:
                        flat[f"{prefix}{k}"] = round(float(v), 1) if k == "IP" else int(float(v))
                    except (TypeError, ValueError):
                        pass
        else:
            pa = int(float(counting.get("PA", 0) or counting.get("AB", 0) or 0))
            g = int(float(counting.get("G", 0) or 0))
            if is_actual:
                flat[f"[2026 actual — {g} G, {pa} PA]"] = 0.0
            else:
                flat["[2026 Steamer projection — full-season]"] = 0.0

            for k in ["AVG", "OBP", "SLG"]:
                v = rate.get(k)
                if v is not None:
                    try:
                        flat[k] = round(float(v), 3)
                    except (TypeError, ValueError):
                        pass
            for k in ["xwOBA", "xBA", "xSLG", "wRC+"]:
                v = advanced.get(k)
                if v is not None:
                    try:
                        flat[k] = round(float(v), 3)
                    except (TypeError, ValueError):
                        pass
            prefix = "" if is_actual else "proj-"
            for k in ["H", "HR", "R", "RBI", "SB", "PA", "G"]:
                v = counting.get(k)
                if v is not None:
                    try:
                        flat[f"{prefix}{k}"] = int(float(v))
                    except (TypeError, ValueError):
                        pass

        if flat:
            result[pid] = flat

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
                rec_ids = [r.player_id for r in rec_rankings]
                # Fetch season stats (actual or Steamer) — grounds the model so it
                # can only cite numbers that actually exist for this player.
                raw_map = _fetch_raw_stats_map(db, rec_ids)
                # Rolling windows add recent-form context on top of season stats.
                rolling_map = _fetch_rolling_windows_map(db, rec_ids)
                # Single-call: all blurbs in one request so the model
                # can vary language across the set (no repeated phrases).
                blurbs = gen.generate_blurbs_single_call(
                    rec_rankings,
                    ranking_type="predictive_season",
                    scoring_categories=categories,
                    raw_stats_map=raw_map or None,
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


# ---------------------------------------------------------------------------
# Endpoint: Roster Analysis
# ---------------------------------------------------------------------------

_ASSESSMENT_PRIORITY = {"empty": 0, "weak": 1, "average": 2, "solid": 3, "elite": 4}

# Categories that are display artefacts / not real scoring categories.
# Filter these from all response fields that surface category names.
_JUNK_CATS = frozenset({"H/AB", "Batting", "Pitching", "AB"})

# Roster slot types that don't benefit from position-specific upgrade suggestions.
_NO_UPGRADE_SLOTS = frozenset({"BN"})


@router.get(
    "/{team_id}/roster-analysis",
    response_model=RosterAnalysisResponse,
    summary="Roster analysis with per-slot waiver and trade upgrade recommendations",
)
def roster_analysis(
    team_id: int,
    db: Session = Depends(get_db),
) -> RosterAnalysisResponse:
    """Evaluate the team's roster and surface targeted upgrade options for every slot.

    Upgrades (waivers + trade targets) are surfaced for every position group, not
    only weak/empty ones — even an elite C might have a better free agent available.
    BN/bench slots are shown but skipped for upgrades (any position can fill BN).

    Junk display categories (H/AB, Batting, Pitching, AB) are stripped from all
    category fields.  NA/IL stash suggestions are appended when the league has
    those roster spots.
    """
    from collections import Counter, defaultdict

    from fantasai.api.v1.analysis import _apply_team_weights  # lazy — avoids circular import
    from fantasai.brain.recommender import _player_eligible_for_slot
    from fantasai.brain.team_evaluator import _compute_group_scores, evaluate_team

    import logging as _log_ra
    _logger_ra = _log_ra.getLogger(__name__)

    team, league = _fetch_team_and_league(team_id, db)
    # Strip display-only artefacts (H/AB, Batting, Pitching, AB) before any
    # computation so they don't pollute z-score means, ranking cache keys, or
    # category display fields.
    categories = [c for c in (league.scoring_categories or []) if c not in _JUNK_CATS]
    roster_positions = league.roster_positions or []
    league_type = league.league_type or "h2h_categories"

    # ── Early guard: empty roster (before the expensive rankings fetch) ───────
    player_ids = team.roster or []
    if not player_ids:
        raise HTTPException(
            status_code=404,
            detail="Team has no rostered players — sync your league first.",
        )

    # ── Rankings ──────────────────────────────────────────────────────────────
    lookback, predictive = _compute_rankings(db, categories)
    if not predictive:
        raise HTTPException(status_code=404, detail="No player stats available for rankings.")

    ranking_map = {r.player_id: r for r in predictive}

    raw_pair = _get_cached_raw_rankings(categories, ProjectionHorizon.SEASON)
    raw_source = raw_pair[1] if raw_pair else predictive
    raw_multi: dict = defaultdict(list)
    for r in raw_source:
        raw_multi[r.player_id].append(r)

    # ── My Roster Rankings ────────────────────────────────────────────────────
    il_ids = list(team.il_player_ids or [])
    injured_stats = dict(team.injured_player_statuses or {})
    consumed: dict = defaultdict(int)

    roster_rankings, _, _ = _apply_team_weights(
        player_ids, ranking_map, raw_multi, consumed,
        roster_positions, il_ids, injured_stats,
    )

    # ── League-relative context for grading ──────────────────────────────────
    # Without this, evaluate_team uses absolute z-score thresholds and most
    # teams cluster in the B/average band.  Build per-team overall scores and
    # per-position mean scores across the whole league so grades are relative.
    league_team_scores: list[float] = []
    league_pos_scores: dict[str, list[float]] = {}
    for t in league.teams:
        t_pids = list(t.roster or [])
        t_rankings = [ranking_map[pid] for pid in t_pids if pid in ranking_map]
        if not t_rankings:
            continue
        league_team_scores.append(sum(r.score for r in t_rankings) / len(t_rankings))
        group_data = _compute_group_scores(t_rankings, categories)
        for pos, (_, _, mean_score) in group_data.items():
            league_pos_scores.setdefault(pos, []).append(mean_score)

    # ── Team Evaluation ───────────────────────────────────────────────────────
    evaluation = evaluate_team(
        roster_rankings=roster_rankings,
        categories=categories,
        roster_positions=roster_positions,
        league_type=league_type,
        league_team_scores=league_team_scores or None,
        league_position_mean_scores=league_pos_scores or None,
    )

    # ── Collect all rostered player IDs across league ─────────────────────────
    all_rostered: set[int] = set()
    for t in league.teams:
        all_rostered.update(t.roster or [])

    # ── Injury data for all relevant players ──────────────────────────────────
    from fantasai.models.player import InjuryRecord as _InjuryRecord
    injury_map: dict[int, _InjuryRecord] = {
        ir.player_id: ir
        for ir in db.query(_InjuryRecord).all()
    }

    # Map Yahoo's injury designations (DTD, Q, O) to our internal status strings.
    _YAHOO_STATUS_MAP = {
        "O":   "il_10",
        "IL":  "il_10",
        "DTD": "day_to_day",
        "Q":   "day_to_day",
    }

    def _injury_fields(player_id: int) -> dict:
        ir = injury_map.get(player_id)
        if ir is not None:
            return {"injury_status": ir.status, "injury_note": ir.injury_description}
        # Fallback: Yahoo-reported status stored on the team object.
        yahoo = (
            injured_stats.get(str(player_id))
            or injured_stats.get(player_id)
        )
        if yahoo:
            internal = _YAHOO_STATUS_MAP.get(str(yahoo).upper())
            if internal:
                return {"injury_status": internal, "injury_note": str(yahoo)}
        return {"injury_status": None, "injury_note": None}

    # ── Available (unrostered) players sorted by score ─────────────────────────
    # Used for direct score-based upgrade candidates — bypasses the category-fit
    # filter in Recommender so that even strong teams always see options.
    # Exclude players on 60-day IL or out for the season — they can't contribute
    # now and shouldn't appear as active waiver/trade targets. (10-day IL and
    # day-to-day are kept since they may return within the week.)
    _LONG_TERM_INJURY = frozenset({"il_60", "out_for_season"})
    available_by_score = sorted(
        [
            r for r in predictive
            if r.player_id not in all_rostered
            and (
                r.player_id not in injury_map
                or injury_map[r.player_id].status not in _LONG_TERM_INJURY
            )
        ],
        key=lambda r: r.score,
        reverse=True,
    )

    # ── Trade target pool ─────────────────────────────────────────────────────
    _DIFF_ORDER = {"possible": 0, "hard": 1, "unrealistic": 2}

    # Absolute rank thresholds — elite players are unrealistic regardless of
    # positional depth on the other team (e.g. Yordan Alvarez with 4 OF teammates
    # should not show as "possible").
    _all_scores = sorted([r.score for r in predictive], reverse=True)
    _top25_threshold = _all_scores[24] if len(_all_scores) >= 25 else 0.0
    _top60_threshold = _all_scores[59] if len(_all_scores) >= 60 else 0.0

    def _rank_blurb(r: "PlayerRanking") -> str:
        """Short projection context for display."""
        top_cats = [
            cat for cat, val in sorted(
                r.category_contributions.items(), key=lambda x: x[1], reverse=True
            )
            if val > 0 and cat not in _JUNK_CATS
        ][:2]
        cat_str = f" · strong in {', '.join(top_cats)}" if top_cats else ""
        return f"Ranked #{r.overall_rank} overall{cat_str}"

    trade_pool: dict[int, dict] = {}
    for other_team in (league.teams or []):
        if other_team.team_id == team_id:
            continue
        other_ids = other_team.roster or []
        if not other_ids:
            continue

        other_rankings = [ranking_map[pid] for pid in other_ids if pid in ranking_map]
        if not other_rankings:
            continue

        top_score = max((r.score for r in other_rankings), default=0.0)

        for r in other_rankings:
            # Count teammates who can play the target's PRIMARY position only.
            # Using full set-intersection inflates depth (e.g. a SS/2B/3B player
            # would count all infielders as "same position"), making "they have 8
            # at that spot" style messages badly misleading.
            primary_pos = r.positions[0] if r.positions else None
            same_pos = (
                [x for x in other_rankings if primary_pos and primary_pos in (x.positions or [])]
                if primary_pos else [r]
            )
            same_pos_top = max((x.score for x in same_pos), default=0.0)
            pos_label = primary_pos or "that position"

            # Absolute elite threshold takes priority over positional logic
            if _top25_threshold > 0 and r.score >= _top25_threshold:
                difficulty = "unrealistic"
                reason = f"Top-25 player overall — not realistically available"
            elif _top60_threshold > 0 and r.score >= _top60_threshold:
                difficulty = "hard"
                reason = f"Top-60 player — unlikely to be moved"
            elif len(same_pos) == 1:
                difficulty = "unrealistic"
                reason = f"Only {pos_label} on their roster"
            elif top_score > 0 and r.score >= top_score * 0.92:
                difficulty = "unrealistic"
                reason = "Their best player overall"
            elif same_pos_top > 0 and r.score >= same_pos_top * 0.92:
                difficulty = "hard"
                reason = f"Best {pos_label} on their roster"
            else:
                n = len(same_pos)
                difficulty = "possible"
                reason = f"Depth at {pos_label} — they have {n} at that spot"

            inj = _injury_fields(r.player_id)
            trade_pool[r.player_id] = {
                "player_id": r.player_id,
                "player_name": r.name,
                "positions": r.positions,
                "score": round(r.score, 3),
                "owner_team_name": other_team.team_name,
                "owner_team_id": other_team.team_id,
                "difficulty": difficulty,
                "difficulty_reason": reason,
                "injury_status": inj["injury_status"],
                "injury_note": inj["injury_note"],
                "blurb": _rank_blurb(r),
            }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _top_cats(r: "PlayerRanking", n: int = 3) -> list[str]:
        return [
            cat for cat, val in sorted(
                r.category_contributions.items(), key=lambda x: x[1], reverse=True
            )
            if val > 0 and cat not in _JUNK_CATS
        ][:n]

    def _build_upgrades(
        slot_pos: str,
        min_score: float = 0.0,
    ) -> tuple[list[WaiverUpgradeRead], list[TradeTargetRead]]:
        """Return (waiver_upgrades, trade_targets) for a given slot position.

        min_score: only return candidates whose score exceeds this threshold.
        Pass the current occupant's score so we never suggest a player who is
        worse than (or equal to) who's already in the slot.
        """
        if slot_pos in _NO_UPGRADE_SLOTS:
            return [], []

        # Waiver upgrades: top 3 unrostered players eligible for this slot
        # who are strictly better than the current occupant.
        slot_waivers = [
            r for r in available_by_score
            if _player_eligible_for_slot(r.positions, slot_pos)
            and r.score > min_score
        ][:3]
        waiver_upgrades = [
            WaiverUpgradeRead(
                player_id=r.player_id,
                player_name=r.name,
                positions=r.positions,
                score=round(r.score, 3),
                category_impact={
                    cat: round(val, 3)
                    for cat, val in r.category_contributions.items()
                    if val > 0.01 and cat not in _JUNK_CATS
                },
                blurb=_rank_blurb(r),
                **_injury_fields(r.player_id),
            )
            for r in slot_waivers
        ]

        # Trade targets: per-team runner-up logic, filtered to genuine upgrades.
        # Only include actionable trade targets (possible + hard).
        # "unrealistic" players (top-25 overall, sole position player, best on team)
        # are never going to move — showing them makes the feature feel useless.
        pos_pool = sorted(
            [
                t for t in trade_pool.values()
                if _player_eligible_for_slot(t["positions"], slot_pos)
                and t["score"] > min_score
                and t.get("difficulty") != "unrealistic"
            ],
            key=lambda x: -x["score"],
        )
        pos_candidates: list[dict] = []
        seen_teams: set[int] = set()
        for t in pos_pool:
            tid = t["owner_team_id"]
            if tid not in seen_teams:
                pos_candidates.append(t)
                seen_teams.add(tid)
        pos_candidates.sort(key=lambda x: (_DIFF_ORDER.get(x["difficulty"], 9), -x["score"]))
        trade_targets = [TradeTargetRead(**t) for t in pos_candidates[:5]]

        return waiver_upgrades, trade_targets

    # ── Slot-based roster assignment ──────────────────────────────────────────
    # Assign players to individual roster slots (not just position groups).
    # Greedy: iterate slots in canonical order, assign best eligible unassigned player.
    _SPECIAL_SLOTS = frozenset({"BN", "NA", "IL", "IL+", "DL"})

    # Build assessment lookup from evaluate_team's position breakdown
    # (one assessment per position type, reused for each individual slot of that type)
    group_assessment: dict[str, str] = {
        g.position: g.assessment for g in evaluation.position_breakdown
    }
    group_score_map: dict[str, float] = {
        g.position: g.group_score for g in evaluation.position_breakdown
    }

    # Sort roster rankings by score descending for greedy assignment.
    # Exclude players parked in IL/NA slots — they can't fill active roster
    # spots and must not appear in both a starting slot AND the IL/NA section.
    _il_id_set = set(il_ids)
    assignable = sorted(
        [r for r in roster_rankings if r.player_id not in _il_id_set],
        key=lambda r: r.score, reverse=True,
    )
    assigned_ids: set[int] = set()

    _CANONICAL_ORDER = ["C", "1B", "2B", "3B", "SS", "OF", "Util", "DH", "SP", "RP", "P"]
    _POS_RANK = {p: i for i, p in enumerate(_CANONICAL_ORDER)}
    starting_positions = sorted(
        [p for p in roster_positions if p not in _SPECIAL_SLOTS],
        key=lambda p: (_POS_RANK.get(p, 99), p),
    )
    bench_positions    = [p for p in roster_positions if p == "BN"]

    _logger_ra.info(
        "roster_analysis team=%d: %d starting slots, %d bench, %d available_by_score, %d trade_pool",
        team_id, len(starting_positions), len(bench_positions),
        len(available_by_score), len(trade_pool),
    )

    slots: list[RosterSlotRead] = []
    slot_pos_counts: dict[str, int] = {}

    # Fill starting slots
    for slot_pos in starting_positions:
        idx = slot_pos_counts.get(slot_pos, 0)
        slot_pos_counts[slot_pos] = idx + 1

        # Best eligible unassigned player
        assigned_player = None
        for r in assignable:
            if r.player_id in assigned_ids:
                continue
            if _player_eligible_for_slot(r.positions, slot_pos):
                assigned_player = r
                assigned_ids.add(r.player_id)
                break

        # Assessment: use the assigned player's real position group, not the slot
        # label. A closer (RP) in a generic "P" slot would otherwise get "empty"
        # because group_assessment["P"] is never populated from "RP" group data.
        _PITCHER_SLOTS_SET = frozenset({"SP", "RP", "P"})
        if assigned_player:
            if assigned_player.stat_type == "pitching":
                real_group = next(
                    (p for p in assigned_player.positions if p in _PITCHER_SLOTS_SET),
                    "P",
                )
            else:
                real_group = assigned_player.positions[0] if assigned_player.positions else slot_pos
                if real_group in ("LF", "CF", "RF"):
                    real_group = "OF"
            assessment = (
                group_assessment.get(real_group)
                or group_assessment.get(slot_pos)
                or "average"
            )
            # Per-player override: a group can drag down its assessment if
            # paired with weaker teammates (e.g. Sanchez + Rasmussen → SP
            # group "average").  An individually top-60 player should always
            # show as at least solid regardless of group.
            if assigned_player.score >= _top25_threshold:
                assessment = "elite"
            elif assigned_player.score >= _top60_threshold and assessment not in ("elite",):
                assessment = "solid"
        else:
            assessment = "empty"
        _real_group_key = real_group if assigned_player else slot_pos
        g_score = group_score_map.get(_real_group_key) or group_score_map.get(slot_pos, 0.0)

        player_details = []
        if assigned_player:
            inj = _injury_fields(assigned_player.player_id)
            player_details = [RosterPlayerRead(
                player_name=assigned_player.name,
                positions=assigned_player.positions,
                score=round(assigned_player.score, 3),
                top_categories=_top_cats(assigned_player),
                **inj,
            )]

        occupant_score = assigned_player.score if assigned_player else 0.0
        waiver_upgrades, trade_targets = _build_upgrades(slot_pos, min_score=occupant_score)

        # Determine whether this slot belongs in the "Upgrades Available" fold.
        # Rules:
        #   solid/elite  → always "No Upgrades Needed" — there are always
        #                   theoretically better players but we never nag the
        #                   user to upgrade an already-great spot.  Clear any
        #                   candidates too (no point showing them).
        #   weak/empty   → always "Upgrades Available" even with no candidates
        #                   (surfaces the gap explicitly).
        #   average      → only "Upgrades Available" when actual candidates exist.
        if assessment in ("solid", "elite"):
            has_upgrades = False
            waiver_upgrades = []
            trade_targets = []
        elif assessment in ("weak", "empty"):
            has_upgrades = True
        else:  # "average"
            has_upgrades = bool(waiver_upgrades or trade_targets)

        slots.append(RosterSlotRead(
            position=slot_pos,
            slot_index=idx,
            assessment=assessment,
            players=[assigned_player.name] if assigned_player else [],
            player_details=player_details,
            group_score=g_score,
            priority=_ASSESSMENT_PRIORITY.get(assessment, 5) * 10 + len(slots),
            has_upgrades=has_upgrades,
            waiver_upgrades=waiver_upgrades,
            trade_targets=trade_targets,
        ))

    # Fill BN slots with remaining players (weakest first — most upgrade-worthy).
    # Exclude IL/NA players — they're handled separately in the stash section below
    # and must not also consume BN slots (which would displace actual bench players).
    bench_players = sorted(
        [r for r in roster_rankings if r.player_id not in assigned_ids and r.player_id not in _il_id_set],
        key=lambda r: r.score,  # ascending: weakest first
    )
    bn_idx = 0
    for slot_pos in bench_positions:
        bn_player = bench_players[bn_idx] if bn_idx < len(bench_players) else None
        bn_idx += 1

        assessment = group_assessment.get(
            (bn_player.positions[0] if bn_player and bn_player.positions else "BN"),
            "average"
        ) if bn_player else "empty"

        player_details = []
        if bn_player:
            inj = _injury_fields(bn_player.player_id)
            player_details = [RosterPlayerRead(
                player_name=bn_player.name,
                positions=bn_player.positions,
                score=round(bn_player.score, 3),
                top_categories=_top_cats(bn_player),
                **inj,
            )]

        occupant_score = bn_player.score if bn_player else 0.0
        waiver_upgrades, trade_targets = _build_upgrades(
            bn_player.positions[0] if bn_player and bn_player.positions else "BN",
            min_score=occupant_score,
        )
        # Same solid/elite guard as starting slots — don't nag the user to
        # upgrade a bench spot already occupied by a quality player.
        if assessment in ("solid", "elite"):
            has_upgrades = False
            waiver_upgrades = []
            trade_targets = []
        else:
            has_upgrades = bool(waiver_upgrades or trade_targets) or assessment == "empty"

        slots.append(RosterSlotRead(
            position="BN",
            slot_index=bn_idx - 1,
            assessment=assessment,
            players=[bn_player.name] if bn_player else [],
            player_details=player_details,
            group_score=0.0,
            priority=80 + bn_idx,
            has_upgrades=has_upgrades,
            waiver_upgrades=waiver_upgrades,
            trade_targets=trade_targets,
        ))

    # ── Pre-load stash candidates (used for both occupied and empty slots) ──────
    from datetime import date as _date
    from fantasai.models.player import InjuryRecord as _InjuryRec, Player as _StashPlayer
    from fantasai.models.prospect import ProspectProfile as _StashPP

    _stash_today = _date.today()

    # All high-PAV prospects not already rostered, sorted by weakness-fit then PAV
    _weak_cats    = set(evaluation.weak_categories) - _JUNK_CATS
    _batting_weak  = bool(_weak_cats & {"R", "HR", "RBI", "SB", "AVG", "OPS", "H", "BB"})
    _pitching_weak = bool(_weak_cats & {"W", "SV", "K", "ERA", "WHIP", "QS", "HLD"})

    _all_prospect_rows = (
        db.query(_StashPP, _StashPlayer)
        .join(_StashPlayer, _StashPlayer.player_id == _StashPP.player_id)
        .filter(_StashPP.pav_score >= 40.0)
        .order_by(_StashPP.pav_score.desc())
        .limit(50)
        .all()
    )

    def _na_priority(pp_player: tuple) -> tuple:
        pp, player = pp_player
        if player.player_id in all_rostered:
            return (99, 0)
        stat_type = pp.stat_type or "batting"
        addresses_weakness = (
            (stat_type == "batting" and _batting_weak) or
            (stat_type == "pitching" and _pitching_weak)
        )
        return (0 if addresses_weakness else 1, -(pp.pav_score or 0))

    _sorted_prospects = sorted(_all_prospect_rows, key=_na_priority)

    def _build_na_candidates(min_score: float = 0.0, limit: int = 3) -> list[WaiverUpgradeRead]:
        """Return up to `limit` unrostered prospects better than min_score."""
        result: list[WaiverUpgradeRead] = []
        for pp, player in _sorted_prospects:
            if player.player_id in all_rostered:
                continue
            pav_as_score = round((pp.pav_score or 0) / 10.0, 3)
            if pav_as_score <= min_score:
                continue
            result.append(WaiverUpgradeRead(
                player_id=player.player_id,
                player_name=player.name,
                positions=list(player.positions or [pp.stat_type[:2].upper()]),
                score=pav_as_score,
                category_impact={},
                blurb=f"PAV {pp.pav_score:.0f} prospect — {pp.stat_type or 'batting'}",
            ))
            if len(result) >= limit:
                break
        return result

    # All near-return injured players not already rostered
    _all_injury_rows = (
        db.query(_InjuryRec, _StashPlayer)
        .join(_StashPlayer, _StashPlayer.player_id == _InjuryRec.player_id)
        .filter(_InjuryRec.status != "out_for_season")
        .order_by(_InjuryRec.return_date.asc().nulls_last())
        .limit(100)
        .all()
    )

    def _build_il_candidates(min_score: float = 0.0, limit: int = 3) -> list[WaiverUpgradeRead]:
        """Return up to `limit` unrostered near-return injured players better than min_score."""
        result: list[WaiverUpgradeRead] = []
        for ir, player in _all_injury_rows:
            if player.player_id in all_rostered:
                continue
            if ir.return_date and (ir.return_date - _stash_today).days > 60:
                continue
            ranking = ranking_map.get(player.player_id)
            score = round(ranking.score, 3) if ranking else 0.0
            if score <= min_score:
                continue
            result.append(WaiverUpgradeRead(
                player_id=player.player_id,
                player_name=player.name,
                positions=list(player.positions or []),
                score=score,
                category_impact={},
            ))
            if len(result) >= limit:
                break
        return result

    # ── NA / IL stash suggestions for EMPTY slots ─────────────────────────────
    na_occupied = len(il_ids)
    na_count  = max(0, sum(1 for p in roster_positions if p == "NA") - na_occupied)
    il_count  = sum(1 for p in roster_positions if p in ("IL", "IL+", "DL"))

    if na_count > 0:
        na_upgrades = _build_na_candidates(min_score=0.0)
        if na_upgrades:
            slots.append(RosterSlotRead(
                position="NA",
                slot_index=0,
                assessment="empty",
                players=[],
                player_details=[],
                group_score=0.0,
                priority=90,
                has_upgrades=True,
                waiver_upgrades=na_upgrades,
                trade_targets=[],
            ))

    if il_count > 0:
        il_upgrades = _build_il_candidates(min_score=0.0)
        if il_upgrades:
            slots.append(RosterSlotRead(
                position="IL",
                slot_index=0,
                assessment="empty",
                players=[],
                player_details=[],
                group_score=0.0,
                priority=91,
                has_upgrades=True,
                waiver_upgrades=il_upgrades,
                trade_targets=[],
            ))

    # ── Occupied IL / NA slots ────────────────────────────────────────────────
    # Players the team has parked in IL/NA slots.  Show them as real slot rows
    # with their name and injury badge — the user can see who's sidelined and
    # when they're expected back.  These are informational only (no upgrades).
    # Determine which players are in NA slots vs IL slots.
    # NA = MiLB prospects; IL = injured MLB players.
    # We use ProspectProfile as the authoritative signal — if a player has a
    # prospect profile they belong in a NA slot, otherwise in an IL slot.
    from fantasai.models.prospect import ProspectProfile as _PP
    _prospect_ids: set[int] = {
        pid for (pid,) in db.query(_PP.player_id).all()
    }
    # Count how many NA vs IL slots the team actually has so we don't overflow.
    _na_slots_total  = sum(1 for p in roster_positions if p == "NA")
    _il_slots_total  = sum(1 for p in roster_positions if p in ("IL", "IL+", "DL"))
    _na_used = 0
    _il_used = 0

    # Sort by score desc so the best stash appears first in the section.
    il_ids_sorted = sorted(
        il_ids,
        key=lambda pid: ranking_map[pid].score if pid in ranking_map else 0.0,
        reverse=True,
    )

    for slot_idx, il_pid in enumerate(il_ids_sorted):
        r = ranking_map.get(il_pid)
        if r is None:
            continue
        inj = _injury_fields(il_pid)
        # Classify into NA (prospect) or IL (injured MLB player).
        if il_pid in _prospect_ids and _na_used < _na_slots_total:
            slot_pos = "NA"
            _na_used += 1
        elif _il_used < _il_slots_total:
            slot_pos = "IL"
            _il_used += 1
        else:
            slot_pos = "IL"   # fallback if counts are off
        player_details = [RosterPlayerRead(
            player_name=r.name,
            positions=r.positions,
            score=round(r.score, 3),
            top_categories=_top_cats(r),
            **inj,
        )]
        # Suggest better stash candidates even for occupied slots.
        if slot_pos == "NA":
            stash_upgrades = _build_na_candidates(min_score=r.score)
        else:
            stash_upgrades = _build_il_candidates(min_score=r.score)
        slots.append(RosterSlotRead(
            position=slot_pos,
            slot_index=slot_idx,
            assessment="average",
            players=[r.name],
            player_details=player_details,
            group_score=0.0,
            priority=92 + slot_idx,
            has_upgrades=bool(stash_upgrades),
            waiver_upgrades=stash_upgrades,
            trade_targets=[],
        ))

    # ── Unranked roster members (no stats / stub players) ────────────────────
    # Players whose Yahoo name couldn't be resolved to a FanGraphs ID are stored
    # as stub Player records with negative player_ids and no PlayerStats rows.
    # _apply_team_weights silently skips them, so they never appear in any slot
    # above.  Show them explicitly as BN slots so the user sees their full roster.
    _ranked_ids: set[int] = {r.player_id for r in roster_rankings}
    _unranked_active_ids = [
        pid for pid in player_ids
        if pid not in _ranked_ids and pid not in _il_id_set
    ]
    if _unranked_active_ids:
        from fantasai.models.player import Player as _UnrankedPlayer
        _unranked_map = {
            p.player_id: p
            for p in db.query(_UnrankedPlayer).filter(
                _UnrankedPlayer.player_id.in_(_unranked_active_ids)
            ).all()
        }
        for _uid in _unranked_active_ids:
            _up = _unranked_map.get(_uid)
            if _up is None:
                continue
            inj = _injury_fields(_uid)
            slots.append(RosterSlotRead(
                position="BN",
                slot_index=len(slots),
                assessment="average",
                players=[_up.name],
                player_details=[RosterPlayerRead(
                    player_name=_up.name,
                    positions=list(_up.positions or []),
                    score=0.0,
                    top_categories=[],
                    **inj,
                )],
                group_score=0.0,
                priority=85,
                has_upgrades=False,
                waiver_upgrades=[],
                trade_targets=[],
            ))

    # Slots are already in canonical position order from evaluate_team().
    # NA/IL stash slots appended at the end.
    return RosterAnalysisResponse(
        overall_grade=evaluation.letter_grade,
        overall_score=evaluation.overall_score,
        grade_percentile=evaluation.grade_percentile,
        weak_categories=[c for c in evaluation.weak_categories if c not in _JUNK_CATS],
        strong_categories=[c for c in evaluation.strong_categories if c not in _JUNK_CATS],
        category_strengths={k: v for k, v in evaluation.category_strengths.items() if k not in _JUNK_CATS},
        slots=slots,
    )
