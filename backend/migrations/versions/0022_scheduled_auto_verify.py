"""Geplante Wartung: automatische Prüfung nach dem Auftrag (auto_verify) und Herkunft (manuell/Rollout).

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-21
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE scheduled_maintenance ADD COLUMN IF NOT EXISTS auto_verify boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE scheduled_maintenance ADD COLUMN IF NOT EXISTS origin varchar(16) NOT NULL DEFAULT 'manual'")


def downgrade() -> None:
    op.execute("ALTER TABLE scheduled_maintenance DROP COLUMN IF EXISTS origin")
    op.execute("ALTER TABLE scheduled_maintenance DROP COLUMN IF EXISTS auto_verify")
