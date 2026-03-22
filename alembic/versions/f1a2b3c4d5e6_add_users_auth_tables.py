"""Add users, yahoo_connections, user_settings, anonymous_usage tables.
Add owner_user_id FK to leagues and teams.

Revision ID: f1a2b3c4d5e6
Revises: e8f9a0b1c2d3
Create Date: 2026-03-22 00:00:00.000000
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "f1a2b3c4d5e6"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── users ────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("firebase_uid", sa.String(128), nullable=False),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.Column("managing_style", sa.Text(), nullable=True),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("onboarding_complete", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_users_firebase_uid", "users", ["firebase_uid"], unique=True)

    # ── yahoo_connections ────────────────────────────────────────────────────
    op.create_table(
        "yahoo_connections",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("yahoo_guid", sa.String(64), nullable=True),
        sa.Column("league_key", sa.String(64), nullable=True),
        sa.Column("team_key", sa.String(64), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced", sa.DateTime(timezone=True), nullable=True),
    )

    # ── user_settings ────────────────────────────────────────────────────────
    op.create_table(
        "user_settings",
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "notification_prefs",
            sa.Text(),
            nullable=True,
            server_default='{"weekly_digest": true, "waiver_alerts": true}',
        ),
        sa.Column("watchlist", sa.Text(), nullable=True, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── anonymous_usage ──────────────────────────────────────────────────────
    op.create_table(
        "anonymous_usage",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("ip_hash", sa.String(64), nullable=False),
        sa.Column("feature", sa.String(64), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("ip_hash", "feature", "date", name="uq_anon_usage"),
    )
    op.create_index("ix_anon_usage_ip_hash", "anonymous_usage", ["ip_hash"])

    # ── FK additions to existing tables ─────────────────────────────────────
    op.add_column(
        "leagues",
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "teams",
        sa.Column(
            "owner_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("teams", "owner_user_id")
    op.drop_column("leagues", "owner_user_id")
    op.drop_index("ix_anon_usage_ip_hash", table_name="anonymous_usage")
    op.drop_table("anonymous_usage")
    op.drop_table("user_settings")
    op.drop_table("yahoo_connections")
    op.drop_index("ix_users_firebase_uid", table_name="users")
    op.drop_table("users")
