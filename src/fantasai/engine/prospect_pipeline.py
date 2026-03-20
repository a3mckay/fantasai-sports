"""Prospect data pipeline.

Fetches MiLB stats from the MLB Stats API, computes PAV scores, upserts
ProspectProfile rows, and generates AI blurbs via the Anthropic API.

All I/O lives here; pav_scorer.py is kept pure (zero I/O).

MLB Stats API endpoints used (all free / no auth):
  /api/v1/sports/{sportId}/players?season={year}
      → roster of all players at each MiLB level

  /api/v1/people/{id}/stats?stats=season&group={hitting|pitching}&season={year}
      → per-team/level splits for the season

  /api/v1/people/{id}?hydrate=draft
      → draft year (first year of professional service)

Sport IDs:
  11 = Triple-A, 12 = Double-A, 13 = High-A, 14 = Low-A, 16 = Rookie / Complex
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from sqlalchemy.orm import Session

from fantasai.brain.pav_scorer import (
    SPORT_ID_TO_LEVEL,
    calculate_age_adj_performance,
    calculate_age_adj_performance_pitcher,
    calculate_eta_proximity,
    calculate_pav,
    calculate_prospect_grade,
    calculate_vertical_velocity,
    derive_eta_situation,
)
from fantasai.models.player import Player
from fantasai.models.prospect import ProspectProfile
from fantasai.models.ranking import Ranking

logger = logging.getLogger(__name__)

_MLB_BASE = "https://statsapi.mlb.com/api/v1"

# Sport IDs to query (excludes MLB = 1; we only want minor leaguers)
_MILB_SPORT_IDS: list[int] = [11, 12, 13, 14, 16]

# Age ceiling: only consider players born in or after this year
# (prospects, not career minor-leaguers)
_MAX_PROSPECT_AGE = 26

# Minimum sample thresholds to trust a stint
_MIN_GAMES_HIT = 5     # at least 5 games for a hitting stint
_MIN_IP_PITCH = 10.0   # at least 10 IP for a pitching stint


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: Optional[dict] = None, retries: int = 3) -> Optional[dict]:
    """GET with simple retry + backoff.  Returns parsed JSON or None on failure."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                logger.warning("MLB Stats API fetch failed: %s — %s", url, exc)
    return None


# ---------------------------------------------------------------------------
# Name normalization for cross-reference fallback
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Lowercase, strip accents (best-effort), remove non-alpha chars."""
    s = name.lower().strip()
    # Simple accent removal for common characters
    _accent_map = str.maketrans(
        "áàäâãåæçéèëêíìïîñóòöôõøúùüûý",
        "aaaaaaaceeeeiiiinoooooouuuuy",
    )
    s = s.translate(_accent_map)
    s = re.sub(r"[^a-z ]", "", s)
    # Collapse whitespace
    return " ".join(s.split())


# ---------------------------------------------------------------------------
# Fetch: prospect IDs from MiLB rosters
# ---------------------------------------------------------------------------

def fetch_prospect_ids(season: int = 2025) -> list[dict]:
    """Return a list of {mlbam_id, birth_year} for all MiLB players age ≤ 26.

    Queries each sport level and deduplicates by mlbam_id.  Only players
    young enough to be plausible prospects are included.
    """
    seen: set[int] = set()
    prospects: list[dict] = []

    current_year = datetime.now().year
    min_birth_year = current_year - _MAX_PROSPECT_AGE

    for sport_id in _MILB_SPORT_IDS:
        url = f"{_MLB_BASE}/sports/{sport_id}/players"
        data = _get(url, {"season": season, "fields": "people,id,fullName,birthDate,currentTeam"})
        if not data:
            continue
        for p in data.get("people", []):
            mid = p.get("id")
            if not mid or mid in seen:
                continue
            birth_date = p.get("birthDate", "")
            try:
                birth_year = int(birth_date[:4]) if birth_date else None
            except ValueError:
                birth_year = None
            if birth_year and birth_year < min_birth_year:
                continue  # too old
            seen.add(mid)
            prospects.append({
                "mlbam_id": mid,
                "birth_year": birth_year,
                "full_name": p.get("fullName", ""),
            })
        time.sleep(0.1)  # be gentle

    logger.info("Fetched %d MiLB prospect candidates for %d", len(prospects), season)
    return prospects


# ---------------------------------------------------------------------------
# Fetch: MiLB stints for a single player
# ---------------------------------------------------------------------------

def _parse_age(birth_year: Optional[int], season: int) -> float:
    """Approximate player age at the midpoint of the season."""
    if birth_year:
        return float(season - birth_year)
    return 22.0  # fallback


def fetch_hitting_stints(
    mlbam_id: int,
    season: int = 2025,
    birth_year: Optional[int] = None,
) -> list[dict]:
    """Return per-level hitting stints for a player.

    Each entry: {level, games, ops, player_age}
    Skips stints with fewer than _MIN_GAMES_HIT games.
    """
    # Must query each sport level separately — the MLB Stats API returns empty
    # results for MiLB players when sportId is not specified.
    url = f"{_MLB_BASE}/people/{mlbam_id}/stats"
    stints: list[dict] = []

    for sport_id in _MILB_SPORT_IDS:
        level = SPORT_ID_TO_LEVEL.get(sport_id, "")
        if not level or level == "MLB":
            continue

        data = _get(url, {"stats": "season", "group": "hitting", "season": season, "sportId": sport_id})
        if not data:
            continue

        for stat_group in data.get("stats", []):
            for split in stat_group.get("splits", []):
                s = split.get("stat", {})
                games = int(s.get("gamesPlayed", 0))
                if games < _MIN_GAMES_HIT:
                    continue

                try:
                    obp = float(s.get("obp", 0) or 0)
                    slg = float(s.get("slg", 0) or 0)
                    ops = obp + slg
                    if ops <= 0:
                        ops_str = s.get("ops", "")
                        ops = float(ops_str) if ops_str else 0.700
                except (TypeError, ValueError):
                    ops = 0.700

                stints.append({
                    "level": level,
                    "games": games,
                    "ops": round(ops, 3),
                    "player_age": _parse_age(birth_year, season),
                })

        time.sleep(0.05)

    return stints


def fetch_pitching_stints(
    mlbam_id: int,
    season: int = 2025,
    birth_year: Optional[int] = None,
) -> list[dict]:
    """Return per-level pitching stints for a player.

    Each entry: {level, ip, era, k9, whip, player_age}
    Skips stints with fewer than _MIN_IP_PITCH innings.
    """
    # Must query each sport level separately — same reason as fetch_hitting_stints.
    url = f"{_MLB_BASE}/people/{mlbam_id}/stats"
    stints: list[dict] = []

    for sport_id in _MILB_SPORT_IDS:
        level = SPORT_ID_TO_LEVEL.get(sport_id, "")
        if not level or level == "MLB":
            continue

        data = _get(url, {"stats": "season", "group": "pitching", "season": season, "sportId": sport_id})
        if not data:
            continue

        for stat_group in data.get("stats", []):
            for split in stat_group.get("splits", []):
                s = split.get("stat", {})
                try:
                    ip_str = str(s.get("inningsPitched", "0"))
                    parts = ip_str.split(".")
                    full_inn = int(parts[0]) if parts[0] else 0
                    outs = int(parts[1]) if len(parts) > 1 else 0
                    ip = full_inn + outs / 3.0
                except (TypeError, ValueError):
                    ip = 0.0

                if ip < _MIN_IP_PITCH:
                    continue

                try:
                    era = float(s.get("era", 4.50) or 4.50)
                    whip = float(s.get("whip", 1.30) or 1.30)
                    k9 = float(s.get("strikeoutsPer9Inn", 7.0) or 7.0)
                except (TypeError, ValueError):
                    era, whip, k9 = 4.50, 1.30, 7.0

                stints.append({
                    "level": level,
                    "ip": round(ip, 1),
                    "era": round(era, 2),
                    "k9": round(k9, 2),
                    "whip": round(whip, 2),
                    "player_age": _parse_age(birth_year, season),
                })

        time.sleep(0.05)

    return stints


# ---------------------------------------------------------------------------
# Fetch: draft year
# ---------------------------------------------------------------------------

def fetch_draft_year(mlbam_id: int) -> Optional[int]:
    """Fetch the year a player was drafted or signed as an IFA.

    Falls back to None if the endpoint doesn't return draft info.
    """
    url = f"{_MLB_BASE}/people/{mlbam_id}"
    data = _get(url, {"hydrate": "draft"})
    if not data:
        return None
    for person in data.get("people", []):
        drafts = person.get("draftYear")
        if drafts:
            try:
                return int(drafts)
            except ValueError:
                pass
        # Some players have draft info in a nested structure
        for d in person.get("drafts", []):
            try:
                return int(d.get("year", 0))
            except (ValueError, TypeError):
                pass
    return None


# ---------------------------------------------------------------------------
# Highest-level and levels-in-season helpers
# ---------------------------------------------------------------------------

def _highest_level(stints: list[dict]) -> str:
    """Return the highest MiLB level reached across all stints."""
    from fantasai.brain.pav_scorer import LEVEL_ORDER
    if not stints:
        return "Low-A"
    best_idx = 0
    for s in stints:
        lvl = s.get("level", "Low-A")
        try:
            idx = LEVEL_ORDER.index(lvl)
            if idx > best_idx:
                best_idx = idx
        except ValueError:
            pass
    return LEVEL_ORDER[best_idx]


def _count_levels(stints: list[dict]) -> int:
    """Count distinct levels played across all stints."""
    return len({s.get("level") for s in stints if s.get("level")})


# ---------------------------------------------------------------------------
# Per-player PAV computation
# ---------------------------------------------------------------------------

def _compute_pav_for_player(
    player: Player,
    pp: ProspectProfile,
    season: int,
    bio: dict,
) -> Optional[float]:
    """Fetch stints, derive ETA, compute PAV, populate ProspectProfile fields.

    Returns the final pav_score or None if there's not enough data.
    """
    mlbam_id = pp.mlbam_id or player.mlbam_id
    if not mlbam_id:
        return None

    birth_year = bio.get("birth_year") or player.birth_year
    pitcher = pp.stat_type == "pitching"

    if pitcher:
        stints = fetch_pitching_stints(mlbam_id, season, birth_year)
    else:
        stints = fetch_hitting_stints(mlbam_id, season, birth_year)

    if not stints:
        return None

    highest = _highest_level(stints)
    levels = _count_levels(stints)
    draft_year = pp.draft_year or fetch_draft_year(mlbam_id)
    years_pro = max(1, season - draft_year) if draft_year else 1

    eta = pp.eta_situation or derive_eta_situation(highest, years_pro, levels)

    # Compute grade from pipeline_rank; fallback to 55 if no rank yet
    grade = calculate_prospect_grade(
        pipeline_grade=pp.pipeline_grade,
        ba_grade=pp.ba_grade,
        fg_grade=pp.fg_grade,
        consensus_rank=pp.pipeline_rank,
    )

    if pitcher:
        perf = calculate_age_adj_performance_pitcher(stints)
        position = "SP"  # default; closer bonus applied if risk_note indicates
    else:
        perf = calculate_age_adj_performance(stints)
        positions = player.positions or []
        position = positions[0] if positions else "OF"

    velocity = calculate_vertical_velocity(levels, highest, years_pro)
    eta_score = calculate_eta_proximity(eta)

    result = calculate_pav(
        prospect_grade=grade,
        age_adj_perf=perf,
        vertical_velocity=velocity,
        eta_proximity=eta_score,
        position=position,
        pitcher=pitcher,
    )

    # Update ProspectProfile fields
    pp.stints = stints
    pp.levels_in_season = levels
    pp.highest_level = highest
    pp.draft_year = draft_year
    pp.eta_situation = eta
    pp.pav_score = result["pav_final"]
    pp.proxy_mlb_rank = result["proxy_mlb_rank"]
    pp.last_synced = datetime.now(timezone.utc)

    return result["pav_final"]


# ---------------------------------------------------------------------------
# Prospect grade ranking (pipeline_rank)
# ---------------------------------------------------------------------------

def _assign_pipeline_ranks(db: Session) -> None:
    """Rank all prospects by pav_score descending and store as pipeline_rank.

    This gives each prospect an implied rank (#1 = best by our model) which
    then feeds back into calculate_prospect_grade() to produce a grade.
    Because grade is set AFTER ranking, there's no circular dependency:
    the first sync uses grade=55 (default); subsequent syncs use the rank from
    the previous pass.  Two passes of sync_prospect_data() fully converge.
    """
    profiles = (
        db.query(ProspectProfile)
        .filter(ProspectProfile.pav_score.isnot(None))
        .order_by(ProspectProfile.pav_score.desc())
        .all()
    )
    for rank, pp in enumerate(profiles, start=1):
        pp.pipeline_rank = rank
    db.flush()


# ---------------------------------------------------------------------------
# Blurb generation
# ---------------------------------------------------------------------------

def generate_prospect_blurbs(
    db: Session,
    profiles: list[ProspectProfile],
    api_key: Optional[str],
    season: int = 2025,
) -> int:
    """Generate keeper-context blurbs for prospects with new/changed PAV scores.

    Blurbs are stored in the Ranking table with ranking_type="pav" so the
    existing blurb-merge logic in rankings.py picks them up automatically.

    Returns count of blurbs generated.
    """
    if not api_key:
        logger.info("No ANTHROPIC_API_KEY — skipping prospect blurb generation")
        return 0

    try:
        import anthropic
        from fantasai.brain.writer_persona import SYSTEM_PROMPT as _PERSONA
    except ImportError:
        logger.warning("anthropic package not available — skipping blurb generation")
        return 0

    client = anthropic.Anthropic(api_key=api_key)
    generated = 0

    for pp in profiles:
        player = pp.player
        if not player:
            continue

        # Check if blurb already exists and score hasn't changed significantly
        existing = (
            db.query(Ranking)
            .filter_by(player_id=player.player_id, ranking_type="pav", league_id=None)
            .first()
        )
        if existing and existing.blurb:
            # Regenerate only if score changed by >1 point
            # We track previous score as a rough heuristic using rank change
            continue  # skip if blurb exists (first-sync only: always generate)

        # Build context for the blurb
        stints = pp.stints or []
        stat_lines = []
        for s in stints:
            if pp.stat_type == "pitching":
                stat_lines.append(
                    f"{s.get('level','?')} ({s.get('ip',0):.0f} IP): "
                    f"ERA {s.get('era','?')}, {s.get('k9','?')} K/9, WHIP {s.get('whip','?')}"
                )
            else:
                stat_lines.append(
                    f"{s.get('level','?')} ({s.get('games',0)}G): {s.get('ops',0):.3f} OPS"
                )

        stat_summary = " | ".join(stat_lines) if stat_lines else "limited data"
        age = (season - (player.birth_year or (season - 22)))
        positions = ", ".join(player.positions or ["?"])
        years_pro = max(1, season - (pp.draft_year or (season - 1)))
        pitcher_word = "pitching" if pp.stat_type == "pitching" else "hitting"

        prompt = (
            f"Write a 2–3 sentence keeper-league fantasy baseball scouting note for "
            f"{player.name} ({player.team} · MiLB, {positions}, age {age}).\n\n"
            f"2025 {pitcher_word} stats: {stat_summary}\n"
            f"Prospect info: #{pp.pipeline_rank or '?'} in our system, "
            f"PAV score {pp.pav_score:.1f}/100, proxy rank #{pp.proxy_mlb_rank}, "
            f"ETA situation: {pp.eta_situation}, year {years_pro} pro.\n\n"
            f"Lead with their fantasy floor/ceiling, mention their age-appropriate "
            f"performance and MLB timeline, and close with a keeper verdict for keeper leagues. "
            f"Be direct and confident. No hedging."
        )

        try:
            msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=200,
                system=_PERSONA,
                messages=[{"role": "user", "content": prompt}],
            )
            blurb_text = msg.content[0].text.strip()
        except Exception as exc:
            logger.warning("Blurb generation failed for %s: %s", player.name, exc)
            continue

        if existing:
            existing.blurb = blurb_text
        else:
            db.add(Ranking(
                player_id=player.player_id,
                ranking_type="pav",
                period=f"{season}-season",
                league_id=None,
                blurb=blurb_text,
            ))

        generated += 1
        time.sleep(0.3)  # rate-limit courtesy pause

    return generated


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def sync_prospect_data(
    db: Session,
    season: int = 2025,
    api_key: Optional[str] = None,
) -> dict:
    """Full prospect sync: fetch MiLB stats → compute PAV → upsert DB.

    Two-phase approach:
    1. Compute PAV for all prospects (using default grade=55 for new players).
    2. Assign pipeline_rank by sorting on pav_score.
    3. Re-compute PAV using the new grade derived from rank.
    4. Generate blurbs for new/updated prospects.

    Returns a summary dict: {synced, skipped, errors, blurbs_generated}.
    """
    logger.info("Starting prospect sync for season %d", season)

    # Build primary lookup: mlbam_id → Player
    all_players = db.query(Player).all()
    mlbam_to_player: dict[int, Player] = {
        p.mlbam_id: p for p in all_players if p.mlbam_id is not None
    }
    # Fallback lookup: normalized name → Player (catches MiLB-only players whose
    # mlbam_id isn't in the Chadwick register and therefore not set in our DB)
    name_to_player: dict[str, Player] = {
        _normalize_name(p.name): p for p in all_players if p.name
    }

    # Build a lookup: player_id → ProspectProfile (existing)
    existing_profiles: dict[int, ProspectProfile] = {
        pp.player_id: pp
        for pp in db.query(ProspectProfile).all()
    }

    # Also load profiles keyed by mlbam_id for easy lookup
    mlbam_to_profile: dict[int, ProspectProfile] = {
        pp.mlbam_id: pp
        for pp in existing_profiles.values()
        if pp.mlbam_id
    }

    # Fetch all MiLB prospect candidates
    candidates = fetch_prospect_ids(season)

    synced = 0
    skipped = 0
    errors = 0
    updated_profiles: list[ProspectProfile] = []
    # Track player_ids already processed this run to prevent duplicates when two
    # different mlbam_ids normalize to the same player name (e.g. same-name players
    # appearing at different levels in the MLB Stats API response).
    processed_player_ids: set[int] = set()

    for bio in candidates:
        mid = bio["mlbam_id"]
        player = mlbam_to_player.get(mid)
        if not player:
            # Fallback: try to match by name (handles MiLB-only players where
            # Chadwick register doesn't have an mlbam_id mapping)
            norm = _normalize_name(bio.get("full_name", ""))
            player = name_to_player.get(norm) if norm else None
            if player:
                # Cache the mlbam_id on the player row so future syncs use the
                # faster ID-based path
                player.mlbam_id = mid
                mlbam_to_player[mid] = player
                logger.info(
                    "Matched prospect %r by name → player_id=%d; stored mlbam_id=%d",
                    bio.get("full_name"),
                    player.player_id,
                    mid,
                )
        if not player:
            skipped += 1
            continue  # player not in our DB

        # Skip if we already processed this player under a different mlbam_id
        # (avoids UniqueViolation when two API entries normalize to the same name)
        if player.player_id in processed_player_ids:
            skipped += 1
            continue
        processed_player_ids.add(player.player_id)

        # Get or create ProspectProfile
        pp = existing_profiles.get(player.player_id) or mlbam_to_profile.get(mid)
        if not pp:
            pp = ProspectProfile(
                player_id=player.player_id,
                mlbam_id=mid,
                stat_type="pitching" if "P" in (player.positions or []) and
                           "1B" not in (player.positions or []) else "batting",
            )
            db.add(pp)
            db.flush()
            # Keep the in-memory dicts in sync so a second bio for the same player
            # (e.g. same name at a different level) hits the existing-profile branch
            existing_profiles[player.player_id] = pp
            mlbam_to_profile[mid] = pp
        else:
            pp.mlbam_id = mid

        try:
            pav = _compute_pav_for_player(player, pp, season, bio)
            if pav is None:
                skipped += 1
                continue
            updated_profiles.append(pp)
            synced += 1
        except Exception as exc:
            logger.error("PAV computation failed for player_id=%d: %s", player.player_id, exc)
            errors += 1

        time.sleep(0.05)  # be gentle with MLB Stats API

    db.flush()

    # Phase 2: assign implied ranks and re-compute grades
    _assign_pipeline_ranks(db)

    # Phase 3: re-run PAV with proper grades now that ranks are assigned
    # Rebuild player lookup now that name-matched players may have had mlbam_id set
    player_id_to_player: dict[int, Player] = {p.player_id: p for p in all_players}
    for pp in updated_profiles:
        player = player_id_to_player.get(pp.player_id)
        if not player:
            continue
        try:
            bio = next((b for b in candidates if b["mlbam_id"] == pp.mlbam_id), {})
            _compute_pav_for_player(player, pp, season, bio)
        except Exception as exc:
            logger.warning("Re-rank PAV pass failed for player_id=%d: %s", player.player_id, exc)

    db.flush()

    # Phase 4: generate blurbs for new prospects (or all if none exist)
    blurbs_generated = 0
    if api_key and updated_profiles:
        blurbs_generated = generate_prospect_blurbs(db, updated_profiles, api_key, season)

    db.commit()

    logger.info(
        "Prospect sync complete: synced=%d skipped=%d errors=%d blurbs=%d",
        synced, skipped, errors, blurbs_generated,
    )
    return {
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "blurbs_generated": blurbs_generated,
        "season": season,
    }
