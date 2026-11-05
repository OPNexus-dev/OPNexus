"""Reihenfolge-Änderungen von Regeln (Verschieben auf Probe mit automatischer Rücknahme).

Revision ID: 0036
Revises: 0035
Create Date: 2026-11-05
"""
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS rule_order_changes (
            id uuid PRIMARY KEY,
            host_id uuid NOT NULL REFERENCES opnsense_hosts(id) ON DELETE CASCADE,
            rule_uuid varchar(64) NOT NULL,
            description varchar(255) NOT NULL,
            prio_group varchar(32) NOT NULL,
            anchor_next varchar(64),
            anchor_prev varchar(64),
            target_uuid varchar(64),
            status varchar(16) NOT NULL,
            message varchar(500),
            actor varchar(150) NOT NULL,
            created_at timestamptz NOT NULL,
            revert_deadline timestamptz,
            finished_at timestamptz
        )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_rule_order_changes_host_status ON rule_order_changes (host_id, status)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rule_order_changes")
