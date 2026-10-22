"""Regel-Bereinigung (1.31, B4 Teil 2): Operationen, Einträge, Einstellungen.

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)

# Must equal OPEN_ITEM_STATES in app/models/rule_cleanup.py (the schema comparison of the tests notices a drift).
_OPEN_STATES = ("queued", "disabling", "disabled", "reverting", "deleting", "restoring", "unknown", "revert_failed")

_SETTINGS = [
    ("cleanup_max_rules", "integer NOT NULL DEFAULT 10"),
    ("cleanup_delete_min_days", "integer NOT NULL DEFAULT 7"),
    ("cleanup_management_sources", "json NOT NULL DEFAULT '[]'::json"),
    ("cleanup_management_ports", "json NOT NULL DEFAULT '[]'::json"),
]


def upgrade() -> None:
    # A pre-Alembic install is bridged with create_all() first (app/migrate.py), which may already have made all of it.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("rule_cleanup_operations"):
        op.create_table(
            "rule_cleanup_operations",
            sa.Column("id", UUID, nullable=False),
            sa.Column("host_id", UUID, nullable=False),
            sa.Column("cluster_name", sa.String(120), nullable=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("actor", sa.String(150), nullable=False),
            sa.Column("actor_id", UUID, nullable=True),
            sa.Column("created_at", TS, nullable=False),
            sa.Column("expires_at", TS, nullable=False),
            sa.Column("started_at", TS, nullable=True),
            sa.Column("heartbeat_at", TS, nullable=True),
            sa.Column("revert_deadline", TS, nullable=True),
            sa.Column("confirmed_at", TS, nullable=True),
            sa.Column("finished_at", TS, nullable=True),
            sa.Column("message", sa.String(1000), nullable=True),
            sa.Column("preview_fingerprint", sa.String(64), nullable=False),
            sa.Column("options", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_rule_cleanup_operations_host", "rule_cleanup_operations", ["host_id", "created_at"])
        op.create_index("ix_rule_cleanup_operations_status", "rule_cleanup_operations", ["status"])
    if not inspector.has_table("rule_cleanup_items"):
        op.create_table(
            "rule_cleanup_items",
            sa.Column("id", UUID, nullable=False),
            sa.Column("operation_id", UUID, nullable=False),
            sa.Column("host_id", UUID, nullable=False),
            sa.Column("rule_uuid", sa.String(64), nullable=False),
            sa.Column("seq", sa.Integer(), nullable=False),
            sa.Column("carp_role", sa.String(16), nullable=True),
            sa.Column("state", sa.String(16), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("categories", sa.JSON(), nullable=False),
            sa.Column("confidence", sa.String(16), nullable=True),
            sa.Column("row_before", sa.JSON(), nullable=True),
            sa.Column("fp_before", sa.String(64), nullable=True),
            sa.Column("cover_uuid", sa.String(64), nullable=True),
            sa.Column("cover_fp", sa.String(64), nullable=True),
            sa.Column("snapshot", sa.JSON(), nullable=True),
            sa.Column("anchor_next_uuid", sa.String(64), nullable=True),
            sa.Column("anchor_prev_uuid", sa.String(64), nullable=True),
            sa.Column("prio_group", sa.String(32), nullable=True),
            sa.Column("source_item_id", UUID, nullable=True),
            sa.Column("restored_uuid", sa.String(64), nullable=True),
            sa.Column("changed_at", TS, nullable=True),
            sa.Column("message", sa.String(1000), nullable=True),
            sa.ForeignKeyConstraint(["operation_id"], ["rule_cleanup_operations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_rule_cleanup_items_operation", "rule_cleanup_items", ["operation_id"])
        op.create_index(
            "uq_cleanup_rule_open", "rule_cleanup_items", ["host_id", "rule_uuid"], unique=True,
            postgresql_where=sa.text("state IN (" + ", ".join(f"'{s}'" for s in _OPEN_STATES) + ")"),
        )
    for name, ddl in _SETTINGS:
        op.execute(f"ALTER TABLE governance_settings ADD COLUMN IF NOT EXISTS {name} {ddl}")
        # The default only fills the existing row; the model supplies it for new ones (and the schema comparison
        # cannot compare a JSON server default).
        op.execute(f"ALTER TABLE governance_settings ALTER COLUMN {name} DROP DEFAULT")


def downgrade() -> None:
    for name, _ddl in reversed(_SETTINGS):
        op.execute(f"ALTER TABLE governance_settings DROP COLUMN IF EXISTS {name}")
    op.execute("DROP TABLE IF EXISTS rule_cleanup_items")
    op.execute("DROP TABLE IF EXISTS rule_cleanup_operations")
