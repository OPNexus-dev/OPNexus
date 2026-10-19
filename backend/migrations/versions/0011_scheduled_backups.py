"""Geplante Backups mit Aufbewahrung und verschlüsseltem Offsite-Ziel; Benachrichtigung bei Backup-Problemen.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    op.execute("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS backup_failed_enabled boolean NOT NULL DEFAULT true")
    if not inspector.has_table("backup_settings"):
        op.create_table(
            "backup_settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("scheduled_enabled", sa.Boolean(), nullable=False),
            sa.Column("interval_hours", sa.Integer(), nullable=False),
            sa.Column("keep_count", sa.Integer(), nullable=False),
            sa.Column("offsite_enabled", sa.Boolean(), nullable=False),
            sa.Column("s3_endpoint", sa.String(length=255), nullable=True),
            sa.Column("s3_region", sa.String(length=64), nullable=False),
            sa.Column("s3_bucket", sa.String(length=255), nullable=True),
            sa.Column("s3_prefix", sa.String(length=255), nullable=False),
            sa.Column("s3_access_key", sa.String(length=255), nullable=True),
            sa.Column("s3_path_style", sa.Boolean(), nullable=False),
            sa.Column("s3_secret_set", sa.Boolean(), nullable=False),
            sa.Column("passphrase_set", sa.Boolean(), nullable=False),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_offsite_ok_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_offsite_error", sa.String(length=300), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    if not inspector.has_table("backup_uploads"):
        op.create_table(
            "backup_uploads",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("host_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("snapshot_id", sa.String(length=64), nullable=False),
            sa.Column("remote_key", sa.String(length=400), nullable=False),
            sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("host_id", "snapshot_id", name="uq_backup_upload"),
        )
        op.create_index("ix_backup_uploads_host_id", "backup_uploads", ["host_id"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS backup_uploads")
    op.execute("DROP TABLE IF EXISTS backup_settings")
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS backup_failed_enabled")
