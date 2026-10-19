"""Standort-Variablen: Werte für {{NAME}}-Platzhalter je Host.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("host_variables"):
        return
    op.create_table(
        "host_variables",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=41), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("host_id", "name", name="uq_host_variable"),
    )
    op.create_index("ix_host_variables_host_id", "host_variables", ["host_id"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS host_variables")
