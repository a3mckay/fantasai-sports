"""Player news fetcher for blurb context enrichment.

Fetches recent news items per player (MLB.com API or Rotoworld RSS),
applies a staleness filter, and returns structured items for blurb prompts.

Current status: STUB — returns empty lists.  The interface is defined so
blurb_scheduler.py can already include `recent_news: [...]` in prompts;
the model will see an empty list and behave normally.

Implementation plan (follow-on):
  1. MLB.com newsroom endpoint — per-player MLBAM ID query
  2. Rotoworld RSS feed — parse + player-name match
  3. Staleness filter: discard items older than 7 days
  4. Cache in Redis / local dict with TTL so we don't hammer the APIs
     every blurb run
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class PlayerNewsItem:
    """A single news item for a player."""
    headline: str
    body: str
    source: str
    published_at: Optional[datetime] = None
    tags: list[str] = field(default_factory=list)


def fetch_recent_news(
    player_id: int,
    mlbam_id: Optional[int] = None,
    player_name: Optional[str] = None,
    max_age_days: int = 7,
    max_items: int = 3,
) -> list[PlayerNewsItem]:
    """Return recent news items for a player.

    Args:
        player_id: FanGraphs player ID (primary key).
        mlbam_id: Optional MLB MLBAM ID for MLB.com API queries.
        player_name: Optional name for RSS feed matching.
        max_age_days: Discard items older than this many days.
        max_items: Maximum number of items to return.

    Returns:
        List of PlayerNewsItem, most recent first.
        Returns empty list until the live implementation is built.
    """
    # TODO: implement MLB.com API + Rotoworld RSS fetch
    return []


def format_news_for_prompt(items: list[PlayerNewsItem]) -> str:
    """Format news items as a compact block for a blurb prompt.

    Returns a string like:
        recent_news:
          - [2026-03-28] IL stint with hamstring tightness, expected back in 10 days (MLB.com)
          - [2026-03-25] Moved to leadoff in lineup (Rotoworld)

    Returns empty string when there are no items.
    """
    if not items:
        return ""

    lines = ["recent_news:"]
    for item in items:
        date_str = item.published_at.strftime("%Y-%m-%d") if item.published_at else "unknown"
        lines.append(f"  - [{date_str}] {item.headline} ({item.source})")
    return "\n".join(lines)
