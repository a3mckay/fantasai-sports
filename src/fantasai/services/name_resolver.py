"""Resolve Yahoo player name strings to FanGraphs player_id integers.

Yahoo Fantasy rosters contain display names like "Shohei Ohtani" or "Rafael Devers".
This module matches those against the ``players`` table using:
  1. Exact match after unicode normalization (strip accents, lowercase)
  2. Token-set Jaccard similarity fallback (catches "De La Cruz" vs "de la Cruz")
  3. difflib close-match fallback (catches single-character typos / Jr. differences)

Any name that cannot be resolved with sufficient confidence is left as-is
(returned value will be None for that name).
"""
from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from typing import Optional

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.82  # difflib ratio threshold


_PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")


def _strip_qualifier(name: str) -> str:
    """Remove trailing parenthetical qualifiers Yahoo appends to player names.

    Examples:
      "Shohei Ohtani (Batter)"  → "Shohei Ohtani"
      "Shohei Ohtani (Pitcher)" → "Shohei Ohtani"
      "José Ramírez (3B)"       → "José Ramírez"
    """
    return _PAREN_SUFFIX.sub("", name).strip()


def _normalize(name: str) -> str:
    """Strip accents, lowercase, remove punctuation, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_str = nfkd.encode("ascii", "ignore").decode("ascii")
    # Remove periods and apostrophes (Jr., O'Brien → OBrien)
    cleaned = ascii_str.replace(".", "").replace("'", "").replace("-", " ")
    return " ".join(cleaned.lower().split())


def _token_set(name: str) -> set[str]:
    return set(_normalize(name).split())


def resolve_player_names(
    names: list[str],
    db: Session,
) -> dict[str, Optional[int]]:
    """Map each Yahoo display name to a FanGraphs player_id.

    Args:
        names: list of player name strings from Yahoo roster
        db: SQLAlchemy session

    Returns:
        dict mapping each input name → player_id (int) or None if unresolved
    """
    from fantasai.models.player import Player  # local import to avoid circular deps

    from fantasai.models.player import PlayerStats

    # Load all players once
    all_players: list[Player] = db.query(Player.player_id, Player.name).all()

    # Player IDs that have any stats — used to break ties when the same player
    # name maps to multiple player_ids (e.g. two-way players whose batting and
    # pitching pipeline rows received different IDfg values).  We prefer the ID
    # that actually has stat rows over a stale/orphan player record.
    stat_player_ids: set[int] = {
        r.player_id
        for r in db.query(PlayerStats.player_id).distinct().all()
    }

    # Group candidates by normalized name, then pick the best:
    # prefer a candidate that has stats over one that doesn't.
    name_to_candidates: dict[str, list[int]] = {}
    for row in all_players:
        norm = _normalize(row.name)
        name_to_candidates.setdefault(norm, []).append(row.player_id)

    # Build lookup structures
    exact: dict[str, int] = {}  # normalized_name → player_id
    normalized_list: list[tuple[str, int]] = []  # (normalized_name, player_id)

    for norm, candidates in name_to_candidates.items():
        with_stats = [pid for pid in candidates if pid in stat_player_ids]
        chosen = (with_stats or candidates)[0]
        exact[norm] = chosen
        normalized_list.append((norm, chosen))

    all_norms = [n for n, _ in normalized_list]
    norm_to_id = {n: pid for n, pid in normalized_list}

    results: dict[str, Optional[int]] = {}

    for name in names:
        # Strip Yahoo positional qualifiers before matching:
        # "Shohei Ohtani (Batter)" → "Shohei Ohtani"
        lookup_name = _strip_qualifier(name)
        norm = _normalize(lookup_name)

        # 1. Exact match
        if norm in exact:
            results[name] = exact[norm]
            continue

        # 2. Token-set Jaccard similarity
        name_tokens = _token_set(lookup_name)
        best_jaccard: float = 0.0
        best_jaccard_id: Optional[int] = None

        for candidate_norm, candidate_id in normalized_list:
            candidate_tokens = set(candidate_norm.split())
            if not name_tokens or not candidate_tokens:
                continue
            intersection = len(name_tokens & candidate_tokens)
            union = len(name_tokens | candidate_tokens)
            jaccard = intersection / union if union else 0.0
            if jaccard > best_jaccard:
                best_jaccard = jaccard
                best_jaccard_id = candidate_id

        if best_jaccard >= 0.85 and best_jaccard_id is not None:
            results[name] = best_jaccard_id
            continue

        # 3. difflib ratio
        close = difflib.get_close_matches(norm, all_norms, n=1, cutoff=_SIMILARITY_THRESHOLD)  # norm already uses lookup_name
        if close:
            results[name] = norm_to_id[close[0]]
            _log.debug("Fuzzy resolved '%s' → '%s' (%.2f)", name, close[0], _SIMILARITY_THRESHOLD)
            continue

        _log.warning("Could not resolve player name: '%s' (stripped: '%s')", name, lookup_name)
        results[name] = None

    resolved = sum(1 for v in results.values() if v is not None)
    _log.info(
        "Name resolution: %d/%d resolved (%.0f%%)",
        resolved,
        len(names),
        100 * resolved / len(names) if names else 0,
    )
    return results
