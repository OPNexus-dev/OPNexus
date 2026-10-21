"""Alarm bei fehlenden API-Rechten und pro Gateway abschaltbarer Gateway-Alarm.

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-21
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("api_rights_enabled", "boolean NOT NULL DEFAULT true"),
    ("gateway_alert_muted", "json NOT NULL DEFAULT '[]'::json"),
]


def upgrade() -> None:
    # IF NOT EXISTS: a pre-Alembic install is bridged with create_all() first, which may already have the columns.
    for name, ddl in _COLUMNS:
        op.execute(f"ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS {name} {ddl}")
    # The list default only fills existing rows; the model supplies it for new ones (and Alembic's schema
    # comparison cannot compare a JSON server default).
    op.execute("ALTER TABLE notification_settings ALTER COLUMN gateway_alert_muted DROP DEFAULT")


def downgrade() -> None:
    for name, _ddl in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE notification_settings DROP COLUMN IF EXISTS {name}")
