"""Versionsprüfung im Webinterface einstellbar.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_DEFAULT_FEED_URL = "https://updates.opnexus.dev/latest.json"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("update_check_settings"):
        return
    op.create_table(
        "update_check_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("feed_url", sa.String(length=500), server_default=_DEFAULT_FEED_URL, nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS update_check_settings")
