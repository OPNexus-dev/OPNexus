"""Einrichtungsassistent: Merker, dass ein Nutzer ihn abgeschlossen oder übersprungen hat.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-10
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE user_preferences ADD COLUMN IF NOT EXISTS setup_done boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE user_preferences DROP COLUMN IF EXISTS setup_done")
