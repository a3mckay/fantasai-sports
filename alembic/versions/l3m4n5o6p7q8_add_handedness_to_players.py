"""Add throws and bats columns to players table.

Revision ID: l3m4n5o6p7q8
Revises: k2l3m4n5o6p7
Create Date: 2026-04-04

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "l3m4n5o6p7q8"
down_revision = "83900d6ce121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # throws: "R" | "L" | "S" (switch) — for pitchers only
    op.add_column("players", sa.Column("throws", sa.String(1), nullable=True))
    # bats: "R" | "L" | "S" — for batters / two-way players
    op.add_column("players", sa.Column("bats", sa.String(1), nullable=True))


def downgrade() -> None:
    op.drop_column("players", "bats")
    op.drop_column("players", "throws")
