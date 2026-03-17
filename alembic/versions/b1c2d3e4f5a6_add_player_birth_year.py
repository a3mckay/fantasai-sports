"""add_player_birth_year

Revision ID: b1c2d3e4f5a6
Revises: a0b45d6b9db7
Create Date: 2026-03-16 00:00:00.000000

Add birth_year to the players table so keeper-league evaluations can apply
age-based future-value multipliers (younger players are worth more to keep).
Populated by the ingestion pipeline from the pybaseball Age column.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'a0b45d6b9db7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'players',
        sa.Column('birth_year', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('players', 'birth_year')
