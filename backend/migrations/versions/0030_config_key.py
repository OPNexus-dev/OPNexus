"""Stabile Schlüssel (config_key) für Regeln und NAT-Objekte (1.32, Konfiguration als Code).

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-23
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_TABLES = (
    ("firewall_rules", "r"),
    ("port_forward_rules", "pf"),
    ("one_to_one_rules", "nat1"),
    ("outbound_nat_rules", "out"),
    ("policy_hierarchy_rules", "ph"),
)


def upgrade() -> None:
    # IF NOT EXISTS / DO-Block: ein Altbestand ohne Alembic wird zuerst per create_all() angehoben und hat Spalte samt
    # Constraint dann schon.
    for table, prefix in _TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS config_key varchar(64)")
        op.execute(
            f"UPDATE {table} SET config_key = '{prefix}-' || substr(replace(id::text, '-', ''), 1, 12) "
            "WHERE config_key IS NULL"
        )
        op.execute(f"ALTER TABLE {table} ALTER COLUMN config_key SET NOT NULL")
        op.execute(
            f"""DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{table}_config_key_key') THEN
                    ALTER TABLE {table} ADD CONSTRAINT {table}_config_key_key UNIQUE (config_key);
                END IF;
            END $$"""
        )


def downgrade() -> None:
    for table, _prefix in _TABLES:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_config_key_key")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS config_key")
