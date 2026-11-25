"""Sprache der Benachrichtigungstexte (NULL = automatisch).

Revision ID: 0038
Revises: 0037
Create Date: 2026-11-22
"""
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS language varchar(5)")


def downgrade() -> None:
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS language")
