"""Benachrichtigung, wenn eine geplante Wartung nicht ausgeführt werden konnte.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-10
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS "
        "scheduled_maintenance_failed_enabled boolean NOT NULL DEFAULT true"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS scheduled_maintenance_failed_enabled")
