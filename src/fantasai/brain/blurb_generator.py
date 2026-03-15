"""LLM-powered player blurb generator using the Anthropic API.

Generates 2–4 sentence analyst-style blurbs for ranked players. Designed to
read like a FantasyPros/Rotoball analyst wrote it — not generic filler.

Cost strategy
-------------
* Prompt caching: the system prompt is marked with cache_control so it's
  cached server-side after the first request (up to 90% cost reduction on
  the system prompt portion across repeated calls).
* Parallel generation: on-demand requests generate blurbs in parallel via
  ThreadPoolExecutor — 15 blurbs complete in ~1–2 seconds instead of ~15s.
* Batches API: nightly pipeline uses the Batches API (50% cost reduction)
  for full-roster blurb refreshes. Returns a batch_id for async collection.
* Tiering: only top-N players receive blurbs; lower-ranked players get None.

Usage
-----
Single blurb (on-demand, e.g. from an API request)::

    gen = BlurbGenerator()
    blurb = gen.generate_blurb(ranking, ranking_type="lookback", categories=[...])

Parallel batch (for waiver recommendations)::

    blurbs = gen.generate_blurbs_parallel(
        rankings[:15], ranking_type="predictive", categories=[...]
    )

Async Batches API (nightly pipeline)::

    batch_id = gen.submit_blurb_batch(rankings, ranking_type="lookback", ...)
    # ... poll later ...
    results = gen.collect_batch_results(batch_id)
"""
from __future__ import annotations

import hashlib
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from fantasai.brain.writer_persona import SYSTEM_PROMPT as _SYSTEM_PROMPT
from fantasai.engine.scoring import PlayerRanking

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Sonnet 4.6: excellent creative writing quality at $3/$15 per 1M tokens —
# the right balance for high-volume short-form content generation.
# Upgrade to claude-opus-4-6 only if quality becomes a blocker.
MODEL = "claude-sonnet-4-6"

# Max parallel threads for on-demand generation.
MAX_WORKERS = 8

# Number of top players to generate blurbs for. Players ranked below this
# threshold return None (avoids spending on low-value tail players).
DEFAULT_TOP_N = 100


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_category_signals(contributions: dict[str, float]) -> str:
    """Translate category contributions into analyst-readable signals.

    Deliberately avoids z-score terminology — the model sees "elite",
    "strong", "average", "below average", "drag" instead of raw σ values.
    """
    if not contributions:
        return "  (no category signal data available)"

    TIERS = [
        (2.0, "elite"),
        (1.0, "strong"),
        (0.3, "average"),
        (-0.3, "slightly below average"),
        (-1.0, "below average"),
        (-2.0, "drag"),
    ]

    def _tier(z: float) -> str:
        for threshold, label in TIERS:
            if z >= threshold:
                return label
        return "significant drag"

    lines = []
    for cat, z in sorted(contributions.items(), key=lambda kv: -abs(kv[1])):
        lines.append(f"  {cat}: {_tier(z)} ({'+' if z >= 0 else ''}{z:.1f})")
    return "\n".join(lines)


def _format_raw_stats(raw_stats: dict[str, float]) -> str:
    """Format raw stats as a clean, human-readable data block."""
    top = sorted(raw_stats.items(), key=lambda kv: abs(kv[1]), reverse=True)[:10]
    return "  " + " | ".join(f"{k}: {v}" for k, v in top)


def _make_user_prompt(
    ranking: PlayerRanking,
    ranking_type: str,
    scoring_categories: list[str],
    raw_stats: Optional[dict[str, float]] = None,
    rolling_windows: Optional[dict[str, dict[str, float]]] = None,
) -> str:
    """Build the per-player prompt for blurb generation.

    All facts the model may cite must appear in this prompt. The DATA BLOCK
    header signals to the model that only these figures are in-bounds.
    """
    positions_str = ", ".join(ranking.positions) if ranking.positions else "UTIL"
    ranking_label = (
        "LOOKBACK (season-to-date performance)"
        if ranking_type == "lookback"
        else "PREDICTIVE (forward-looking projection)"
    )
    signals_str = _format_category_signals(ranking.category_contributions)

    lines = [
        "━━━ DATA BLOCK — ONLY CITE FACTS FROM THIS BLOCK ━━━",
        f"Player: {ranking.name} | Team: {ranking.team} | Positions: {positions_str}",
        f"Stat type: {ranking.stat_type} | Ranking type: {ranking_label}",
        f"Overall rank: #{ranking.overall_rank} (among all rostered + available players)",
        f"League scoring categories: {', '.join(scoring_categories)}",
        "",
        "Category signals (season-to-date):",
        signals_str,
    ]

    if raw_stats:
        lines += [
            "",
            "Key stats:",
            _format_raw_stats(raw_stats),
        ]

    if rolling_windows:
        for window_label, stats in rolling_windows.items():
            rank_note = f" | Rank: #{stats.pop('rank', '—')} overall ({window_label})" if 'rank' in stats else ""
            lines += [
                "",
                f"{window_label} stats:{rank_note}",
                _format_raw_stats(stats),
            ]

    lines += [
        "━━━ END DATA BLOCK ━━━",
        "",
        "Write the blurb:",
    ]

    return "\n".join(lines)


def _parse_batch_response(
    text: str,
    eligible: list[PlayerRanking],
) -> dict[int, str]:
    """Parse a batch blurb response into a player_id → blurb dict.

    Expected format:
        [player_id=123]
        blurb text here...

        [player_id=456]
        blurb text here...

    Falls back gracefully: if fewer blurbs are found than expected, returns
    whatever was successfully parsed rather than raising.
    """
    pattern = re.compile(
        r'\[player_id=(\d+)\]\s*(.*?)(?=\n\s*\[player_id=\d+\]|\Z)',
        re.DOTALL,
    )
    matches = pattern.findall(text)

    if not matches:
        # Try positional fallback: split on blank lines and map by index
        logger.warning("Batch response had no [player_id=...] markers; trying positional parse")
        chunks = [c.strip() for c in re.split(r'\n{2,}', text.strip()) if c.strip()]
        return {
            r.player_id: chunks[i]
            for i, r in enumerate(eligible)
            if i < len(chunks)
        }

    result: dict[int, str] = {}
    for pid_str, blurb_text in matches:
        try:
            pid = int(pid_str)
            blurb = blurb_text.strip()
            if blurb:
                result[pid] = blurb
        except ValueError:
            logger.warning("Could not parse player_id from batch marker: %s", pid_str)

    missing = [r.player_id for r in eligible if r.player_id not in result]
    if missing:
        logger.warning("Batch response missing blurbs for player_ids: %s", missing)

    return result


def _stats_hash(ranking: PlayerRanking) -> str:
    """Stable hash of a player's scoring data — used for cache invalidation."""
    key = f"{ranking.player_id}:{ranking.score:.4f}:" + ":".join(
        f"{k}={v:.4f}" for k, v in sorted(ranking.category_contributions.items())
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# BlurbGenerator
# ---------------------------------------------------------------------------


class BlurbGenerator:
    """Generate analyst-style blurbs via the Anthropic API.

    Instantiate once per process and reuse — the client is thread-safe.
    """

    def __init__(self, api_key: Optional[str] = None) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)

    # ------------------------------------------------------------------
    # Single blurb (synchronous)
    # ------------------------------------------------------------------

    def generate_blurb(
        self,
        ranking: PlayerRanking,
        ranking_type: str,
        scoring_categories: list[str],
        raw_stats: Optional[dict[str, float]] = None,
        rolling_windows: Optional[dict[str, dict[str, float]]] = None,
    ) -> str:
        """Generate a single blurb synchronously.

        Uses prompt caching on the system prompt to reduce costs on repeated calls.

        Args:
            ranking: The player's ranking data including category signals.
            ranking_type: "lookback" or "predictive".
            scoring_categories: League scoring categories (for context).
            raw_stats: Optional {stat: value} for the season-to-date data block.
            rolling_windows: Optional {"Last 14 days": {stat: value}, ...} for
                recent performance windows. Keys become section headers.

        Returns:
            2–4 sentence analyst blurb as a string.
        """
        user_prompt = _make_user_prompt(
            ranking, ranking_type, scoring_categories, raw_stats, rolling_windows
        )

        response = self._client.messages.create(
            model=MODEL,
            max_tokens=256,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_prompt}],
        )

        text_blocks = [b for b in response.content if b.type == "text"]
        if not text_blocks:
            raise ValueError(f"No text in Anthropic response for player {ranking.player_id}")
        return text_blocks[0].text.strip()

    # ------------------------------------------------------------------
    # Single-call generation (on-demand, preferred for small batches)
    # ------------------------------------------------------------------

    def generate_blurbs_single_call(
        self,
        rankings: list[PlayerRanking],
        ranking_type: str,
        scoring_categories: list[str],
        raw_stats_map: Optional[dict[int, dict[str, float]]] = None,
        rolling_windows_map: Optional[dict[int, dict[str, dict[str, float]]]] = None,
        top_n: int = DEFAULT_TOP_N,
    ) -> dict[int, str]:
        """Generate blurbs for multiple players in a single API call.

        Preferred over generate_blurbs_parallel for on-demand batches of ≤ 20
        players because:
        - The model sees all players at once and naturally avoids phrase
          repetition, varied openers, and varied stat choices.
        - One API call instead of N, so prompt-cache savings are maximised.
        - Better coherence: the writing reads like a single analyst session.

        Falls back to generate_blurbs_parallel if response parsing fails.

        Args:
            rankings: PlayerRanking objects to generate blurbs for.
            ranking_type: "lookback" or "predictive".
            scoring_categories: League scoring categories.
            raw_stats_map: Optional {player_id: {stat: value}} season stats.
            rolling_windows_map: Optional {player_id: {"Last 14 days": {...}}}.
            top_n: Only generate for top N players (0 = all).

        Returns:
            Dict mapping player_id → blurb text.
        """
        eligible = [r for r in rankings if r.overall_rank <= top_n or top_n == 0]
        if not eligible:
            return {}

        # Build a combined message with all data blocks separated by markers
        player_sections = []
        for r in eligible:
            stats = (raw_stats_map or {}).get(r.player_id)
            windows = (rolling_windows_map or {}).get(r.player_id)
            data_block = _make_user_prompt(r, ranking_type, scoring_categories, stats, windows)
            player_sections.append(f"[player_id={r.player_id}]\n{data_block}")

        batch_prompt = (
            f"Write blurbs for the following {len(eligible)} players. "
            "Apply all persona and voice guidelines. "
            "This is a single writing session — vary your language, openers, "
            "closers, and featured stats across the set. "
            "No phrase should appear twice.\n\n"
            "Format: start each blurb with its marker on its own line, "
            "e.g. [player_id=123], then the blurb text. "
            "Nothing else — no preamble, no summary.\n\n"
            + "\n\n".join(player_sections)
        )

        try:
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=min(512 * len(eligible), 4096),
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": batch_prompt}],
            )

            text_blocks = [b for b in response.content if b.type == "text"]
            if not text_blocks:
                raise ValueError("No text in Anthropic response for batch")

            return _parse_batch_response(text_blocks[0].text, eligible)

        except Exception as exc:
            logger.warning(
                "Single-call batch generation failed (%s), falling back to parallel", exc
            )
            return self.generate_blurbs_parallel(
                rankings, ranking_type, scoring_categories,
                raw_stats_map, rolling_windows_map, top_n,
            )

    # ------------------------------------------------------------------
    # Parallel generation (fallback / large batches)
    # ------------------------------------------------------------------

    def generate_blurbs_parallel(
        self,
        rankings: list[PlayerRanking],
        ranking_type: str,
        scoring_categories: list[str],
        raw_stats_map: Optional[dict[int, dict[str, float]]] = None,
        rolling_windows_map: Optional[dict[int, dict[str, dict[str, float]]]] = None,
        top_n: int = DEFAULT_TOP_N,
    ) -> dict[int, str]:
        """Generate blurbs for multiple players in parallel.

        Uses a ThreadPoolExecutor so all API calls happen concurrently.
        Failed individual blurbs are logged and skipped (returns partial results).

        Args:
            rankings: List of PlayerRanking objects.
            ranking_type: "lookback" or "predictive".
            scoring_categories: League scoring categories.
            raw_stats_map: Optional {player_id: {stat: value}} for season stats.
            rolling_windows_map: Optional {player_id: {"Last 14 days": {stat: value}, ...}}
                for recent performance windows to include in the data block.
            top_n: Only generate blurbs for the top N players by rank.

        Returns:
            Dict mapping player_id → blurb text for players that succeeded.
        """
        eligible = [r for r in rankings if r.overall_rank <= top_n or top_n == 0]

        results: dict[int, str] = {}

        def _generate(r: PlayerRanking) -> tuple[int, str]:
            stats = (raw_stats_map or {}).get(r.player_id)
            windows = (rolling_windows_map or {}).get(r.player_id)
            blurb = self.generate_blurb(r, ranking_type, scoring_categories, stats, windows)
            return r.player_id, blurb

        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(eligible))) as pool:
            futures = {pool.submit(_generate, r): r for r in eligible}
            for future in as_completed(futures):
                ranking = futures[future]
                try:
                    pid, blurb = future.result()
                    results[pid] = blurb
                except Exception as exc:
                    logger.warning(
                        "Blurb generation failed for player %s (%s): %s",
                        ranking.player_id,
                        ranking.name,
                        exc,
                    )

        return results

    # ------------------------------------------------------------------
    # Batches API (nightly pipeline — 50% cost reduction)
    # ------------------------------------------------------------------

    def submit_blurb_batch(
        self,
        rankings: list[PlayerRanking],
        ranking_type: str,
        scoring_categories: list[str],
        raw_stats_map: Optional[dict[int, dict[str, float]]] = None,
        rolling_windows_map: Optional[dict[int, dict[str, dict[str, float]]]] = None,
        top_n: int = DEFAULT_TOP_N,
    ) -> str:
        """Submit blurb generation to the Anthropic Batches API.

        Batches are processed asynchronously (typically < 1 hour) at 50%
        of standard pricing. Call collect_batch_results() with the returned
        batch_id once the batch has completed.

        Returns:
            batch_id string — pass to collect_batch_results().
        """
        eligible = [r for r in rankings if r.overall_rank <= top_n or top_n == 0]
        if not eligible:
            raise ValueError("No eligible players for blurb batch generation")

        requests = []
        for r in eligible:
            stats = (raw_stats_map or {}).get(r.player_id)
            windows = (rolling_windows_map or {}).get(r.player_id)
            user_prompt = _make_user_prompt(r, ranking_type, scoring_categories, stats, windows)
            custom_id = f"{r.player_id}_{ranking_type}"

            requests.append(
                Request(
                    custom_id=custom_id,
                    params=MessageCreateParamsNonStreaming(
                        model=MODEL,
                        max_tokens=256,
                        system=[
                            {
                                "type": "text",
                                "text": _SYSTEM_PROMPT,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                        messages=[{"role": "user", "content": user_prompt}],
                    ),
                )
            )

        batch = self._client.messages.batches.create(requests=requests)
        logger.info(
            "Submitted blurb batch %s with %d requests (type=%s)",
            batch.id,
            len(requests),
            ranking_type,
        )
        return batch.id

    def collect_batch_results(
        self,
        batch_id: str,
    ) -> dict[int, str]:
        """Retrieve results from a completed Anthropic batch.

        Parses custom_id (format: "{player_id}_{ranking_type}") to key
        results by player_id. Failed requests are logged and excluded.

        Returns:
            Dict mapping player_id → blurb text.
        """
        results: dict[int, str] = {}

        for result in self._client.messages.batches.results(batch_id):
            if result.result.type != "succeeded":
                logger.warning(
                    "Batch request %s failed: %s",
                    result.custom_id,
                    result.result.type,
                )
                continue

            try:
                player_id_str = result.custom_id.split("_")[0]
                player_id = int(player_id_str)
            except (ValueError, IndexError):
                logger.warning("Could not parse player_id from custom_id: %s", result.custom_id)
                continue

            msg = result.result.message
            text_blocks = [b for b in msg.content if b.type == "text"]
            if text_blocks:
                results[player_id] = text_blocks[0].text.strip()

        logger.info("Collected %d blurbs from batch %s", len(results), batch_id)
        return results

    def get_batch_status(self, batch_id: str) -> str:
        """Return the current processing status of a batch."""
        batch = self._client.messages.batches.retrieve(batch_id)
        return batch.processing_status


# ---------------------------------------------------------------------------
# Module-level convenience — lazy singleton
# ---------------------------------------------------------------------------

_generator: Optional[BlurbGenerator] = None


def get_blurb_generator(api_key: Optional[str] = None) -> BlurbGenerator:
    """Return the module-level BlurbGenerator singleton.

    Lazy-initialized on first call. Subsequent calls return the same instance.
    Pass api_key on first call only; it is ignored on subsequent calls.
    """
    global _generator
    if _generator is None:
        _generator = BlurbGenerator(api_key=api_key)
    return _generator
