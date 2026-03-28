"""Purge transactions with unknown types (draft picks, commish actions, etc.)

Removes any transaction rows whose transaction_type is not one of the
canonical move types (add, drop, trade).  These were incorrectly backfilled
before the Yahoo type-filter fix was applied.

Revision ID: b2c3d4e5f6a7
Revises: a3f7b2c1d4e5
Create Date: 2026-03-27 15:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a3f7b2c1d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    # Use literal SQL for the IN list — safe since _VALID_TYPES is a hardcoded constant.
    result = connection.execute(
        sa.text(
            "DELETE FROM transactions"
            " WHERE transaction_type NOT IN ('add', 'drop', 'trade')"
        )
    )
    deleted = result.rowcount
    if deleted:
        import logging
        logging.getLogger(__name__).info(
            "purge_non_move_transactions: deleted %d rows with non-move types", deleted
        )


def downgrade() -> None:
    # Data purge — cannot be reversed
    pass
