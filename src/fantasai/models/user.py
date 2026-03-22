"""User, YahooConnection, UserSettings, and AnonymousUsage models."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from fantasai.models.base import Base, TimestampMixin


def _new_uuid() -> str:
    return str(uuid.uuid4())


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    firebase_uid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255))
    name: Mapped[Optional[str]] = mapped_column(String(255))
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    managing_style: Mapped[Optional[str]] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False)
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relationships
    yahoo_connection: Mapped[Optional["YahooConnection"]] = relationship(
        "YahooConnection",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    settings: Mapped[Optional["UserSettings"]] = relationship(
        "UserSettings",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id!r} email={self.email!r} role={self.role!r}>"


class YahooConnection(Base):
    __tablename__ = "yahoo_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    yahoo_guid: Mapped[Optional[str]] = mapped_column(String(64))
    league_key: Mapped[Optional[str]] = mapped_column(String(64))
    team_key: Mapped[Optional[str]] = mapped_column(String(64))
    encrypted_access_token: Mapped[Optional[str]] = mapped_column(Text)
    encrypted_refresh_token: Mapped[Optional[str]] = mapped_column(Text)
    token_expiry: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_synced: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship("User", back_populates="yahoo_connection")

    def __repr__(self) -> str:
        return f"<YahooConnection user_id={self.user_id!r} league_key={self.league_key!r}>"


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # notification_prefs: {weekly_digest: bool, waiver_alerts: bool}
    notification_prefs: Mapped[Optional[Any]] = mapped_column(
        Text,  # stored as JSON string; loaded/dumped manually for SQLite compat
        default='{"weekly_digest": true, "waiver_alerts": true}',
    )
    # watchlist: list of player_id strings
    watchlist: Mapped[Optional[Any]] = mapped_column(Text, default="[]")
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship("User", back_populates="settings")

    def __repr__(self) -> str:
        return f"<UserSettings user_id={self.user_id!r}>"


class AnonymousUsage(Base):
    """IP-based rate-limiting for unauthenticated analysis endpoint calls."""
    __tablename__ = "anonymous_usage"
    __table_args__ = (
        UniqueConstraint("ip_hash", "feature", "date", name="uq_anon_usage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ip_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    feature: Mapped[str] = mapped_column(String(64), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    def __repr__(self) -> str:
        return f"<AnonymousUsage ip={self.ip_hash[:8]}... feature={self.feature!r} count={self.count}>"
