"""Reset all transaction grades for re-grading with improved prompt logic.

Clears grade_letter, grade_score, grade_rationale, graded_at, and
card_image_path so the next poll cycle regenerates every grade using:
  - Live DB player facts (correct team, position, current stats)
  - Rest-of-season ranking from RankingSnapshot (not week rank)
  - Correct league format (H2H categories, not "points league")
  - No stale injury history from training data

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-03-27 17:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    result = connection.execute(
        sa.text(
            "UPDATE transactions SET"
            "  grade_letter = NULL,"
            "  grade_score = NULL,"
            "  grade_rationale = NULL,"
            "  graded_at = NULL,"
            "  card_image_path = NULL"
            " WHERE grade_letter IS NOT NULL"
        )
    )
    import logging
    logging.getLogger(__name__).info(
        "reset_transaction_grades: cleared grades on %d transactions", result.rowcount
    )


def downgrade() -> None:
    # Grades will be regenerated automatically — downgrade is a no-op
    pass
