"""Einstellungen im Webinterface statt in der .env (runtime_settings) und Plattenalarm des OPNexus-Servers.

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-24
"""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: a pre-Alembic install is bridged with create_all() first, which may already have created these.
    op.execute(
        "CREATE TABLE IF NOT EXISTS runtime_settings ("
        " key varchar(100) PRIMARY KEY,"
        " value json,"
        " updated_at timestamptz NOT NULL DEFAULT now(),"
        " updated_by varchar(150))"
    )
    op.execute("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS server_disk_enabled boolean NOT NULL DEFAULT true")
    op.execute("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS server_disk_threshold_pct integer NOT NULL DEFAULT 90")


def downgrade() -> None:
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS server_disk_threshold_pct")
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS server_disk_enabled")
    op.execute("DROP TABLE IF EXISTS runtime_settings")
