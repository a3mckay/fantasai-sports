"""Yahoo Fantasy transaction log polling.

Fetches new transactions from Yahoo for all connected leagues, deduplicates
against the transactions table, and returns unseen transactions for grading.

Called by the APScheduler job every 20 minutes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

_YAHOO_FANTASY_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"

# Map Yahoo's verbose transaction types → our canonical set (add | drop | trade)
_TYPE_MAP: dict[str, str] = {
    "add": "add",
    "drop": "drop",
    "waiver": "add",          # waiver claim: shows as an "add" (may also drop)
    "add/drop": "add",        # combined waiver claim
    "trade": "trade",
    "accepted_trade": "trade",
    "pending_trade": "trade",
}


def _normalize_txn_type(raw_type: str) -> str:
    """Normalize a Yahoo transaction type to our canonical add/drop/trade."""
    return _TYPE_MAP.get(raw_type, raw_type)


def fetch_league_transactions(access_token: str, league_key: str, count: int = 50) -> list[dict]:
    """Fetch the most recent `count` transactions for a league from Yahoo.

    Returns a list of raw transaction dicts with keys:
        transaction_id, type, timestamp, players (list of player dicts with
        player_key, name, transaction_data containing type/source/destination team)
    """
    import httpx

    # Yahoo Fantasy API v2 uses semicolons for path-level resource filters,
    # not query params.  type/count must be in the path.
    # Include all relevant transaction types: waiver (add from waivers + optional drop),
    # add (add from FA), drop, and accepted_trade / pending_trade for trades.
    url = (
        f"{_YAHOO_FANTASY_BASE}/league/{league_key}"
        f"/transactions;type=add,drop,waiver,accepted_trade,pending_trade;count={count}"
    )
    params = {"format": "json"}
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=15)
        _log.debug("fetch_league_transactions: %s → HTTP %s", resp.url, resp.status_code)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        _log.error("fetch_league_transactions: failed for league %s", league_key, exc_info=True)
        return []

    # Navigate Yahoo's deeply nested JSON structure.
    # Yahoo returns league as a 2-element list: [league_info, {sub-resources}]
    try:
        league_list = data.get("fantasy_content", {}).get("league", [])
        last_elem = league_list[-1] if league_list else {}
        transactions_raw = last_elem.get("transactions", {})
    except (IndexError, AttributeError, TypeError):
        _log.warning("fetch_league_transactions: unexpected response shape for %s", league_key)
        return []

    # Yahoo returns transactions as a dict {"count": N, "0": {...}, ...} when
    # transactions exist, but may return a list, empty string, or empty dict
    # when there are none (varies by season / league state).
    if isinstance(transactions_raw, list):
        # List format: [{"count": N}, {transaction_data...}] — extract count
        count_in_list = transactions_raw[0].get("count", 0) if transactions_raw else 0
        _log.info(
            "fetch_league_transactions: Yahoo returned list format with count=%s for %s",
            count_in_list, league_key,
        )
        if count_in_list == 0:
            return []
        # Non-zero count in list format is unexpected — fall through to warning
        transactions_raw = {}  # will hit the warning below

    if not transactions_raw:
        # Empty dict/string — no transactions yet (normal at start of season)
        _log.info(
            "fetch_league_transactions: Yahoo returned 0 transactions for %s "
            "(empty response — normal if season just started or no moves yet)",
            league_key,
        )
        return []

    if not isinstance(transactions_raw, dict):
        _log.warning(
            "fetch_league_transactions: unexpected transactions type %s for %s",
            type(transactions_raw).__name__, league_key,
        )
        return []

    results = []
    count_val = transactions_raw.get("count", 0)
    _log.info("fetch_league_transactions: Yahoo reports %s transactions for %s", count_val, league_key)
    for i in range(int(count_val)):
        raw = transactions_raw.get(str(i), {}).get("transaction", [])
        if not raw:
            continue
        try:
            meta = raw[0]
            players_block = raw[1].get("players", {}) if len(raw) > 1 else {}
            txn_id = meta.get("transaction_id", "")
            # Normalize Yahoo's verbose types to our canonical set (add/drop/trade)
            _raw_type = meta.get("type", "")
            txn_type = _normalize_txn_type(_raw_type)
            timestamp_raw = meta.get("timestamp")
            timestamp = (
                datetime.fromtimestamp(int(timestamp_raw), tz=timezone.utc)
                if timestamp_raw else None
            )

            # Parse players involved
            players = []
            if isinstance(players_block, dict):
                player_count = players_block.get("count", 0)
                for j in range(int(player_count)):
                    p_raw = players_block.get(str(j), {}).get("player", [])
                    if not p_raw:
                        continue
                    p_info = p_raw[0] if isinstance(p_raw[0], list) else [p_raw[0]]
                    p_meta = {}
                    for item in p_info:
                        if isinstance(item, dict):
                            p_meta.update(item)
                    p_txn_data = p_raw[1].get("transaction_data", [{}])[0] if len(p_raw) > 1 else {}
                    players.append({
                        "player_key": p_meta.get("player_key", ""),
                        "name": p_meta.get("full_name", p_meta.get("name", {}).get("full", "")),
                        "type": p_txn_data.get("type", ""),           # "add" | "drop"
                        "source_team_key": p_txn_data.get("source_team_key", ""),
                        "destination_team_key": p_txn_data.get("destination_team_key", ""),
                        "source_type": p_txn_data.get("source_type", ""),        # "waivers"|"freeagents"|"team"
                        "destination_type": p_txn_data.get("destination_type", ""),
                    })

            if txn_id:
                results.append({
                    "transaction_id": txn_id,
                    "type": txn_type,
                    "timestamp": timestamp,
                    "players": players,
                    # For trades, also pull trader/tradee team keys
                    "trader_team_key": meta.get("trader_team_key", ""),
                    "tradee_team_key": meta.get("tradee_team_key", ""),
                })
        except Exception:
            _log.debug("fetch_league_transactions: failed to parse transaction %d", i, exc_info=True)
            continue

    return results


def build_participants(
    txn: dict,
    teams_by_key: dict[str, Any],
    players_by_name: dict[str, int],
) -> list[dict]:
    """Convert a raw Yahoo transaction dict into our participants JSON schema."""
    txn_type = txn["type"]
    players = txn.get("players", [])

    if txn_type in ("add", "drop"):
        parts = []
        for p in players:
            # Each player entry carries its own per-player action ("add" or "drop"),
            # which matters for waiver claims stored as "add" that also drop a player.
            action = p.get("type") or txn_type
            team_key = p.get("destination_team_key") or p.get("source_team_key", "")
            team = teams_by_key.get(team_key, {})
            parts.append({
                "manager_name": team.get("manager_name", ""),
                "team_key": team_key,
                "team_name": team.get("team_name", ""),
                "player_name": p.get("name", ""),
                "player_id": players_by_name.get(p.get("name", "")),
                "action": action,
            })
        return parts

    if txn_type == "trade":
        # Group players by destination team
        sides: dict[str, dict] = {}
        for p in players:
            dest_key = p.get("destination_team_key", "")
            src_key = p.get("source_team_key", "")
            # Player moves from src_key to dest_key — dest gains them
            if dest_key not in sides:
                team = teams_by_key.get(dest_key, {})
                sides[dest_key] = {
                    "manager_name": team.get("manager_name", ""),
                    "team_key": dest_key,
                    "team_name": team.get("team_name", ""),
                    "players_added": [],
                    "players_dropped": [],
                }
            sides[dest_key]["players_added"].append({
                "player_name": p.get("name", ""),
                "player_id": players_by_name.get(p.get("name", "")),
            })
            if src_key and src_key not in sides:
                team = teams_by_key.get(src_key, {})
                sides[src_key] = {
                    "manager_name": team.get("manager_name", ""),
                    "team_key": src_key,
                    "team_name": team.get("team_name", ""),
                    "players_added": [],
                    "players_dropped": [],
                }
            if src_key:
                sides[src_key]["players_dropped"].append({
                    "player_name": p.get("name", ""),
                    "player_id": players_by_name.get(p.get("name", "")),
                })
        return list(sides.values())

    return []


def poll_all_leagues(count: int = 50, is_backfill: bool = False) -> int:
    """Poll Yahoo transaction logs for all connected leagues.

    Fetches new transactions, stores them in the DB, and kicks off grading.
    Returns count of new transactions found.
    Called by APScheduler every 20 minutes.

    Args:
        count: Number of most-recent transactions to fetch from Yahoo.
        is_backfill: If True, marks all inserted transactions as backfill so
            they never appear in the ticker.
    """
    from fantasai.database import SessionLocal
    from fantasai.models.league import League, Team
    from fantasai.models.transaction import Transaction
    from fantasai.models.user import YahooConnection
    from fantasai.services.yahoo_sync import get_valid_access_token

    db: "Session" = SessionLocal()
    total_new = 0

    try:
        # Get all active Yahoo connections
        connections = (
            db.query(YahooConnection)
            .filter(YahooConnection.encrypted_access_token.isnot(None))
            .all()
        )

        for conn in connections:
            if not conn.league_key:
                continue
            try:
                access_token = get_valid_access_token(conn, db)
            except Exception:
                _log.warning("poll_all_leagues: token refresh failed for conn %s", conn.id)
                continue

            league = db.query(League).filter(League.league_id == conn.league_key).first()
            if not league:
                continue

            # Build team lookup
            teams = db.query(Team).filter(Team.league_id == conn.league_key).all()
            teams_by_key: dict[str, Any] = {
                t.yahoo_team_key: {"manager_name": t.manager_name, "team_name": t.team_name or ""}
                for t in teams if t.yahoo_team_key
            }

            # Build player name → player_id lookup
            from fantasai.models.player import Player
            all_players = db.query(Player).all()
            players_by_name: dict[str, int] = {p.name: p.player_id for p in all_players}

            # If this league has never had transactions polled, do a full backfill
            # automatically so the Move Grades feed is populated on first use.
            existing_count = db.query(Transaction).filter(
                Transaction.league_id == conn.league_key
            ).count()
            effective_count = count
            effective_backfill = is_backfill
            if existing_count == 0 and not is_backfill:
                effective_count = 200
                effective_backfill = True
                _log.info(
                    "poll_all_leagues: first-run backfill for league %s (no existing transactions)",
                    conn.league_key,
                )

            raw_transactions = fetch_league_transactions(access_token, conn.league_key, count=effective_count)

            for txn in raw_transactions:
                yahoo_id = f"{conn.league_key}:{txn['transaction_id']}"
                existing = db.query(Transaction).filter(
                    Transaction.yahoo_transaction_id == yahoo_id
                ).first()
                if existing:
                    continue  # already seen

                participants = build_participants(txn, teams_by_key, players_by_name)

                new_txn = Transaction(
                    yahoo_transaction_id=yahoo_id,
                    league_id=conn.league_key,
                    transaction_type=txn["type"],
                    participants=participants,
                    yahoo_timestamp=txn.get("timestamp"),
                    is_backfill=effective_backfill,
                )
                db.add(new_txn)
                total_new += 1

            db.commit()

            # Grade all ungraded transactions for this league
            _grade_ungraded(db, conn.league_key, league)

    except Exception:
        _log.error("poll_all_leagues: unexpected error", exc_info=True)
        db.rollback()
    finally:
        db.close()

    if total_new:
        _log.info("poll_all_leagues: found %d new transactions (backfill=%s)", total_new, is_backfill)
    return total_new


def _grade_ungraded(db: "Session", league_id: str, league: Any) -> None:
    """Grade any transactions in this league that haven't been graded yet."""
    from fantasai.brain.move_grader import grade_transaction
    from fantasai.models.transaction import Transaction

    ungraded = (
        db.query(Transaction)
        .filter(
            Transaction.league_id == league_id,
            Transaction.grade_letter.is_(None),
        )
        .order_by(Transaction.yahoo_timestamp)
        .limit(50)
        .all()
    )

    for txn in ungraded:
        try:
            grade_transaction(db, txn, league)
            db.commit()
        except Exception:
            db.rollback()
            _log.error(
                "poll_all_leagues: grading failed for txn %s", txn.yahoo_transaction_id,
                exc_info=True,
            )
