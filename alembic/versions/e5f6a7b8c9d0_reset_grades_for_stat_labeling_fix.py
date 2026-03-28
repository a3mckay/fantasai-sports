"""Reset transaction grades for stat-labeling and prompt improvements.

Clears all grades so they regenerate with:
  - Projection stats clearly labeled as full-season Steamer projections
  - Actual 2026 stats labeled with sample size (G/PA/IP)
  - Ranks described as 'predicted-season-rank' not generic '#N'
  - Early-season small-sample warnings
  - Correct K/9 elite/above-avg thresholds
  - No VERDICT label in output
  - Better prospect language for unknown players

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-03-28
"""
from __future__ import annotations
from alembic import op

revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE transactions
        SET grade_letter = NULL,
            grade_score = NULL,
            grade_rationale = NULL,
            graded_at = NULL,
            card_image_path = NULL
        WHERE grade_letter IS NOT NULL
    """)


def downgrade() -> None:
    pass
