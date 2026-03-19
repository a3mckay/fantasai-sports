from __future__ import annotations

import logging
import unicodedata
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.brain.injury_classifier import maybe_apply_classification
from fantasai.config import settings
from fantasai.engine.projection import ProjectionHorizon
from fantasai.schemas.ranking import PlayerRankingRead

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rankings", tags=["rankings"])

# Default 6x6 roto categories — used when no league context is available.
# Kept here (not just in recommendations.py) so the rankings page always uses
# a stable, predictable category set regardless of any per-league overrides.
RANKINGS_DEFAULT_CATEGORIES = [
    "R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP",
]

# Period label used when storing / looking up pre-generated blurbs.
CURRENT_PERIOD = "2025-season"


@router.get("", response_model=list[PlayerRankingRead])
def list_rankings(
    ranking_type: Optional[str] = Query(default="lookback", pattern="^(lookback|predictive)$"),
    season: int = Query(default=2025),
    horizon: str = Query(
        default="season",
        pattern="^(week|month|season)$",
        description="Projection horizon for predictive rankings: week, month, or season. Ignored for lookback.",
    ),
    position: Optional[str] = Query(default=None, description="Filter by position, e.g. 'OF'"),
    stat_type: Optional[str] = Query(default=None, pattern="^(batting|pitching)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list:
    """Compute and return player rankings from stored season stats.

    Rankings are computed on-demand from the most recent PlayerStats in the DB,
    using a 5-minute in-process cache shared with the analysis endpoints.
    Returns pre-generated blurbs from the Ranking table when available.

    The ``horizon`` parameter only affects predictive rankings:
    - ``week``  — projects ~26 PA / 6 IP; 35% talent signal
    - ``month`` — projects ~100 PA / 28 IP; 65% talent signal
    - ``season`` — projects full-season volume; 85% talent signal (default)
    """
    from fantasai.models.ranking import Ranking

    proj_horizon = ProjectionHorizon(horizon)

    # Re-use the shared cache for the current season (fast path).
    # For historical seasons the cache is keyed on 2025, so fall through to a
    # fresh uncached query — this path is only hit in tests / edge cases.
    _CACHED_SEASON = 2025
    if season == _CACHED_SEASON:
        from fantasai.api.v1.recommendations import _compute_rankings
        lookback, predictive = _compute_rankings(
            db, RANKINGS_DEFAULT_CATEGORIES, horizon=proj_horizon
        )
    else:
        from fantasai.adapters.base import NormalizedPlayerData
        from fantasai.adapters.mlb import MLBAdapter
        from fantasai.engine.scoring import ScoringEngine
        from fantasai.models.player import Player, PlayerStats

        stats_rows = db.query(PlayerStats).filter(
            PlayerStats.season == season,
            PlayerStats.stat_type.in_(["batting", "pitching"]),
        ).all()
        if not stats_rows:
            return []
        players = []
        for stats in stats_rows:
            player = db.get(Player, stats.player_id)
            if not player:
                continue
            players.append(NormalizedPlayerData(
                player_id=stats.player_id,
                name=player.name,
                team=player.team,
                positions=player.positions or [],
                stat_type=stats.stat_type,
                counting_stats=stats.counting_stats or {},
                rate_stats=stats.rate_stats or {},
                advanced_stats=stats.advanced_stats or {},
            ))
        if not players:
            return []
        adapter = MLBAdapter()
        eng = ScoringEngine(adapter, RANKINGS_DEFAULT_CATEGORIES)
        lookback_raw = eng.compute_lookback_rankings(season, players=players)
        predictive_raw = eng.compute_predictive_rankings(
            season, players=players, horizon=proj_horizon
        )

        def _dedup(rnks: list) -> list:
            seen: dict = {}
            for r in rnks:
                if r.player_id not in seen or r.score > seen[r.player_id].score:
                    seen[r.player_id] = r
            deduped = sorted(seen.values(), key=lambda r: r.score, reverse=True)
            for i, r in enumerate(deduped):
                r.overall_rank = i + 1
            return deduped

        lookback, predictive = _dedup(lookback_raw), _dedup(predictive_raw)

    rankings = predictive if ranking_type == "predictive" else lookback

    if not rankings:
        return []

    # Pull pre-generated blurbs from the Ranking table.
    # Keyed on (player_id, ranking_type, period); league_id=None = global blurbs.
    player_ids = [r.player_id for r in rankings]
    blurb_rows = (
        db.query(Ranking)
        .filter(
            Ranking.player_id.in_(player_ids),
            Ranking.ranking_type == ranking_type,
            Ranking.period == CURRENT_PERIOD,
            Ranking.league_id.is_(None),
        )
        .all()
    )
    blurb_map: dict[int, str] = {row.player_id: row.blurb for row in blurb_rows if row.blurb}

    # Apply filters
    if stat_type:
        rankings = [r for r in rankings if r.stat_type == stat_type]

    if position:
        pos = position.upper()
        rankings = [r for r in rankings if pos in r.positions]

    # Paginate
    rankings = rankings[offset: offset + limit]

    return [
        PlayerRankingRead(
            player_id=r.player_id,
            name=r.name,
            team=r.team,
            positions=r.positions,
            stat_type=r.stat_type,
            overall_rank=r.overall_rank,
            score=r.score,
            raw_score=r.raw_score,
            category_contributions=r.category_contributions,
            blurb=blurb_map.get(r.player_id),
            injury_status=r.injury_status,
            risk_flag=r.risk_flag,
            risk_note=r.risk_note,
        )
        for r in rankings
    ]


# ---------------------------------------------------------------------------
# Admin: cache management
# ---------------------------------------------------------------------------


@router.post("/clear-cache", tags=["admin"])
def clear_rankings_cache() -> dict:
    """Clear the in-process rankings cache, forcing a fresh compute on next request."""
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    n = len(_RANKINGS_CACHE)
    _RANKINGS_CACHE.clear()
    return {"cleared": n, "status": "ok"}


# ---------------------------------------------------------------------------
# Admin: injury management
# ---------------------------------------------------------------------------


@router.post("/sync-mlbam-ids", tags=["admin"])
def sync_mlbam_ids(db: Session = Depends(get_db)) -> dict:
    """Backfill Player.mlbam_id for all players using the Chadwick Bureau register.

    Required before sync-injuries will work: the injury sync cross-references
    MLB Stats API players by MLBAM ID. Run this once after ingesting stats,
    then re-run sync-injuries to pick up IL data.

    Safe to call repeatedly — only updates rows where mlbam_id is currently NULL.
    """
    from fantasai.engine.pipeline import backfill_mlbam_ids
    updated = backfill_mlbam_ids(db)
    return {"updated": updated, "status": "ok"}


@router.post("/sync-injuries", tags=["admin"])
def sync_injuries(db: Session = Depends(get_db)) -> dict:
    """Fetch current MLB IL data from the MLB Stats API and upsert into injury_records.

    Iterates all 30 MLB teams, fetches each team's injured-list roster, and
    cross-references players by mlbam_id.  Auto-runs the MLBAM ID backfill
    first if any players are missing their mlbam_id.
    """
    import httpx
    from datetime import datetime, timezone
    from fantasai.models.player import InjuryRecord, Player

    # Auto-backfill missing MLBAM IDs so the sync can match players
    from fantasai.engine.pipeline import backfill_mlbam_ids
    backfilled = backfill_mlbam_ids(db)
    if backfilled:
        logger.info("sync-injuries: backfilled %d MLBAM IDs before sync", backfilled)

    synced = 0
    not_found = 0
    errors: list[str] = []

    try:
        teams_resp = httpx.get(
            "https://statsapi.mlb.com/api/v1/teams",
            params={"sportId": 1, "season": 2026},
            timeout=15.0,
        )
        teams_resp.raise_for_status()
        teams = teams_resp.json().get("teams", [])
    except Exception as exc:
        return {"error": f"Failed to fetch teams: {exc}", "synced": 0}

    # Build mlbam_id → player_id lookup (batch)
    all_players = db.query(Player).filter(Player.mlbam_id.isnot(None)).all()
    mlbam_map: dict[int, int] = {p.mlbam_id: p.player_id for p in all_players}  # type: ignore[index]

    now_utc = datetime.now(timezone.utc)

    for team in teams:
        team_id = team.get("id")
        if not team_id:
            continue
        try:
            il_resp = httpx.get(
                f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                params={"rosterType": "injuredList", "season": 2026},
                timeout=10.0,
            )
            il_resp.raise_for_status()
            roster = il_resp.json().get("roster", [])
        except Exception as exc:
            errors.append(f"team {team_id}: {exc}")
            continue

        for entry in roster:
            mlbam_id = entry.get("person", {}).get("id")
            if not mlbam_id:
                continue

            player_id = mlbam_map.get(mlbam_id)
            if not player_id:
                not_found += 1
                continue

            status_desc = entry.get("status", {}).get("description", "")
            if "60" in status_desc:
                status = "il_60"
            elif "10" in status_desc or "15" in status_desc:
                status = "il_10"
            else:
                status = "day_to_day"

            injury_note = (
                entry.get("note")
                or entry.get("injuryDescription")
                or status_desc
                or None
            )

            existing = db.query(InjuryRecord).filter(
                InjuryRecord.player_id == player_id
            ).first()
            if existing:
                existing.status = status
                existing.injury_description = injury_note
                existing.fetched_at = now_utc
            else:
                db.add(InjuryRecord(
                    player_id=player_id,
                    status=status,
                    injury_description=injury_note,
                    fetched_at=now_utc,
                ))

            # Auto-classify severity and set risk_flag on the Player row.
            # "fragile" is never overwritten (manual-only flag).
            player_obj = db.get(Player, player_id)
            if player_obj:
                maybe_apply_classification(
                    player=player_obj,
                    description=injury_note or status_desc,
                    il_status=status,
                    api_key=settings.anthropic_api_key,
                )

            synced += 1

    db.commit()

    # Bust the rankings cache so changes take effect immediately
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    _RANKINGS_CACHE.clear()

    return {
        "synced": synced,
        "not_found_in_db": not_found,
        "team_errors": errors,
        "status": "ok",
    }


class InjuryOverrideBody(BaseModel):
    """Request body for manually setting a player's current injury status.

    Provide either ``player_id`` (FanGraphs IDfg) or ``player_name`` (partial
    match, case-insensitive).  ``player_id`` takes precedence when both are given.
    """
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    status: str  # "il_10" | "il_60" | "day_to_day" | "out_for_season" | "active"
    return_date: Optional[str] = None   # ISO date string: "2026-07-01"
    injury_description: Optional[str] = None


class RiskFlagBody(BaseModel):
    """Request body for setting a player's chronic risk flag.

    Provide either ``player_id`` (FanGraphs IDfg) or ``player_name`` (partial
    match, case-insensitive).  ``player_id`` takes precedence when both are given.
    """
    player_id: Optional[int] = None
    player_name: Optional[str] = None
    risk_flag: Optional[str] = None   # "fragile" | "recent_surgery" | null to clear
    risk_note: Optional[str] = None


def _fold_name(text: str) -> str:
    """Strip diacritics and lowercase for accent-insensitive name matching.

    Converts "José Ramírez" → "jose ramirez" so that typing without accents
    still finds the correct player.
    """
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()


def _resolve_player(db: Session, player_id: Optional[int], player_name: Optional[str]):
    """Return a Player ORM object from either player_id or a name search.

    Name search is accent- and case-insensitive substring match — returns the
    best single match or raises 404/422 if the name is ambiguous or not found.
    """
    from sqlalchemy import func as sqlfunc

    from fantasai.models.player import Player

    if player_id is not None:
        player = db.get(Player, player_id)
        if not player:
            raise HTTPException(status_code=404, detail=f"Player {player_id} not found")
        return player

    if player_name:
        norm = _fold_name(player_name.strip())

        # Try DB-level unaccent() (PostgreSQL); fall back to Python filtering.
        rows: list[Player] = []
        try:
            rows = (
                db.query(Player)
                .filter(sqlfunc.unaccent(sqlfunc.lower(Player.name)).contains(norm))
                .limit(10)
                .all()
            )
        except Exception:
            # unaccent() not available (SQLite/extension missing) — Python fallback.
            candidates = db.query(Player).limit(5000).all()
            rows = [r for r in candidates if norm in _fold_name(r.name)][:10]

        if not rows:
            raise HTTPException(status_code=404, detail=f"No player found matching '{player_name}'")
        if len(rows) == 1:
            return rows[0]
        # Prefer an exact accent-folded match when multiple partial hits exist.
        exact = [r for r in rows if _fold_name(r.name) == norm]
        if len(exact) == 1:
            return exact[0]
        names = ", ".join(r.name for r in rows[:5])
        raise HTTPException(
            status_code=422,
            detail=f"Ambiguous name '{player_name}' — matched: {names}. Use player_id instead.",
        )

    raise HTTPException(status_code=422, detail="Provide either player_id or player_name.")


@router.post("/set-injury", tags=["admin"])
def set_injury(body: InjuryOverrideBody, db: Session = Depends(get_db)) -> dict:
    """Manually set or clear a player's current injury status.

    Use this for spring-training injuries (not yet in the MLB Stats API IL),
    or to set precise return dates that the API doesn't provide.
    Setting status="active" removes the injury record entirely.

    Pass either ``player_id`` (FanGraphs IDfg) or ``player_name`` (e.g. "Tyler Glasnow").
    """
    from datetime import date as _date, datetime, timezone
    from fantasai.models.player import InjuryRecord

    player = _resolve_player(db, body.player_id, body.player_name)

    pid = player.player_id

    if body.status == "active":
        # Clear any existing injury record and reset auto-classified risk flag.
        db.query(InjuryRecord).filter(InjuryRecord.player_id == pid).delete()
        # Only clear auto-classified flags — preserve manual "fragile" flag.
        if player.risk_flag != "fragile":
            player.risk_flag = None
            player.risk_note = None
    else:
        return_date = None
        if body.return_date:
            try:
                return_date = _date.fromisoformat(body.return_date)
            except ValueError:
                raise HTTPException(status_code=422, detail="return_date must be ISO format: YYYY-MM-DD")

        existing = db.query(InjuryRecord).filter(InjuryRecord.player_id == pid).first()
        if existing:
            existing.status = body.status
            existing.return_date = return_date
            existing.injury_description = body.injury_description
            existing.fetched_at = datetime.now(timezone.utc)
        else:
            db.add(InjuryRecord(
                player_id=pid,
                status=body.status,
                return_date=return_date,
                injury_description=body.injury_description,
                fetched_at=datetime.now(timezone.utc),
            ))

        # Auto-classify severity when a description is provided.
        # Always re-runs so manual overrides with updated descriptions work correctly.
        # "fragile" is never overwritten (manual-only flag).
        if body.injury_description:
            maybe_apply_classification(
                player=player,
                description=body.injury_description,
                il_status=body.status,
                api_key=settings.anthropic_api_key,
            )

    db.commit()

    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    _RANKINGS_CACHE.clear()

    return {
        "player_id": pid,
        "name": player.name,
        "status": body.status,
        "risk_flag": player.risk_flag,
        "risk_note": player.risk_note,
        "ok": True,
    }


@router.post("/set-risk-flag", tags=["admin"])
def set_risk_flag(body: RiskFlagBody, db: Session = Depends(get_db)) -> dict:
    """Set or clear a player's chronic injury risk flag.

    risk_flag values:
      "fragile"        — chronically injury-prone: 0.70× PA/IP (Glasnow, Seager)
      "recent_surgery" — post-surgery risk: 0.80× PA/IP (Wheeler)
      null / ""        — clear the flag (player is healthy profile)

    Pass either ``player_id`` (FanGraphs IDfg) or ``player_name`` (e.g. "Tyler Glasnow").
    """
    player = _resolve_player(db, body.player_id, body.player_name)

    player.risk_flag = body.risk_flag or None
    player.risk_note = body.risk_note or None
    db.commit()

    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    _RANKINGS_CACHE.clear()

    return {
        "player_id": player.player_id,
        "name": player.name,
        "risk_flag": player.risk_flag,
        "risk_note": player.risk_note,
        "ok": True,
    }


# ---------------------------------------------------------------------------
# Admin: projection sync
# ---------------------------------------------------------------------------


@router.post(
    "/sync-projections",
    tags=["admin"],
    summary="Ingest Steamer 2026 projections from FanGraphs",
)
def sync_projections(
    season: int = Query(default=2026, ge=2025, le=2030),
    db: Session = Depends(get_db),
) -> dict:
    """Fetch Steamer projections for the given season and store them as
    PlayerStats rows (season=2026, stat_type=batting|pitching).

    Safe to re-run: upserts existing rows rather than duplicating them.
    Keeper evaluation will automatically prefer these projection rows over
    YTD actuals the next time the keeper-eval endpoint is called.

    Returns the number of rows upserted.
    """
    import logging as _log
    _logger = _log.getLogger(__name__)

    from fantasai.engine.pipeline import sync_steamer_projections

    # Invalidate the projection rankings cache so the next keeper-eval call
    # sees the fresh data immediately.
    from fantasai.api.v1.recommendations import _RANKINGS_CACHE
    stale_keys = [k for k in list(_RANKINGS_CACHE.keys()) if k.startswith("proj|")]
    for k in stale_keys:
        del _RANKINGS_CACHE[k]
    _logger.info("Invalidated %d stale projection cache entries", len(stale_keys))

    upserted = sync_steamer_projections(db, season=season)
    return {"season": season, "rows_upserted": upserted, "status": "ok"}

