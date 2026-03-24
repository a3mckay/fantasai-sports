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
import unicodedata
from typing import Optional

from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

_SIMILARITY_THRESHOLD = 0.82  # difflib ratio threshold


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

    # Load all players once
    all_players: list[Player] = db.query(Player.player_id, Player.name).all()

    # Build lookup structures
    exact: dict[str, int] = {}  # normalized_name → player_id
    normalized_list: list[tuple[str, int]] = []  # (normalized_name, player_id)

    for row in all_players:
        norm = _normalize(row.name)
        exact[norm] = row.player_id
        normalized_list.append((norm, row.player_id))

    all_norms = [n for n, _ in normalized_list]
    norm_to_id = {n: pid for n, pid in normalized_list}

    results: dict[str, Optional[int]] = {}

    for name in names:
        norm = _normalize(name)

        # 1. Exact match
        if norm in exact:
            results[name] = exact[norm]
            continue

        # 2. Token-set Jaccard similarity
        name_tokens = _token_set(name)
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
        close = difflib.get_close_matches(norm, all_norms, n=1, cutoff=_SIMILARITY_THRESHOLD)
        if close:
            results[name] = norm_to_id[close[0]]
            _log.debug("Fuzzy resolved '%s' → '%s' (%.2f)", name, close[0], _SIMILARITY_THRESHOLD)
            continue

        _log.warning("Could not resolve player name: '%s'", name)
        results[name] = None

    resolved = sum(1 for v in results.values() if v is not None)
    _log.info(
        "Name resolution: %d/%d resolved (%.0f%%)",
        resolved,
        len(names),
        100 * resolved / len(names) if names else 0,
    )
    return results
