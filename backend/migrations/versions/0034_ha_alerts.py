"""HA H2c: Zeitstempel in ha_state (letzter Abgleich, Standby zuletzt gesehen) und der Schalter für die HA-Alarme.

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-25
"""
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ha_state ADD COLUMN IF NOT EXISTS last_sync_at timestamptz")
    op.execute("ALTER TABLE ha_state ADD COLUMN IF NOT EXISTS standby_seen_at timestamptz")
    op.execute("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS ha_alerts_enabled boolean NOT NULL DEFAULT true")


def downgrade() -> None:
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS ha_alerts_enabled")
    op.execute("ALTER TABLE ha_state DROP COLUMN IF EXISTS standby_seen_at")
    op.execute("ALTER TABLE ha_state DROP COLUMN IF EXISTS last_sync_at")
