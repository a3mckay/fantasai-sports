"""Reset grades for team-in-blurb and projection-time-period prompt fixes.

Forces re-grading so blurbs now:
- Always write 'Name (TEAM)' on first mention (disambiguates same-name players)
- Always say 'projects for X this season' when citing Steamer stats

Revision ID: g8h9i0j1k2l3
Revises: f7a8b9c0d1e2
Create Date: 2026-03-28
"""
from __future__ import annotations
from alembic import op

revision = 'g8h9i0j1k2l3'
down_revision = 'f7a8b9c0d1e2'
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
