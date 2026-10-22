"""Änderungsverlauf der Regeln auf den Firewalls (1.30): letzter beobachteter Stand und Änderungsereignisse.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # A pre-Alembic install is bridged with create_all() first (app/migrate.py), which already made the tables.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("live_rule_states"):
        op.create_table(
            "live_rule_states",
            sa.Column("host_id", UUID, nullable=False),
            sa.Column("rule_uuid", sa.String(64), nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("row", sa.JSON(), nullable=False),
            sa.Column("present", sa.Boolean(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("first_seen_at", TS, nullable=False),
            sa.Column("changed_at", TS, nullable=False),
            sa.Column("last_seen_at", TS, nullable=False),
            sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("host_id", "rule_uuid"),
        )
    if not inspector.has_table("rule_change_events"):
        op.create_table(
            "rule_change_events",
            sa.Column("id", UUID, nullable=False),
            sa.Column("host_id", UUID, nullable=False),
            sa.Column("rule_uuid", sa.String(64), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("observed_at", TS, nullable=False),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("before", sa.JSON(), nullable=True),
            sa.Column("after", sa.JSON(), nullable=True),
            sa.Column("origin", sa.String(16), nullable=False),
            sa.Column("actor", sa.String(150), nullable=True),
            sa.Column("audit_id", UUID, nullable=True),
            sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("host_id", "rule_uuid", "seq", name="uq_rule_change_event"),
        )
        op.create_index("ix_rule_change_events_observed_at", "rule_change_events", ["observed_at"])
        op.create_index("ix_rule_change_events_rule", "rule_change_events", ["host_id", "rule_uuid", "observed_at"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rule_change_events")
    op.execute("DROP TABLE IF EXISTS live_rule_states")
