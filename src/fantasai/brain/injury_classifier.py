"""Automated injury severity classifier for fantasy baseball projections.

Determines whether a player's injury description warrants a ``risk_flag``
(``"recent_surgery"`` or ``None``) that scales down their projected IP/PA.

Design
------
Three-layer classification:

1. **Keyword fast-path** — unambiguous terms resolve instantly without an API call.
   - Serious (→ ``"recent_surgery"``): surgery, torn, ucl, tjs, fracture, break,
     rupture, reconstruction, ligament, tendon repair.
   - Minor (→ ``None``): tightness, soreness, bruise, contusion, fatigue, cramps.

2. **Claude API** — ambiguous descriptions (e.g. "elbow inflammation on 60-day IL")
   are sent to ``claude-sonnet-4-6`` for structured analysis. The model returns a
   JSON object with ``risk_flag``, ``risk_note``, and ``reasoning``.

3. **Conservative fallback** — if the API key is absent or the call fails, apply
   a conservative heuristic: 60-day IL → ``"recent_surgery"``; 10-day IL / DTD → ``None``.

Rules
-----
* ``"fragile"`` is **never** set automatically — it requires observing a multi-year
  injury history pattern and must be set manually via ``POST /rankings/set-risk-flag``.
* An existing ``"fragile"`` flag is **never** overwritten; it always takes priority.
* The returned ``risk_note`` is always a concise human-readable label (≤ 80 chars),
  suitable for the ⚠ tooltip in the UI.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Keyword sets for the fast-path
# ---------------------------------------------------------------------------

# Any of these in the description → "recent_surgery"
_SERIOUS_PATTERN = re.compile(
    r"\b("
    r"surgery|surgical|operated|operation"
    r"|torn|tear|tore"
    r"|ucl|tjs|tommy.?john"
    r"|fracture|fractured|broken|break"
    r"|rupture|ruptured"
    r"|reconstruction|reconstructed"
    r"|ligament|tendon repair|labrum"
    r"|replaced|removal|removed"
    r"|elbow.{0,20}(procedure|repair|scope)"
    r"|shoulder.{0,20}(procedure|repair|scope)"
    r")\b",
    re.IGNORECASE,
)

# Any of these alone → None (minor, no projection adjustment)
_MINOR_PATTERN = re.compile(
    r"\b("
    r"tightness|tight"
    r"|soreness|sore"
    r"|bruise|bruised|bruising|contusion"
    r"|fatigue|fatigued"
    r"|cramp|cramping"
    r"|blister"
    r"|illness|sick|flu|covid"
    r"|rest|precautionary|maintenance"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

ClassificationResult = tuple[Optional[str], str]  # (risk_flag, risk_note)


def classify_injury(
    description: str,
    il_status: str,
    api_key: str = "",
    player_name: str = "",
) -> ClassificationResult:
    """Classify an injury description and return (risk_flag, risk_note).

    Args:
        description: Free-text injury description from the MLB Stats API or
                     manual override (e.g. "UCL reconstruction, TJS").
        il_status:   Current IL status string: "il_10", "il_60", "day_to_day",
                     "out_for_season", or "active".
        api_key:     Anthropic API key. If empty, falls back to heuristic.
        player_name: Optional player name for logging context.

    Returns:
        Tuple of ``(risk_flag, risk_note)`` where ``risk_flag`` is either
        ``"recent_surgery"`` or ``None``.
    """
    if not description:
        return _conservative_heuristic(il_status)

    text = description.strip()

    # --- Layer 1: Keyword fast-path -----------------------------------------
    if _SERIOUS_PATTERN.search(text):
        note = _derive_note_from_description(text) or "Significant structural injury"
        logger.debug("Injury fast-path [serious]: %r → recent_surgery (%s)", text[:60], player_name)
        return "recent_surgery", note

    if _MINOR_PATTERN.search(text) and il_status in ("il_10", "day_to_day"):
        logger.debug("Injury fast-path [minor]: %r → None (%s)", text[:60], player_name)
        return None, ""

    # --- Layer 2: Claude API -------------------------------------------------
    if api_key:
        result = _classify_with_claude(text, il_status, api_key, player_name)
        if result is not None:
            return result

    # --- Layer 3: Conservative heuristic ------------------------------------
    return _conservative_heuristic(il_status)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _derive_note_from_description(text: str) -> str:
    """Extract a concise note from the raw description (≤ 80 chars)."""
    # Capitalise and truncate
    note = text.strip().rstrip(".")
    if len(note) > 80:
        note = note[:77] + "…"
    return note


def _conservative_heuristic(il_status: str) -> ClassificationResult:
    """Fall-back when neither keywords nor the API resolve ambiguity."""
    if il_status in ("il_60", "out_for_season"):
        return "recent_surgery", "Significant injury (60-day IL)"
    return None, ""


_CLAUDE_SYSTEM = (
    "You are a baseball injury analyst for a fantasy sports application. "
    "Given a player's injury description and IL status, determine whether the injury "
    "warrants a projection discount flag.\n\n"
    "Rules:\n"
    "- Return risk_flag = \"recent_surgery\" ONLY for structural injuries that will "
    "materially limit a player's PA or IP over the coming months: surgeries, "
    "torn ligaments/tendons, fractures, labrum repairs, Tommy John, UCL procedures.\n"
    "- Return risk_flag = null for soft-tissue issues (tightness, soreness, bruises), "
    "illnesses, rest stints, or minor strains unlikely to limit full-season volume.\n"
    "- Never return \"fragile\" — that is a manual-only flag.\n"
    "- risk_note must be ≤ 80 characters, written in plain English for a tooltip "
    "(e.g. \"Shoulder surgery, return timeline uncertain\").\n\n"
    "Respond with ONLY valid JSON matching this schema:\n"
    "{\"risk_flag\": \"recent_surgery\" | null, \"risk_note\": string, \"reasoning\": string}"
)


def _classify_with_claude(
    description: str,
    il_status: str,
    api_key: str,
    player_name: str = "",
) -> Optional[ClassificationResult]:
    """Call Claude API to classify ambiguous injury descriptions.

    Returns ``None`` on any error so the caller can fall through to the heuristic.
    """
    try:
        import anthropic  # local import to avoid hard dep in tests

        client = anthropic.Anthropic(api_key=api_key)
        user_msg = (
            f"Player: {player_name or 'Unknown'}\n"
            f"IL status: {il_status}\n"
            f"Injury description: {description}\n\n"
            "Classify this injury."
        )
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=_CLAUDE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code fence if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL).strip()
        data = json.loads(raw)
        risk_flag = data.get("risk_flag") or None
        # Validate — only allow known values
        if risk_flag not in (None, "recent_surgery"):
            logger.warning("Claude returned unexpected risk_flag %r — ignoring", risk_flag)
            risk_flag = None
        risk_note = (data.get("risk_note") or "").strip()[:80]
        logger.info(
            "Injury classified by Claude: %r → %r (%s)",
            description[:60],
            risk_flag,
            player_name,
        )
        return risk_flag, risk_note
    except Exception as exc:
        logger.warning("Claude injury classification failed (%s): %s", player_name, exc)
        return None


# ---------------------------------------------------------------------------
# Helper used by the rankings endpoints
# ---------------------------------------------------------------------------

def maybe_apply_classification(
    player,  # fantasai.models.player.Player ORM object
    description: str,
    il_status: str,
    api_key: str = "",
) -> None:
    """Classify an injury and apply the result to *player* in-place.

    Respects the priority rule: existing ``"fragile"`` flags are never overwritten.
    Only ``"recent_surgery"`` or clearing to ``None`` is permitted here.

    The ``player`` object must already be associated with a SQLAlchemy session;
    the caller is responsible for committing.
    """
    if player.risk_flag == "fragile":
        # Manual chronic-risk flag always wins — do not touch it.
        logger.debug(
            "Skipping auto-classification for %s: 'fragile' flag is manual-only",
            player.name,
        )
        return

    risk_flag, risk_note = classify_injury(
        description=description,
        il_status=il_status,
        api_key=api_key,
        player_name=player.name,
    )

    player.risk_flag = risk_flag
    player.risk_note = risk_note or None
