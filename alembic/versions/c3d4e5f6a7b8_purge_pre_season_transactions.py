"""Purge pre-season transactions (before March 23, 2026 pick-up period).

Transactions before the league's pick-up start date are keeper/draft-adjacent
moves stored with empty participant data — they produce blank "C+" grade cards
and should not appear in the Move Grades feed.

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-27 16:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    # Delete transactions from before the 2026 regular pick-up period.
    # These are keeper designations and draft-adjacent moves that Yahoo
    # records as "add"/"trade" but with no meaningful participant data.
    result = connection.execute(
        sa.text(
            "DELETE FROM transactions"
            " WHERE yahoo_timestamp < '2026-03-22 00:00:00+00'"
        )
    )
    deleted = result.rowcount
    if deleted:
        import logging
        logging.getLogger(__name__).info(
            "purge_pre_season_transactions: deleted %d pre-season rows", deleted
        )


def downgrade() -> None:
    # Data purge — cannot be reversed
    pass
