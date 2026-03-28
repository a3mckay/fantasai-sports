"""Add bench_player_ids column to teams table.

Tracks which players are in BN (bench) slots so the Matchup Analyzer
can project only the active lineup instead of all rostered players,
preventing inflated weekly stat projections.

Revision ID: i0j1k2l3m4n5
Revises: h9i0j1k2l3m4
Create Date: 2026-03-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'i0j1k2l3m4n5'
down_revision = 'h9i0j1k2l3m4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('teams', sa.Column('bench_player_ids', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('teams', 'bench_player_ids')
