"""Add lookback grade columns to transactions table.

Revision ID: f7a8b9c0d1e2
Revises: e5f6a7b8c9d0
Create Date: 2026-03-28
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f7a8b9c0d1e2'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('transactions', sa.Column('lookback_grade_letter', sa.String(3), nullable=True))
    op.add_column('transactions', sa.Column('lookback_grade_score', sa.Float, nullable=True))
    op.add_column('transactions', sa.Column('lookback_grade_rationale', sa.Text, nullable=True))
    op.add_column('transactions', sa.Column('lookback_graded_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('transactions', 'lookback_graded_at')
    op.drop_column('transactions', 'lookback_grade_rationale')
    op.drop_column('transactions', 'lookback_grade_score')
    op.drop_column('transactions', 'lookback_grade_letter')
