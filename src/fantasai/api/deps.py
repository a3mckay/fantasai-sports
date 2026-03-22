"""FastAPI dependency functions: DB session, auth, rate limiting."""
from __future__ import annotations

import hashlib
import logging
from datetime import date
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

# Single source of truth for get_db is fantasai.database
from fantasai.database import get_db  # noqa: F401

_log = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Auth dependencies
# ---------------------------------------------------------------------------


def _extract_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[str]:
    """Return the raw Bearer token string, or None if not present."""
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return None


def get_optional_user(
    token: Optional[str] = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> Optional["User"]:  # type: ignore[name-defined]  # noqa: F821
    """Return the authenticated User if a valid token is provided, else None."""
    if not token:
        return None
    try:
        from fantasai.models.user import User
        from fantasai.services.firebase_auth import verify_firebase_token

        claims = verify_firebase_token(token)
        firebase_uid = claims["sub"]
        return db.query(User).filter(User.firebase_uid == firebase_uid).first()
    except Exception:
        _log.debug("Token verification failed", exc_info=True)
        return None


def get_current_user(
    token: Optional[str] = Depends(_extract_token),
    db: Session = Depends(get_db),
) -> "User":  # type: ignore[name-defined]  # noqa: F821
    """Return the authenticated User. Raises 401 if not authenticated."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        from fantasai.models.user import User
        from fantasai.services.firebase_auth import verify_firebase_token

        claims = verify_firebase_token(token)
        firebase_uid = claims["sub"]
        user = db.query(User).filter(User.firebase_uid == firebase_uid).first()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User not found — please complete sign-up",
            )
        return user
    except HTTPException:
        raise
    except Exception as exc:
        _log.debug("Token verification failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_admin(
    user: "User" = Depends(get_current_user),  # type: ignore[name-defined]  # noqa: F821
) -> "User":  # type: ignore[name-defined]  # noqa: F821
    """Require the authenticated user to have the 'admin' role."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


# ---------------------------------------------------------------------------
# IP-based rate limiting for anonymous users
# ---------------------------------------------------------------------------

_DAILY_LIMIT = 1  # free uses per feature per IP per day


def _get_ip(request: Request) -> str:
    """Extract the client IP, respecting Railway's X-Forwarded-For header."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(ip.encode()).hexdigest()


def check_rate_limit(feature: str):
    """Return a FastAPI dependency that rate-limits anonymous users for `feature`."""

    def _dependency(
        request: Request,
        user: Optional["User"] = Depends(get_optional_user),  # type: ignore[name-defined]  # noqa: F821
        db: Session = Depends(get_db),
    ) -> None:
        # Authenticated users have no limit
        if user is not None:
            return

        from fantasai.models.user import AnonymousUsage

        ip_hash = _hash_ip(_get_ip(request))
        today = date.today()

        # Fetch existing usage
        usage = (
            db.query(AnonymousUsage)
            .filter(
                AnonymousUsage.ip_hash == ip_hash,
                AnonymousUsage.feature == feature,
                AnonymousUsage.date == today,
            )
            .first()
        )

        if usage and usage.count >= _DAILY_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "requires_auth": True,
                    "message": f"You've used your free {feature} for today. Create an account to continue.",
                    "feature": feature,
                },
            )

        # Increment or insert
        if usage:
            usage.count += 1
        else:
            db.add(AnonymousUsage(ip_hash=ip_hash, feature=feature, date=today, count=1))
        try:
            db.commit()
        except Exception:
            db.rollback()
            _log.warning("Failed to record anonymous usage for %s", feature, exc_info=True)

    return _dependency
