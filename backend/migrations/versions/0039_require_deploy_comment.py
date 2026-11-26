"""Option: ein Kommentar beim Ausrollen ist Pflicht (Standard aus).

Revision ID: 0039
Revises: 0038
Create Date: 2026-11-26
"""
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE governance_settings ADD COLUMN IF NOT EXISTS require_deploy_comment boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE governance_settings DROP COLUMN IF EXISTS require_deploy_comment")
