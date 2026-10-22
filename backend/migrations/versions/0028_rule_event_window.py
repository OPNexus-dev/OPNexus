"""Änderungsverlauf: Fensteranfang je Ereignis für die Herkunftsbestimmung (1.30).

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-22
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: a pre-Alembic install is bridged with create_all() first, which may already have the column.
    op.execute("ALTER TABLE rule_change_events ADD COLUMN IF NOT EXISTS window_start timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE rule_change_events DROP COLUMN IF EXISTS window_start")
