"""Benachrichtigung bei kritischen/hohen Sicherheitsbefunden (standardmäßig aus).

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-20
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS compliance_alerts_enabled boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS compliance_alerts_enabled")
