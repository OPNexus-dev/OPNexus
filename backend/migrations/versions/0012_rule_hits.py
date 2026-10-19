"""Regelnutzung: wann hat eine Regel zuletzt etwas getroffen.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("rule_hits"):
        return
    op.create_table(
        "rule_hits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rule_uuid", sa.String(length=36), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_packets", sa.BigInteger(), nullable=False),
        sa.Column("last_bytes", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host_id", "rule_uuid", name="uq_rule_hit"),
    )
    op.create_index("ix_rule_hits_host_id", "rule_hits", ["host_id"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rule_hits")
