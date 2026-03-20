"""add prospect profiles table

Revision ID: e8f9a0b1c2d3
Revises: d7eddb613f65
Create Date: 2026-03-19 12:00:00.000000

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "e8f9a0b1c2d3"
down_revision = "d7eddb613f65"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prospect_profiles",
        sa.Column("player_id", sa.Integer(), sa.ForeignKey("players.player_id"), primary_key=True),
        sa.Column("mlbam_id", sa.Integer(), nullable=True),
        sa.Column("pipeline_rank", sa.Integer(), nullable=True),
        sa.Column("pipeline_grade", sa.Float(), nullable=True),
        sa.Column("ba_grade", sa.Float(), nullable=True),
        sa.Column("fg_grade", sa.Float(), nullable=True),
        sa.Column("stints", sa.JSON(), nullable=True),
        sa.Column("levels_in_season", sa.Integer(), nullable=True),
        sa.Column("highest_level", sa.String(20), nullable=True),
        sa.Column("draft_year", sa.Integer(), nullable=True),
        sa.Column("eta_situation", sa.String(30), nullable=True),
        sa.Column("stat_type", sa.String(20), nullable=False, server_default="batting"),
        sa.Column("pav_score", sa.Float(), nullable=True),
        sa.Column("proxy_mlb_rank", sa.Integer(), nullable=True),
        sa.Column("last_synced", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_prospect_profiles_mlbam_id", "prospect_profiles", ["mlbam_id"])
    op.create_index("ix_prospect_profiles_pav_score", "prospect_profiles", ["pav_score"])


def downgrade() -> None:
    op.drop_index("ix_prospect_profiles_pav_score", table_name="prospect_profiles")
    op.drop_index("ix_prospect_profiles_mlbam_id", table_name="prospect_profiles")
    op.drop_table("prospect_profiles")
