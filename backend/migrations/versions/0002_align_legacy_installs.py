"""Gleicht Altinstallationen (vor Alembic) an das kanonische Schema an.

Die manuellen Patches aus app/legacy_schema_patches.py haben Spalten ohne
Server-Default und ohne Index angelegt, die Modelle verlangen beides. Auf
frischen Installationen hat 0001 das bereits; hier ist alles idempotent und
dort ein No-op.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE log_settings ALTER COLUMN max_size_gb SET DEFAULT 10")
    for column in ("carp_health_enabled", "nat_drift_enabled", "carp_auto_revert_failed_enabled"):
        op.execute(f"ALTER TABLE notification_settings ALTER COLUMN {column} SET DEFAULT true")
    op.execute("ALTER TABLE user_preferences ALTER COLUMN language SET DEFAULT 'de'")
    for column in ("cert_refs", "ca_refs"):
        op.execute(f"ALTER TABLE openvpn_instances ALTER COLUMN {column} SET DEFAULT '{{}}'::json")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_opnsense_hosts_cluster_name "
        "ON opnsense_hosts (cluster_name)"
    )


def downgrade() -> None:
    # Reine Angleichung ans kanonische Schema — nichts, was man zurückrollen wollte.
    pass
