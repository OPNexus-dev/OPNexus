"""Benachrichtigung bei Netzwerk-Abweichungen zwischen den Knoten eines Clusters.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS network_drift_enabled boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS network_drift_enabled")
