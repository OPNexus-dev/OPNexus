"""Sicherheitslage: letztes Prüfergebnis je Host.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Eine Altinstallation wird mit create_all() nach den aktuellen Modellen gebaut.
    if sa.inspect(op.get_bind()).has_table("compliance_results"):
        return
    op.create_table(
        "compliance_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(length=20), nullable=True),
        sa.Column("error_detail", sa.String(length=300), nullable=True),
        sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_compliance_results_host_id", "compliance_results", ["host_id"], unique=True)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS compliance_results")
