"""Grade card image renderer.

Generates a shareable PNG grade card for a graded transaction using Pillow.
Cards are stored in /tmp/grade_cards/ and referenced by Transaction.card_image_path.

Design matches the FantasAI Sports dark theme:
  - Background: #0a0f1a  (deep navy)
  - Card surface: #111827
  - Green accent: #2d8a40
  - Grade colours: green for A/B, amber for C, red for D/F
  - Player headshots fetched from MLB Stats API via mlbam_id
"""
from __future__ import annotations

import logging
import os
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from fantasai.models.transaction import Transaction

_log = logging.getLogger(__name__)

# Output directory (writable on Railway via /tmp)
_CARD_DIR = Path("/tmp/grade_cards")

# Card dimensions
_W, _H = 800, 480

# Colour palette
_BG         = (10, 15, 26)       # #0a0f1a
_CARD_BG    = (17, 24, 39)       # #111827
_BORDER     = (30, 41, 59)       # #1e293b
_GREEN      = (45, 138, 64)      # #2d8a40
_AMBER      = (217, 119, 6)      # #d97706
_RED        = (192, 57, 43)      # #c0392b
_WHITE      = (249, 250, 251)    # #f9fafb
_MUTED      = (156, 163, 175)    # #9ca3af
_DARK_MUTED = (55, 65, 81)       # #374151


def _grade_colour(letter: str) -> tuple:
    if letter.startswith("A") or letter.startswith("B"):
        return _GREEN
    if letter.startswith("C"):
        return _AMBER
    return _RED


def _fetch_headshot(mlbam_id: Optional[int], size: int = 100) -> Optional[object]:
    """Fetch a player headshot from MLB Stats API. Returns a PIL Image or None."""
    if not mlbam_id:
        return None
    try:
        import httpx
        from PIL import Image
        url = (
            f"https://img.mlbstatic.com/mlb-photos/image/upload/"
            f"d_people:generic:headshot:67:current.png/"
            f"w_{size},q_auto:best/v1/people/{mlbam_id}/headshot/67/current"
        )
        resp = httpx.get(url, timeout=8, follow_redirects=True)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGBA")
        img = img.resize((size, size))
        return img
    except Exception:
        _log.debug("_fetch_headshot: failed for mlbam_id=%s", mlbam_id, exc_info=True)
        return None


def _make_circle_mask(size: int):
    """Return a circular mask image."""
    from PIL import Image, ImageDraw
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    return mask


def _load_font(size: int, bold: bool = False):
    """Load a font, falling back gracefully."""
    from PIL import ImageFont
    # Try common system fonts available on Linux/Mac
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_rounded_rect(draw, xy, radius: int, fill):
    """Draw a rounded rectangle."""
    from PIL import ImageDraw
    x1, y1, x2, y2 = xy
    draw.rectangle([x1 + radius, y1, x2 - radius, y2], fill=fill)
    draw.rectangle([x1, y1 + radius, x2, y2 - radius], fill=fill)
    draw.ellipse([x1, y1, x1 + radius * 2, y1 + radius * 2], fill=fill)
    draw.ellipse([x2 - radius * 2, y1, x2, y1 + radius * 2], fill=fill)
    draw.ellipse([x1, y2 - radius * 2, x1 + radius * 2, y2], fill=fill)
    draw.ellipse([x2 - radius * 2, y2 - radius * 2, x2, y2], fill=fill)


def render_blurb_card(
    player_id: int,
    player_name: str,
    team: str,
    positions: list[str],
    overall_rank: int,
    score: float,
    blurb: str,
    share_token: str,
    mlbam_id: Optional[int] = None,
) -> Optional[str]:
    """Render a shareable blurb card PNG for a ranked player.

    Returns the file path or None on failure.
    Card is 800×480, dark theme, with headshot, rank, score, blurb, and branding.
    """
    try:
        from PIL import Image, ImageDraw
        _CARD_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _CARD_DIR / f"blurb_{player_id}_{share_token[:8]}.png"

        img = Image.new("RGB", (_W, _H), _BG)
        draw = ImageDraw.Draw(img)

        # ── Card background ───────────────────────────────────────────────────
        _draw_rounded_rect(draw, (20, 20, _W - 20, _H - 60), 12, _CARD_BG)
        draw.rectangle([20, 20, _W - 20, _H - 60], outline=_BORDER, width=1)

        # ── Headshot (left column) ────────────────────────────────────────────
        headshot_size = 100
        headshot = _fetch_headshot(mlbam_id, size=headshot_size)
        hs_x, hs_y = 40, 36
        if headshot:
            mask = _make_circle_mask(headshot_size)
            hs_rgba = headshot.convert("RGBA")
            img.paste(hs_rgba, (hs_x, hs_y), mask)
        else:
            draw.ellipse([hs_x, hs_y, hs_x + headshot_size, hs_y + headshot_size],
                         fill=_DARK_MUTED)
            font_hs = _load_font(28, bold=True)
            initials = "".join(w[0] for w in player_name.split()[:2]).upper()
            iw = draw.textlength(initials, font=font_hs)
            draw.text((hs_x + headshot_size // 2 - iw // 2, hs_y + 32),
                      initials, font=font_hs, fill=_MUTED)

        # ── Rank badge (top-right of headshot) ───────────────────────────────
        badge_x, badge_y = hs_x + headshot_size - 22, hs_y + headshot_size - 22
        draw.ellipse([badge_x, badge_y, badge_x + 36, badge_y + 36], fill=_GREEN)
        font_badge = _load_font(11, bold=True)
        rank_str = f"#{overall_rank}"
        rw = draw.textlength(rank_str, font=font_badge)
        draw.text((badge_x + 18 - rw // 2, badge_y + 10), rank_str,
                  font=font_badge, fill=_WHITE)

        # ── Player info (right of headshot) ───────────────────────────────────
        info_x = hs_x + headshot_size + 20
        font_name  = _load_font(26, bold=True)
        font_meta  = _load_font(14)
        font_score = _load_font(13)

        draw.text((info_x, 40), player_name, font=font_name, fill=_WHITE)

        pos_str = "/".join(positions[:3]) if positions else "—"
        meta_str = f"{team}  ·  {pos_str}"
        draw.text((info_x, 74), meta_str, font=font_meta, fill=_MUTED)

        score_label = f"FantasAI Score: {score:.2f}"
        draw.text((info_x, 96), score_label, font=font_score, fill=_GREEN)

        # ── Blurb text (main body) ────────────────────────────────────────────
        font_blurb = _load_font(15)
        blurb_x, blurb_y = 40, hs_y + headshot_size + 18
        max_width = _W - 60
        max_chars_per_line = max(40, int(max_width / 8.5))
        words = blurb.split()
        lines: list[str] = []
        current = ""
        for word in words:
            test = f"{current} {word}".strip()
            if draw.textlength(test, font=font_blurb) > max_width:
                if current:
                    lines.append(current)
                current = word
            else:
                current = test
        if current:
            lines.append(current)

        max_lines = 7
        line_h = 22
        for i, line in enumerate(lines[:max_lines]):
            if i == max_lines - 1 and len(lines) > max_lines:
                # Truncate last line with ellipsis
                while draw.textlength(line + "…", font=font_blurb) > max_width and line:
                    line = line.rsplit(" ", 1)[0]
                line += "…"
            draw.text((blurb_x, blurb_y + i * line_h), line, font=font_blurb, fill=_WHITE)

        # ── Branding footer ───────────────────────────────────────────────────
        _draw_branding(draw, img)

        img.save(str(out_path), "PNG", optimize=True)
        return str(out_path)

    except Exception:
        _log.error("render_blurb_card: failed for player_id=%s", player_id, exc_info=True)
        return None


def render_grade_card(txn: "Transaction", db: "Session") -> Optional[str]:
    """Render a grade card PNG for a transaction. Returns the file path or None on failure."""
    try:
        from PIL import Image, ImageDraw
        _CARD_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _CARD_DIR / f"txn_{txn.id}_{txn.share_token[:8]}.png"
        img = Image.new("RGB", (_W, _H), _BG)
        draw = ImageDraw.Draw(img)

        if txn.transaction_type == "trade":
            _draw_trade_card(img, draw, txn, db)
        else:
            _draw_add_drop_card(img, draw, txn, db)

        img.save(str(out_path), "PNG", optimize=True)
        return str(out_path)
    except Exception:
        _log.error("render_grade_card: failed for txn %s", txn.id, exc_info=True)
        return None


def _draw_branding(draw, img):
    """Draw FantasAI Sports branding at the bottom of the card."""
    from PIL import Image
    font_sm = _load_font(14)
    font_brand = _load_font(16, bold=True)

    # Bottom bar
    _draw_rounded_rect(draw, (20, _H - 44, _W - 20, _H - 16), 6, _DARK_MUTED)

    # "FantasAI" in white + "AI" in green + " Sports"
    x = 36
    y = _H - 38
    draw.text((x, y), "Fantas", font=font_brand, fill=_WHITE)
    ai_x = x + draw.textlength("Fantas", font=font_brand)
    draw.text((ai_x, y), "AI", font=font_brand, fill=_GREEN)
    sports_x = ai_x + draw.textlength("AI", font=font_brand)
    draw.text((sports_x, y), " Sports  ·  fantasaisports.com", font=font_sm, fill=_MUTED)


def _draw_add_drop_card(img, draw, txn: "Transaction", db: "Session"):
    """Render a card for an add or drop transaction."""
    from PIL import Image, ImageDraw

    participants = txn.participants or []
    txn_type = txn.transaction_type
    grade_letter = txn.grade_letter or "?"
    grade_col = _grade_colour(grade_letter)
    rationale = txn.grade_rationale or ""

    # Card background
    _draw_rounded_rect(draw, (20, 20, _W - 20, _H - 60), 12, _CARD_BG)

    # Grade circle (right side)
    grade_x, grade_y, grade_r = _W - 100, 80, 64
    draw.ellipse(
        [grade_x - grade_r, grade_y - grade_r, grade_x + grade_r, grade_y + grade_r],
        fill=grade_col,
    )
    font_grade = _load_font(52, bold=True)
    font_grade_sm = _load_font(36, bold=True)
    use_font = font_grade if len(grade_letter) == 1 else font_grade_sm
    bbox = draw.textbbox((0, 0), grade_letter, font=use_font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((grade_x - tw // 2, grade_y - th // 2 - 2), grade_letter, font=use_font, fill=_WHITE)

    # Transaction type label
    type_label = txn_type.upper()
    label_col = _GREEN if txn_type == "add" else _RED
    font_label = _load_font(13, bold=True)
    draw.text((grade_x - draw.textlength(type_label, font=font_label) // 2, grade_y + grade_r + 10), type_label, font=font_label, fill=label_col)

    # Left side — player info
    left_x = 44
    y_cursor = 40

    font_mgr = _load_font(13)
    font_name = _load_font(28, bold=True)
    font_sub = _load_font(15)
    font_body = _load_font(14)

    # Determine primary player and manager
    primary = None
    for p in participants:
        if txn_type == "add" and p.get("action") == "add":
            primary = p
            break
        elif txn_type == "drop":
            primary = p
            break
    if not primary and participants:
        primary = participants[0]

    if primary:
        # Manager name
        mgr_name = primary.get("manager_name", "")
        if mgr_name:
            draw.text((left_x, y_cursor), mgr_name.upper(), font=font_mgr, fill=_MUTED)
            y_cursor += 22

        # Player headshot
        mlbam_id = _get_mlbam_id(primary.get("player_id"), db)
        headshot = _fetch_headshot(mlbam_id, size=80) if mlbam_id else None
        if headshot:
            mask = _make_circle_mask(80)
            headshot_rgb = headshot.convert("RGB")
            img.paste(headshot_rgb, (left_x, y_cursor), mask)
            name_x = left_x + 96
        else:
            # Placeholder circle
            draw.ellipse([left_x, y_cursor, left_x + 80, y_cursor + 80], fill=_DARK_MUTED)
            name_x = left_x + 96

        name_y = y_cursor + 8
        player_name = primary.get("player_name", "Unknown Player")
        draw.text((name_x, name_y), player_name, font=font_name, fill=_WHITE)

        team_pos = _get_team_pos(primary.get("player_id"), db)
        if team_pos:
            draw.text((name_x, name_y + 38), team_pos, font=font_sub, fill=_GREEN)

        y_cursor += 96

    # Divider line
    draw.line([(left_x, y_cursor), (_W - 120, y_cursor)], fill=_BORDER, width=1)
    y_cursor += 14

    # Rationale text (word-wrapped)
    if rationale:
        font_body = _load_font(15)
        _draw_wrapped_text(draw, rationale, left_x, y_cursor, _W - 140, font_body, _WHITE, line_height=22)

    _draw_branding(draw, img)


def _draw_trade_card(img, draw, txn: "Transaction", db: "Session"):
    """Render a combined card for a trade transaction."""
    from PIL import Image

    participants = txn.participants or []
    grade_letter = txn.grade_letter or "?"
    grade_col = _grade_colour(grade_letter)
    rationale = txn.grade_rationale or ""

    # Card background
    _draw_rounded_rect(draw, (20, 20, _W - 20, _H - 60), 12, _CARD_BG)

    font_title = _load_font(18, bold=True)
    font_mgr = _load_font(14, bold=True)
    font_player = _load_font(13)
    font_grade_label = _load_font(11)
    font_body = _load_font(14)

    # Header
    draw.text((40, 32), "TRADE GRADE", font=font_title, fill=_MUTED)

    # Two columns for two sides
    col_w = (_W - 60) // 2
    for i, side in enumerate(participants[:2]):
        col_x = 30 + i * (col_w + 4)
        y = 65

        side_grade = side.get("_grade_letter", "?")
        side_col = _grade_colour(side_grade)

        # Side grade badge
        badge_r = 28
        bx = col_x + col_w - badge_r - 8
        by = y + badge_r
        draw.ellipse([bx - badge_r, by - badge_r, bx + badge_r, by + badge_r], fill=side_col)
        font_badge = _load_font(26, bold=True) if len(side_grade) == 1 else _load_font(20, bold=True)
        bbox = draw.textbbox((0, 0), side_grade, font=font_badge)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        draw.text((bx - tw // 2, by - th // 2 - 1), side_grade, font=font_badge, fill=_WHITE)

        # Manager name
        mgr = side.get("manager_name", f"Team {i+1}")
        draw.text((col_x, y), mgr, font=font_mgr, fill=_WHITE)
        y += 24

        # Players received
        draw.text((col_x, y), "RECEIVES", font=font_grade_label, fill=_GREEN)
        y += 16
        for p in side.get("players_added", [])[:3]:
            draw.text((col_x + 8, y), f"+ {p.get('player_name', '?')}", font=font_player, fill=_WHITE)
            y += 18

        y += 4
        draw.text((col_x, y), "GIVES UP", font=font_grade_label, fill=_RED)
        y += 16
        for p in side.get("players_dropped", [])[:3]:
            draw.text((col_x + 8, y), f"- {p.get('player_name', '?')}", font=font_player, fill=_MUTED)
            y += 18

    # Divider
    mid_y = _H - 130
    draw.line([(30, mid_y), (_W - 30, mid_y)], fill=_BORDER, width=1)

    # Rationale
    if rationale:
        _draw_wrapped_text(draw, rationale, 40, mid_y + 12, _W - 80, font_body, _WHITE, line_height=22)

    _draw_branding(draw, img)


def _draw_trade_side_card(img, draw, txn: "Transaction", side_idx: int, db: "Session"):
    """Render one side of a trade as a full-width grade card."""
    participants = txn.participants or []
    if side_idx >= len(participants):
        return

    side = participants[side_idx]
    other = participants[1 - side_idx] if len(participants) > 1 else {}

    side_grade = side.get("_grade_letter", "?")
    grade_col = _grade_colour(side_grade)
    rationale = side.get("_grade_rationale") or txn.grade_rationale or ""

    _draw_rounded_rect(draw, (20, 20, _W - 20, _H - 60), 12, _CARD_BG)

    font_title = _load_font(16, bold=True)
    font_mgr = _load_font(15, bold=True)
    font_player = _load_font(13)
    font_label = _load_font(11)
    font_body = _load_font(13)

    # Header: "TRADE GRADE"
    draw.text((40, 30), "TRADE GRADE", font=font_title, fill=_MUTED)

    # Grade badge (top-right)
    badge_r = 32
    bx = _W - 60
    by = 60
    draw.ellipse([bx - badge_r, by - badge_r, bx + badge_r, by + badge_r], fill=grade_col)
    font_badge = _load_font(28, bold=True) if len(side_grade) == 1 else _load_font(22, bold=True)
    bbox = draw.textbbox((0, 0), side_grade, font=font_badge)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((bx - tw // 2, by - th // 2 - 1), side_grade, font=font_badge, fill=_WHITE)

    mgr = side.get("manager_name", f"Team {side_idx + 1}")
    other_mgr = other.get("manager_name", "other team")
    y = 56

    # Manager name
    draw.text((40, y), mgr, font=font_mgr, fill=_WHITE)
    y += 26

    # Players received
    draw.text((40, y), "RECEIVES", font=font_label, fill=_GREEN)
    y += 16
    for p in side.get("players_added", [])[:4]:
        pname = p.get("player_name", "?")
        mlbam = _get_mlbam_id(p.get("player_id"), db)
        headshot = _fetch_headshot(mlbam, size=28) if mlbam else None
        if headshot:
            try:
                from PIL import Image as _PILImage
                mask = _make_circle_mask(28)
                img.paste(headshot, (40, y - 2), mask)
                draw.text((76, y + 2), f"+ {pname}", font=font_player, fill=_WHITE)
            except Exception:
                draw.text((48, y + 2), f"+ {pname}", font=font_player, fill=_WHITE)
        else:
            draw.text((48, y + 2), f"+ {pname}", font=font_player, fill=_WHITE)
        team_pos = _get_team_pos(p.get("player_id"), db)
        if team_pos:
            draw.text((48 + draw.textlength(f"+ {pname}", font=font_player) + 8, y + 4),
                      team_pos, font=_load_font(11), fill=_MUTED)
        y += 22

    y += 6
    draw.text((40, y), "GIVES UP", font=font_label, fill=_RED)
    y += 16
    for p in side.get("players_dropped", [])[:4]:
        pname = p.get("player_name", "?")
        draw.text((48, y + 2), f"- {pname}", font=font_player, fill=_MUTED)
        team_pos = _get_team_pos(p.get("player_id"), db)
        if team_pos:
            draw.text((48 + draw.textlength(f"- {pname}", font=font_player) + 8, y + 4),
                      team_pos, font=_load_font(11), fill=_MUTED)
        y += 22

    # Divider before rationale
    div_y = max(y + 8, _H - 145)
    draw.line([(30, div_y), (_W - 30, div_y)], fill=_BORDER, width=1)

    if rationale:
        _draw_wrapped_text(draw, rationale, 40, div_y + 10, _W - 80, font_body, _WHITE, line_height=20)

    _draw_branding(draw, img)


def render_trade_side_cards(txn: "Transaction", db: "Session") -> list[str]:
    """Render one grade card per trade side. Returns list of file paths (may be partial on error)."""
    # Load identity attrs before any DB operations (avoids autoflush on lazy-load
    # if the session is in a dirty/failed state at call time).
    try:
        txn_id = txn.__dict__.get("id") or txn.id
        txn_token = (txn.__dict__.get("share_token") or txn.share_token)[:8]
        participants = txn.__dict__.get("participants") or txn.participants or []
    except Exception as exc:
        _log.error("render_trade_side_cards: could not read txn attrs: %s", exc)
        return []

    try:
        from PIL import Image, ImageDraw
        _CARD_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        _log.error("render_trade_side_cards: PIL setup failed for txn %s", txn_id, exc_info=True)
        return []

    paths: list[str] = []
    for side_idx in range(min(2, len(participants))):
        try:
            from PIL import Image, ImageDraw
            out_path = _CARD_DIR / f"txn_{txn_id}_{txn_token}_side{side_idx}.png"
            img = Image.new("RGB", (_W, _H), _BG)
            draw = ImageDraw.Draw(img)
            _draw_trade_side_card(img, draw, txn, side_idx, db)
            img.save(str(out_path), "PNG", optimize=True)
            paths.append(str(out_path))
        except Exception:
            _log.error(
                "render_trade_side_cards: failed for txn %s side %d", txn_id, side_idx, exc_info=True
            )
    return paths


def _draw_wrapped_text(draw, text: str, x: int, y: int, max_w: int, font, fill, line_height: int = 20):
    """Simple word-wrap text drawing."""
    words = text.split()
    line = ""
    for word in words:
        test = f"{line} {word}".strip()
        if draw.textlength(test, font=font) <= max_w:
            line = test
        else:
            if line:
                draw.text((x, y), line, font=font, fill=fill)
                y += line_height
            line = word
    if line:
        draw.text((x, y), line, font=font, fill=fill)


def _get_mlbam_id(player_id: Optional[int], db: "Session") -> Optional[int]:
    if not player_id:
        return None
    try:
        from fantasai.models.player import Player
        p = db.get(Player, player_id)
        return p.mlbam_id if p else None
    except Exception:
        return None


def _get_team_pos(player_id: Optional[int], db: "Session") -> Optional[str]:
    if not player_id:
        return None
    try:
        from fantasai.models.player import Player
        p = db.get(Player, player_id)
        if not p:
            return None
        pos = "/".join(p.positions[:2]) if p.positions else ""
        return f"{p.team}  ·  {pos}" if pos else p.team
    except Exception:
        return None
