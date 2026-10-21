"""Wiederkehrende Wartungsfenster mit Ausnahmen (1.25).

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A pre-Alembic install is bridged with create_all() first (app/migrate.py), which already made the tables.
    if sa.inspect(op.get_bind()).has_table("maintenance_windows"):
        return
    op.create_table(
        "maintenance_windows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("recurrence", sa.String(length=16), nullable=False),
        sa.Column("weekdays", sa.JSON(), nullable=False),
        sa.Column("month_mode", sa.String(length=16), nullable=True),
        sa.Column("month_nth", sa.Integer(), nullable=True),
        sa.Column("month_weekday", sa.Integer(), nullable=True),
        sa.Column("month_day", sa.Integer(), nullable=True),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("start_cutoff_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_by", sa.String(length=150), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "maintenance_window_exceptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("window_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("occurrence_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("moved_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=300), nullable=False),
        sa.Column("actor", sa.String(length=150), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["window_id"], ["maintenance_windows.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("window_id", "occurrence_date", name="uq_window_exception_date"),
    )
    op.create_index("ix_maintenance_window_exceptions_window_id", "maintenance_window_exceptions", ["window_id"])


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS maintenance_window_exceptions")
    op.execute("DROP TABLE IF EXISTS maintenance_windows")
