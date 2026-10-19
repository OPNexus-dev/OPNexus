"""Management-Overlay (WireGuard-Hub) für Firewalls hinter NAT.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("overlay_settings"):
        op.create_table(
            "overlay_settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("endpoint", sa.String(length=255), nullable=True),
            sa.Column("subnet", sa.String(length=18), nullable=False),
            sa.Column("listen_port", sa.Integer(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
    if not inspector.has_table("overlay_peers"):
        op.create_table(
            "overlay_peers",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("ip", sa.String(length=15), nullable=False),
            sa.Column("public_key", sa.String(length=44), nullable=True),
            sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("name"),
            sa.UniqueConstraint("ip"),
            sa.UniqueConstraint("public_key"),
        )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS overlay_peers")
    op.execute("DROP TABLE IF EXISTS overlay_settings")
