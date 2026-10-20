"""Alarme für Gateways, Ressourcen und Dienste; zusätzliche Benachrichtigungskanäle.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_COLUMNS = [
    ("gateway_down_enabled", "boolean NOT NULL DEFAULT true"),
    ("resource_threshold_enabled", "boolean NOT NULL DEFAULT false"),
    ("service_down_enabled", "boolean NOT NULL DEFAULT false"),
    ("gateway_loss_threshold_pct", "integer NOT NULL DEFAULT 20"),
    ("gateway_delay_threshold_ms", "integer NOT NULL DEFAULT 500"),
    ("disk_threshold_pct", "integer NOT NULL DEFAULT 90"),
    ("memory_threshold_pct", "integer NOT NULL DEFAULT 95"),
    ("swap_threshold_pct", "integer NOT NULL DEFAULT 50"),
    ("load_per_cpu_threshold", "double precision NOT NULL DEFAULT 2.0"),
    ("alert_confirm_samples", "integer NOT NULL DEFAULT 3"),
    ("resource_confirm_samples", "integer NOT NULL DEFAULT 5"),
    ("service_watchlist", "json"),
]


def upgrade() -> None:
    for name, ddl in _COLUMNS:
        op.execute(f"ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS {name} {ddl}")
    if sa.inspect(op.get_bind()).has_table("notification_channels"):
        return
    op.create_table(
        "notification_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("channel_type", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secret_set", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS notification_channels")
    for name, _ddl in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE notification_settings DROP COLUMN IF EXISTS {name}")
