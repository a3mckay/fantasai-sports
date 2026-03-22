"""User settings and watchlist routes."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasai.api.deps import get_current_user, get_db
from fantasai.models.user import User, UserSettings

router = APIRouter(prefix="/settings", tags=["settings"])
_log = logging.getLogger(__name__)


class NotificationPrefsUpdate(BaseModel):
    weekly_digest: bool = True
    waiver_alerts: bool = True


def _get_or_create_settings(user: User, db: Session) -> UserSettings:
    s = user.settings
    if s is None:
        s = UserSettings(user_id=user.id)
        db.add(s)
        db.flush()
    return s


def _parse_prefs(settings: UserSettings) -> dict[str, bool]:
    try:
        return json.loads(settings.notification_prefs or "{}")
    except (json.JSONDecodeError, TypeError):
        return {"weekly_digest": True, "waiver_alerts": True}


def _parse_watchlist(settings: UserSettings) -> list[str]:
    try:
        return json.loads(settings.watchlist or "[]")
    except (json.JSONDecodeError, TypeError):
        return []


@router.get("")
def get_settings(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return notification preferences and watchlist."""
    s = _get_or_create_settings(user, db)
    db.commit()
    return {
        "notification_prefs": _parse_prefs(s),
        "watchlist": _parse_watchlist(s),
    }


@router.put("")
def update_settings(
    body: NotificationPrefsUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update notification preferences."""
    s = _get_or_create_settings(user, db)
    s.notification_prefs = json.dumps({"weekly_digest": body.weekly_digest, "waiver_alerts": body.waiver_alerts})
    s.updated_at = datetime.now(tz=timezone.utc)
    db.commit()
    return {"notification_prefs": _parse_prefs(s)}


@router.get("/watchlist")
def get_watchlist(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the user's watchlist as a list of player IDs."""
    s = _get_or_create_settings(user, db)
    db.commit()
    return {"watchlist": _parse_watchlist(s)}


@router.post("/watchlist/{player_id}")
def add_to_watchlist(
    player_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Add a player to the watchlist."""
    s = _get_or_create_settings(user, db)
    watchlist = _parse_watchlist(s)
    if player_id not in watchlist:
        watchlist.append(player_id)
        s.watchlist = json.dumps(watchlist)
        s.updated_at = datetime.now(tz=timezone.utc)
        db.commit()
    return {"watchlist": watchlist}


@router.delete("/watchlist/{player_id}")
def remove_from_watchlist(
    player_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Remove a player from the watchlist."""
    s = _get_or_create_settings(user, db)
    watchlist = _parse_watchlist(s)
    if player_id in watchlist:
        watchlist.remove(player_id)
        s.watchlist = json.dumps(watchlist)
        s.updated_at = datetime.now(tz=timezone.utc)
        db.commit()
    return {"watchlist": watchlist}
