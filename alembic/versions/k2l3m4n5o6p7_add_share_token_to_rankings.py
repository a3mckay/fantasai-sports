"""Add share_token to rankings table.

Revision ID: k2l3m4n5o6p7
Revises: j1k2l3m4n5o6
Create Date: 2026-03-29

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "k2l3m4n5o6p7"
down_revision = "j1k2l3m4n5o6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rankings", sa.Column("share_token", sa.String(64), nullable=True))
    op.create_index("ix_rankings_share_token", "rankings", ["share_token"], unique=True)

    # Back-fill existing rows with a unique token each.
    # gen_random_uuid() is built into PostgreSQL 13+ (no pgcrypto needed).
    op.execute("""
        UPDATE rankings
        SET share_token = replace(gen_random_uuid()::text, '-', '')
        WHERE share_token IS NULL
    """)


def downgrade() -> None:
    op.drop_index("ix_rankings_share_token", table_name="rankings")
    op.drop_column("rankings", "share_token")
