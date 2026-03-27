"""add matchup_analyses table and transactions.is_backfill column

Revision ID: a3f7b2c1d4e5
Revises: 5ebf874800fb
Create Date: 2026-03-27 14:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f7b2c1d4e5'
down_revision: Union[str, None] = '5ebf874800fb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add is_backfill to transactions (default False for existing rows)
    op.add_column(
        'transactions',
        sa.Column('is_backfill', sa.Boolean(), nullable=False, server_default='false'),
    )

    # Create matchup_analyses table
    op.create_table(
        'matchup_analyses',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('league_id', sa.String(length=100), nullable=False),
        sa.Column('season', sa.Integer(), nullable=False),
        sa.Column('week', sa.Integer(), nullable=False),
        sa.Column('team1_key', sa.String(length=100), nullable=False),
        sa.Column('team2_key', sa.String(length=100), nullable=False),
        sa.Column('team1_name', sa.String(length=200), nullable=False),
        sa.Column('team2_name', sa.String(length=200), nullable=False),
        sa.Column('manager1_name', sa.String(length=200), nullable=True),
        sa.Column('manager2_name', sa.String(length=200), nullable=True),
        sa.Column('category_projections', sa.JSON(), nullable=False),
        sa.Column('live_stats', sa.JSON(), nullable=True),
        sa.Column('narrative', sa.Text(), nullable=True),
        sa.Column('suggestions', sa.JSON(), nullable=False),
        sa.Column('generated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['league_id'], ['leagues.league_id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_matchup_league_week',
        'matchup_analyses',
        ['league_id', 'season', 'week'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_matchup_league_week', table_name='matchup_analyses')
    op.drop_table('matchup_analyses')
    op.drop_column('transactions', 'is_backfill')
