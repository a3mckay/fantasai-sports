"""Add component score fields to rankings and ranking_snapshots.

New fields support the three-component Rest-of-Season ranking formula:
  - statcast_score: Statcast composite z-score component
  - steamer_score:  Steamer projection z-score component
  - accum_score:    Accumulated results z-score component
  - outperformer_flag: 1=Tier1 sustained, 2=Tier2 single-season, 3=Tier3 small-sample
  - percentile_data: JSON dict of {metric: {pct, label, avg, value}} for blurb prompts

ranking_snapshots gains component_scores (JSON) and outperformer_flag.
percentile_data is omitted from snapshots (large, recomputable on demand).

Revision ID: j1k2l3m4n5o6
Revises: i0j1k2l3m4n5
Create Date: 2026-03-29
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa

revision = 'j1k2l3m4n5o6'
down_revision = 'i0j1k2l3m4n5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # rankings table
    with op.batch_alter_table("rankings") as batch_op:
        batch_op.add_column(sa.Column("statcast_score", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("steamer_score",  sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("accum_score",    sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("outperformer_flag", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("percentile_data", sa.JSON(), nullable=True))

    # ranking_snapshots table
    with op.batch_alter_table("ranking_snapshots") as batch_op:
        batch_op.add_column(sa.Column("component_scores",   sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("outperformer_flag",  sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ranking_snapshots") as batch_op:
        batch_op.drop_column("outperformer_flag")
        batch_op.drop_column("component_scores")

    with op.batch_alter_table("rankings") as batch_op:
        batch_op.drop_column("percentile_data")
        batch_op.drop_column("outperformer_flag")
        batch_op.drop_column("accum_score")
        batch_op.drop_column("steamer_score")
        batch_op.drop_column("statcast_score")
