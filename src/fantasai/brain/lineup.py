"""Optimal lineup assignment and roster-aware player scoring weights.

Given a team's full roster, the league's roster slot template, and which
players are on the IL, computes a *weight* per player reflecting their
expected scoring contribution.

Weights
-------
1.0   — assigned to an active slot (starts every game)
0.0   — on the IL/IL+/NA slot (cannot contribute)

Batters (bench overflow)
  0.30 — 1st overflow: fills in on rest days (~21% rest rate × ~4 eligible slots)
  0.12 — 2nd overflow: rarely gets a spot
  0.05 — 3rd+ overflow: essentially a stash

SPs (bench overflow)
  SPs only pitch 1-2x per week regardless. In daily-change leagues a manager
  simply slots them in on their start day — the bench label costs far less than
  it does for a batter. The 1st bench SP captures ~70% of their normal starts.
  0.90 — 1st overflow SP (manager nearly always slots them in on start day)
  0.70 — 2nd overflow SP (still gets most starts; skipped only on scheduling conflicts)
  0.40 — 3rd+ overflow SP

RPs (bench overflow)
  Similar reasoning to SP — RPs appear 3-5x/week and managers slot them in
  daily when they have saves/holds opportunities.
  0.50 — 1st overflow RP
  0.22 — 2nd overflow RP
  0.08 — 3rd+ overflow RP

Non-IL injured players (DTD, Q, O) receive an additional multiplier on top
of their slot weight.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from fantasai.engine.scoring import PlayerRanking

# Slot labels that are NOT active scoring positions
IL_SLOTS    = {"IL", "IL+", "IL10", "IL60", "IR", "NA"}
BENCH_SLOTS = {"BN"}
NON_ACTIVE  = IL_SLOTS | BENCH_SLOTS

# Bench utilisation weights by player type and overflow depth (0-indexed)
_BATTER_BENCH = [0.30, 0.12, 0.05]
_SP_BENCH     = [0.90, 0.70, 0.40]
_RP_BENCH     = [0.50, 0.22, 0.08]

# Yahoo injury-status → active-play multiplier
INJURY_MULTIPLIERS: dict[str, float] = {
    "Q":   0.80,   # Questionable
    "DTD": 0.60,   # Day-to-day
    "O":   0.15,   # Out (non-IL)
    "NA":  0.05,   # Not available / season-ending
}

# Positions eligible for the generic Util slot
_UTIL_ELIGIBLE = {"C", "1B", "2B", "3B", "SS", "OF", "LF", "CF", "RF", "DH", "Util"}

# Positions eligible for the generic P slot
_P_ELIGIBLE = {"SP", "RP", "P"}

# Fill order: rarest / highest-value position first so we don't burn a
# rare C or SS on the Util slot when a DH-only player needs it.
_SLOT_PRIORITY = ["C", "SS", "2B", "3B", "1B", "OF", "Util", "DH", "SP", "RP", "P"]


def parse_active_slots(roster_positions: list[str]) -> dict[str, int]:
    """Return a count of each *active* slot type in the league template.

    BN / IL / IL+ / NA are excluded — they are not scoring positions.

    Example::

        ["C","1B","2B","3B","SS","OF","OF","OF","Util","SP","SP","RP","RP","BN","BN","IL","IL+"]
        → {"C":1,"1B":1,"2B":1,"3B":1,"SS":1,"OF":3,"Util":1,"SP":2,"RP":2}
    """
    counts: dict[str, int] = {}
    for pos in roster_positions:
        if pos not in NON_ACTIVE:
            counts[pos] = counts.get(pos, 0) + 1
    return counts


def _fits_slot(player_positions: list[str], slot: str) -> bool:
    """Return True if the player can fill *slot*."""
    if slot == "Util":
        return any(p in _UTIL_ELIGIBLE for p in player_positions)
    if slot == "P":
        return any(p in _P_ELIGIBLE for p in player_positions)
    return slot in player_positions


def compute_roster_weights(
    player_rankings: list[PlayerRanking],
    roster_positions: list[str],
    il_player_ids: Optional[list[int]] = None,
    injured_statuses: Optional[dict[int, str]] = None,
) -> dict[int, float]:
    """Compute a scoring weight for every player on the roster.

    Parameters
    ----------
    player_rankings:
        All players on this team from the rankings lookup.
    roster_positions:
        League's slot template (``league.roster_positions``).
    il_player_ids:
        Player IDs currently in an IL/IL+/NA slot → weight 0.0.
    injured_statuses:
        ``{player_id: status}`` for hurt-but-not-IL players (DTD, Q, O).
        Applied as a multiplier on top of the slot weight.

    Returns
    -------
    dict[int, float]
        Weight for every player_id present in *player_rankings*.
    """
    il_ids = set(il_player_ids or [])
    inj    = {pid: s.upper() for pid, s in (injured_statuses or {}).items()}

    active_counts = parse_active_slots(roster_positions)

    # Separate IL-slotted players from those available for lineup
    available        = [r for r in player_rankings if r.player_id not in il_ids]
    available_sorted = sorted(available, key=lambda r: r.score, reverse=True)

    # ── Greedy slot assignment ────────────────────────────────────────────────
    slot_order = sorted(
        active_counts.keys(),
        key=lambda s: _SLOT_PRIORITY.index(s) if s in _SLOT_PRIORITY else 99,
    )
    assigned: set[int] = set()

    for slot in slot_order:
        capacity = active_counts[slot]
        filled   = 0
        for r in available_sorted:
            if filled >= capacity:
                break
            if r.player_id in assigned:
                continue
            if _fits_slot(r.positions or [], slot):
                assigned.add(r.player_id)
                filled += 1

    # ── Assign weights ────────────────────────────────────────────────────────
    weights: dict[int, float] = {}

    for r in player_rankings:
        pid = r.player_id
        if pid in il_ids:
            weights[pid] = 0.0
        elif pid in assigned:
            mult = INJURY_MULTIPLIERS.get(inj.get(pid, ""), 1.0)
            weights[pid] = round(mult, 4)
    # bench overflow handled below

    bench = [r for r in available_sorted if r.player_id not in assigned]
    for depth, r in enumerate(bench):
        positions = r.positions or []
        is_sp = "SP" in positions
        is_rp = "RP" in positions and not is_sp
        table = _SP_BENCH if is_sp else (_RP_BENCH if is_rp else _BATTER_BENCH)
        base  = table[min(depth, len(table) - 1)]
        mult  = INJURY_MULTIPLIERS.get(inj.get(r.player_id, ""), 1.0)
        weights[r.player_id] = round(base * mult, 4)

    return weights


def apply_weights(
    player_rankings: list[PlayerRanking],
    weights: dict[int, float],
) -> list[PlayerRanking]:
    """Return new PlayerRanking objects with scores scaled by *weights*.

    IL players (weight 0.0) are retained so position-breakdown displays and
    blurb context can still reference them — their score simply becomes zero.
    """
    result: list[PlayerRanking] = []
    for r in player_rankings:
        w = weights.get(r.player_id, 1.0)
        if w == 1.0:
            result.append(r)
        else:
            result.append(dataclasses.replace(
                r,
                score=round(r.score * w, 4),
                raw_score=round(r.raw_score * w, 4),
                category_contributions={
                    cat: round(v * w, 4)
                    for cat, v in r.category_contributions.items()
                },
            ))
    return result


def build_roster_notes(
    player_rankings: list[PlayerRanking],
    weights: dict[int, float],
    roster_positions: list[str],
    il_player_ids: Optional[list[int]] = None,
    injured_statuses: Optional[dict[int, str]] = None,
) -> dict:
    """Compute human-readable roster structure notes for AI blurb prompts.

    Returns a dict with keys:
      il_players       — list of names on IL
      active_injured   — list of names hurt but starting (DTD/Q/O)
      bench_overflow   — list of names who can't get a regular active slot
      position_surplus — dict of {position: overflow_count} (too many of one pos)
      position_deficit — dict of {position: deficit_count} (not enough for slots)
    """
    il_ids = set(il_player_ids or [])
    inj    = {pid: s.upper() for pid, s in (injured_statuses or {}).items()}
    active_slots = parse_active_slots(roster_positions)

    il_players:     list[str] = []
    active_injured: list[str] = []
    bench_overflow: list[str] = []

    for r in player_rankings:
        pid = r.player_id
        w   = weights.get(pid, 1.0)
        if pid in il_ids:
            il_players.append(r.name)
        elif w < 0.5:
            # genuinely bench-locked: less than half a slot's worth of starts.
            # Players with 0.5–0.99 weights are slightly discounted due to
            # position group congestion but still receive regular playing time.
            # A threshold of < 0.5 avoids mislabelling stars who merely share a
            # crowded OF/1B group as "bench players."
            bench_overflow.append(r.name)
        elif inj.get(pid):
            # starts but is hurt
            active_injured.append(r.name)

    # Position surplus / deficit
    position_surplus: dict[str, int] = {}
    position_deficit: dict[str, int] = {}

    available = [r for r in player_rankings if r.player_id not in il_ids]
    for pos, slots_needed in active_slots.items():
        eligible = [r for r in available if _fits_slot(r.positions or [], pos)]
        diff = len(eligible) - slots_needed
        if diff > 0:
            position_surplus[pos] = diff
        elif diff < 0:
            position_deficit[pos] = abs(diff)

    return {
        "il_players":       il_players,
        "active_injured":   active_injured,
        "bench_overflow":   bench_overflow,
        "position_surplus": position_surplus,
        "position_deficit": position_deficit,
    }
