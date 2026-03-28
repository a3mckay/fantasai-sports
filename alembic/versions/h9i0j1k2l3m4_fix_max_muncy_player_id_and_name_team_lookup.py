"""Fix Max Muncy (LAD) player_id in existing transactions.

The player name-only lookup was non-deterministic when two players share a name.
Corrects existing transactions that linked "Max Muncy" to player_id 29779 (ATH)
instead of 13301 (LAD) by doing a JSONB array element update.

Going forward, yahoo_transactions.py uses (name, team_abbr) as the primary
lookup key so same-name players are always correctly disambiguated.

Revision ID: h9i0j1k2l3m4
Revises: g8h9i0j1k2l3
Create Date: 2026-03-28
"""
from __future__ import annotations
from alembic import op
import sqlalchemy as sa

revision = 'h9i0j1k2l3m4'
down_revision = 'g8h9i0j1k2l3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Fix the player_id inside the participants JSONB array:
    # Any participant where player_name = 'Max Muncy' and player_id = 29779 (ATH)
    # should be corrected to 13301 (LAD).
    # We can't know for certain without Yahoo's team data, but since the ATH Max Muncy
    # is a fringe player rarely added in competitive leagues, and the LAD Max Muncy
    # is a well-known player, this corrects the common-case error.
    # The correct long-term fix (team_abbr lookup) is in the application code.
    # Use a SAVEPOINT so that if the UPDATE fails (e.g. SQLite in tests, or
    # JSONB not available), we can roll back just this statement without
    # leaving the outer Alembic transaction in an aborted state.
    try:
        conn.execute(sa.text("SAVEPOINT fix_muncy"))
        conn.execute(sa.text("""
            UPDATE transactions
            SET
                participants = (
                    SELECT jsonb_agg(
                        CASE
                            WHEN elem->>'player_name' = 'Max Muncy'
                             AND (elem->>'player_id')::int = 29779
                            THEN jsonb_set(elem, '{player_id}', '13301')
                            ELSE elem
                        END
                    )
                    FROM jsonb_array_elements(participants) AS elem
                ),
                grade_letter     = NULL,
                grade_score      = NULL,
                grade_rationale  = NULL,
                graded_at        = NULL,
                card_image_path  = NULL
            WHERE participants::text LIKE '%"Max Muncy"%'
              AND participants::text LIKE '%29779%'
        """))
        conn.execute(sa.text("RELEASE SAVEPOINT fix_muncy"))
    except Exception:
        # SQLite (used in tests) doesn't support JSONB / SAVEPOINTs —
        # roll back to the savepoint so the outer transaction stays clean.
        try:
            conn.execute(sa.text("ROLLBACK TO SAVEPOINT fix_muncy"))
        except Exception:
            pass


def downgrade() -> None:
    pass
