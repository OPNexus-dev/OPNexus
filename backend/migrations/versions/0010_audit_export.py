"""Audit-Export: Syslog und Webhook.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("audit_export_settings"):
        return
    op.create_table(
        "audit_export_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("syslog_enabled", sa.Boolean(), nullable=False),
        sa.Column("syslog_host", sa.String(length=255), nullable=True),
        sa.Column("syslog_port", sa.Integer(), nullable=False),
        sa.Column("syslog_protocol", sa.String(length=3), nullable=False),
        sa.Column("webhook_enabled", sa.Boolean(), nullable=False),
        sa.Column("webhook_url", sa.String(length=500), nullable=True),
        sa.Column("webhook_signing_secret_set", sa.Boolean(), nullable=False),
        sa.Column("cursor_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor_id", sa.String(length=36), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_export_settings")
