"""add_team_name_and_roster_names_to_teams

Revision ID: acbc21dafa6f
Revises: f1a2b3c4d5e6
Create Date: 2026-03-23 20:00:23.603699
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'acbc21dafa6f'
down_revision: Union[str, None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('teams', sa.Column('team_name', sa.String(length=200), nullable=True))
    op.add_column('teams', sa.Column('roster_names', sa.JSON(), nullable=True, server_default='[]'))


def downgrade() -> None:
    op.drop_column('teams', 'roster_names')
    op.drop_column('teams', 'team_name')
