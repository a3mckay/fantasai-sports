"""add il and injury fields to teams

Revision ID: 1dfbb6e08060
Revises: f69ae681ef16
Create Date: 2026-03-26 12:34:40.773036
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1dfbb6e08060'
down_revision: Union[str, None] = 'f69ae681ef16'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('teams', sa.Column('il_player_ids', sa.JSON(), nullable=True))
    op.add_column('teams', sa.Column('injured_player_statuses', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('teams', 'injured_player_statuses')
    op.drop_column('teams', 'il_player_ids')
