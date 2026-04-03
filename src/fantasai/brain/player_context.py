"""Shared player context utilities for LLM prompts.

Provides three functions used across Compare, Trade Evaluator, and
Scheduled Rankings blurbs to ensure consistent, data-grounded context.
The gold-standard pattern is move_grader._get_player_facts(), which
already handles rate stats, injury context, and workload — this module
makes those patterns reusable without duplicating 200 lines of logic.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

# 2026 season start for sample-size calculations
_SEASON_START_2026 = date(2026, 3, 26)


def build_player_stats_block(
    player_id: int,
    db: "Session",
    stat_type: Optional[str] = None,
) -> str:
    """Return an enriched stats string for use in LLM prompts.

    Includes:
    - Data source label (2026 actual with sample size, or Steamer projection)
    - Rate stats: AVG/OBP/SLG for batters; ERA/WHIP/K9 for pitchers
    - Advanced stats: xwOBA/xBA/Barrel%/HardHit% for batters;
      xERA/xFIP/SIERA/K-BB% for pitchers
    - Projected counting stats clearly labelled when source is projection

    The data source label prevents the LLM from treating Steamer projections
    as observed current-season performance ("has a .385 xwOBA" vs
    "Steamer projects a .385 xwOBA").

    Returns a plain-text string ready for insertion into a DATA BLOCK.
    Empty string if no stats available.
    """
    try:
        from fantasai.models.player import PlayerStats

        rows = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == player_id,
                PlayerStats.season == 2026,
                PlayerStats.week.is_(None),
            )
            .all()
        )
        if not rows:
            return ""

        # Prefer actual over projection; if stat_type given filter to it
        actual = next(
            (r for r in rows if r.data_source == "actual"
             and (stat_type is None or r.stat_type == stat_type)),
            None,
        )
        proj = next(
            (r for r in rows if r.data_source == "projection"
             and (stat_type is None or r.stat_type == stat_type)),
            None,
        )
        row = actual or proj
        if row is None:
            return ""

        is_actual = row.data_source == "actual"
        cnt = row.counting_stats or {}
        rate = row.rate_stats or {}
        adv = row.advanced_stats or {}
        stype = row.stat_type

        parts: list[str] = []

        # ── Label ────────────────────────────────────────────────────────────
        if is_actual:
            if stype == "pitching":
                ip = float(cnt.get("IP") or 0)
                gs = int(float(cnt.get("GS") or 0))
                days = max(0, (date.today() - _SEASON_START_2026).days)
                parts.append(f"[2026 actual — {gs} GS, {ip:.1f} IP, day {days}]")
            else:
                pa = int(float(cnt.get("PA") or cnt.get("AB") or 0))
                g = int(float(cnt.get("G") or 0))
                days = max(0, (date.today() - _SEASON_START_2026).days)
                parts.append(f"[2026 actual — {g} G, {pa} PA, day {days}]")
        else:
            parts.append("[2026 Steamer/consensus projection — full-season]")

        # ── Rate stats ───────────────────────────────────────────────────────
        def _fmt_rate(k: str, decimals: int = 3) -> Optional[str]:
            v = rate.get(k)
            if v is None:
                return None
            try:
                return f"{k}: {float(v):.{decimals}f}"
            except (TypeError, ValueError):
                return None

        def _fmt_adv(k: str, decimals: int = 3, pct: bool = False) -> Optional[str]:
            v = adv.get(k)
            if v is None:
                return None
            try:
                fv = float(v)
                if pct:
                    return f"{k}: {fv:.1f}%"
                return f"{k}: {fv:.{decimals}f}"
            except (TypeError, ValueError):
                return None

        if stype == "pitching":
            for item in [
                _fmt_rate("ERA", 2),
                _fmt_rate("WHIP", 2),
                _fmt_rate("K/9", 2) or _fmt_rate("K9", 2),
                _fmt_rate("BB/9", 2) or _fmt_rate("BB9", 2),
                _fmt_adv("xERA", 2),
                _fmt_adv("xFIP", 2),
                _fmt_adv("SIERA", 2),
                _fmt_adv("K-BB%", 1, pct=True),
                _fmt_adv("CSW%", 1, pct=True),
                _fmt_adv("SwStr%", 1, pct=True),
            ]:
                if item:
                    parts.append(item)
            # Counting stats: label projected clearly
            if is_actual:
                for k in ["IP", "W", "SV", "K", "SO"]:
                    v = cnt.get(k)
                    if v is not None:
                        try:
                            parts.append(f"{k}: {int(float(v))}")
                        except (TypeError, ValueError):
                            pass
            else:
                for k in ["W", "SV", "K", "SO"]:
                    v = cnt.get(k)
                    if v is not None:
                        try:
                            parts.append(f"proj-{k}: {int(float(v))}")
                        except (TypeError, ValueError):
                            pass
        else:
            for item in [
                _fmt_rate("AVG", 3),
                _fmt_rate("OBP", 3),
                _fmt_rate("SLG", 3),
                _fmt_rate("OPS", 3),
                _fmt_adv("xwOBA", 3),
                _fmt_adv("xBA", 3),
                _fmt_adv("xSLG", 3),
                _fmt_adv("Barrel%", 1, pct=True),
                _fmt_adv("HardHit%", 1, pct=True),
                _fmt_rate("BB%", 1, pct=True),
                _fmt_rate("K%", 1, pct=True),
            ]:
                if item:
                    parts.append(item)
            # Counting stats
            for k in ["HR", "R", "RBI", "SB"]:
                v = cnt.get(k)
                if v is not None:
                    try:
                        prefix = "proj-" if not is_actual else ""
                        parts.append(f"{prefix}{k}: {int(float(v))}")
                    except (TypeError, ValueError):
                        pass

        return "  " + "\n  ".join(parts) if parts else ""

    except Exception:
        _log.debug("build_player_stats_block failed for player_id=%d", player_id, exc_info=True)
        return ""


def build_player_injury_note(
    player_id: int,
    db: "Session",
) -> str:
    """Return an injury/risk context string for use in LLM prompts.

    Checks InjuryRecord (current IL status) and Player.risk_flag (chronic
    risk profile). Returns a plain string like:
      "⚠ IL-60 (right elbow surgery, return date unknown). Risk: recent_surgery."
    or empty string if the player is fully healthy with no flags.

    This is pulled directly from the DB — no external RAG required.
    """
    try:
        from fantasai.models.player import InjuryRecord, Player

        player = db.get(Player, player_id)
        if not player:
            return ""

        parts: list[str] = []

        # Current IL status
        injury = player.injury_record
        if injury and injury.status:
            status_label = {
                "il_10": "10-Day IL",
                "il_60": "60-Day IL",
                "day_to_day": "Day-to-day",
                "out_for_season": "Out for season",
            }.get(injury.status, injury.status)

            note = f"⚠ {status_label}"
            if injury.injury_description:
                note += f" ({injury.injury_description})"
            if injury.return_date:
                note += f", expected back {injury.return_date.strftime('%b %-d')}"
            else:
                note += ", return date unknown"
            parts.append(note)

        # Chronic risk flag
        if player.risk_flag:
            risk_label = {
                "fragile": "chronically injury-prone (career IP/PA discount applies)",
                "recent_surgery": "recovering from major surgery (availability discount applies)",
            }.get(player.risk_flag, player.risk_flag)
            if player.risk_note:
                parts.append(f"Risk profile: {risk_label} — {player.risk_note}")
            else:
                parts.append(f"Risk profile: {risk_label}")

        return "\n  ".join(parts) if parts else ""

    except Exception:
        _log.debug("build_player_injury_note failed for player_id=%d", player_id, exc_info=True)
        return ""


def build_pitcher_workload_note(
    player_id: int,
    db: "Session",
    overall_rank: Optional[int] = None,
) -> str:
    """Return IP-discount and upside framing for workload-limited pitchers.

    For pitchers with < 30 projected IP or a risk flag, explains the
    workload context so the LLM can frame the player's value correctly:
    - "Limited to 68 IP by Steamer — discount counting stats accordingly"
    - "Post-surgery ramp — upside scenario: full-season health = top-20 SP"

    Returns empty string for healthy starters with normal workloads,
    or for non-pitchers.
    """
    try:
        from fantasai.models.player import Player, PlayerStats

        player = db.get(Player, player_id)
        if not player or "SP" not in (player.positions or []) and "RP" not in (player.positions or []):
            return ""

        # Grab projected IP from Steamer row
        proj_row = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == player_id,
                PlayerStats.season == 2026,
                PlayerStats.data_source == "projection",
                PlayerStats.stat_type == "pitching",
                PlayerStats.week.is_(None),
            )
            .first()
        )
        actual_row = (
            db.query(PlayerStats)
            .filter(
                PlayerStats.player_id == player_id,
                PlayerStats.season == 2026,
                PlayerStats.data_source == "actual",
                PlayerStats.stat_type == "pitching",
                PlayerStats.week.is_(None),
            )
            .first()
        )

        proj_ip = float((proj_row.counting_stats or {}).get("IP") or 0) if proj_row else 0.0
        actual_ip = float((actual_row.counting_stats or {}).get("IP") or 0) if actual_row else 0.0
        is_sp = "SP" in (player.positions or [])
        normal_ip = 170.0 if is_sp else 62.0
        risk_flag = player.risk_flag

        parts: list[str] = []

        # Flagged as fragile or recent_surgery
        if risk_flag == "fragile":
            parts.append(
                f"WORKLOAD NOTE: Chronic injury history — career average IP meaningfully "
                f"below projection. Steamer projects {proj_ip:.0f} IP but health history "
                f"suggests ~{proj_ip * 0.85:.0f} IP is more realistic. Discount counting "
                f"stats (W, K, SV) accordingly."
            )
        elif risk_flag == "recent_surgery":
            upside_note = ""
            if is_sp and proj_ip < 140 and overall_rank is not None and overall_rank <= 150:
                upside_note = (
                    f" UPSIDE SCENARIO: If fully healthy all season, the underlying "
                    f"stuff projects as a top-{min(overall_rank + 30, 100)} SP."
                )
            parts.append(
                f"WORKLOAD NOTE: Post-surgery — cautious ramp-up expected. "
                f"Steamer projects {proj_ip:.0f} IP vs. {normal_ip:.0f} for a healthy starter. "
                f"Discount counting stats; rate stats (ERA, WHIP, K/9) are unaffected.{upside_note}"
            )
        elif is_sp and 0 < proj_ip < 120:
            # Low-IP SP without a risk flag — swingman, opener, or role uncertainty
            parts.append(
                f"WORKLOAD NOTE: Steamer projects only {proj_ip:.0f} IP — likely a swingman "
                f"or uncertain SP role. Counting stats (W, K, IP) will be limited; "
                f"rate stats reflect real skill if/when they pitch."
            )

        # Also note if they haven't thrown a pitch yet this season
        if actual_ip == 0:
            parts.append(
                "AVAILABILITY NOTE: 0 IP so far this season — has not yet made their 2026 debut."
            )

        return "\n  ".join(parts) if parts else ""

    except Exception:
        _log.debug("build_pitcher_workload_note failed for player_id=%d", player_id, exc_info=True)
        return ""
