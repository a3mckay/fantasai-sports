from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db
from fantasai.schemas.ranking import PlayerRankingRead

router = APIRouter(prefix="/rankings", tags=["rankings"])


@router.get("", response_model=list[PlayerRankingRead])
def list_rankings(
    ranking_type: Optional[str] = Query(default="lookback", pattern="^(lookback|predictive)$"),
    season: int = Query(default=2025),
    position: Optional[str] = Query(default=None, description="Filter by position, e.g. 'OF'"),
    stat_type: Optional[str] = Query(default=None, pattern="^(batting|pitching)$"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list:
    """Compute and return player rankings from stored season stats.

    Rankings are computed on-demand from the most recent PlayerStats in the DB.
    Use refresh_data.py (or the scheduled pipeline) to keep stats current.
    Returns an empty list when no stats have been synced yet.
    """
    from fantasai.adapters.base import NormalizedPlayerData
    from fantasai.adapters.mlb import MLBAdapter
    from fantasai.engine.scoring import ScoringEngine
    from fantasai.models.player import Player, PlayerStats

    # Default 6x6 roto categories — used when no league context is available
    DEFAULT_CATEGORIES = ["R", "HR", "RBI", "SB", "AVG", "OPS", "IP", "W", "SV", "K", "ERA", "WHIP"]

    stats_rows = db.query(PlayerStats).filter(PlayerStats.season == season).all()
    if not stats_rows:
        return []

    players = []
    for stats in stats_rows:
        player = db.get(Player, stats.player_id)
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

    adapter = MLBAdapter()
    engine = ScoringEngine(adapter, DEFAULT_CATEGORIES)

    if ranking_type == "predictive":
        rankings = engine.compute_predictive_rankings(season, players=players)
    else:
        rankings = engine.compute_lookback_rankings(season, players=players)

    # Deduplicate: two-way players (e.g. Ohtani) have both batting and pitching
    # rows in the DB and thus appear twice in rankings. Keep only the entry with
    # the higher score per player_id, then re-sort so ranks stay contiguous.
    seen: dict[int, object] = {}
    for r in rankings:
        if r.player_id not in seen or r.score > seen[r.player_id].score:
            seen[r.player_id] = r
    rankings = sorted(seen.values(), key=lambda r: r.score, reverse=True)
    for i, r in enumerate(rankings):
        r.overall_rank = i + 1

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
        )
        for r in rankings
    ]
