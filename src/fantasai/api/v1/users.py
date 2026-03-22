"""Admin user management routes."""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from fantasai.api.deps import get_db, require_admin
from fantasai.models.user import User

router = APIRouter(prefix="/users", tags=["admin"])
_log = logging.getLogger(__name__)


class RoleUpdate(BaseModel):
    role: str  # "user" | "admin"


def _user_detail(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "firebase_uid": user.firebase_uid,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "onboarding_complete": user.onboarding_complete,
        "date_of_birth": user.date_of_birth.isoformat() if user.date_of_birth else None,
        "managing_style": user.managing_style,
        "yahoo_connected": user.yahoo_connection is not None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


@router.get("")
def list_users(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    search: Optional[str] = Query(None),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List all users (paginated). Admin only."""
    q = db.query(User)
    if search:
        like = f"%{search}%"
        q = q.filter(
            User.email.ilike(like) | User.name.ilike(like)
        )
    total = q.count()
    users = q.order_by(User.created_at.desc()).offset((page - 1) * per_page).limit(per_page).all()
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "users": [_user_detail(u) for u in users],
    }


@router.get("/{user_id}")
def get_user(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Get a single user by ID. Admin only."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_detail(user)


@router.delete("/{user_id}")
def delete_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Delete a user and all associated data. Admin only."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    _log.info("Admin %s deleted user %s (%s)", admin.email, user_id, user.email)
    return {"status": "deleted", "user_id": user_id}


@router.put("/{user_id}/role")
def set_role(
    user_id: str,
    body: RoleUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Update a user's role. Admin only."""
    if body.role not in ("user", "admin"):
        raise HTTPException(status_code=400, detail="role must be 'user' or 'admin'")
    if user_id == admin.id and body.role != "admin":
        raise HTTPException(status_code=400, detail="Cannot remove your own admin role")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.role = body.role
    db.commit()
    _log.info("Admin %s set user %s role to %s", admin.email, user_id, body.role)
    return _user_detail(user)
