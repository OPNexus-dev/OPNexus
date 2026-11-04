"""Regeln: Platzierung beim Ausrollen ("end" = hinter allen Regeln, "top" = ganz oben in der Prioritätsgruppe).

Revision ID: 0035
Revises: 0034
Create Date: 2026-11-04
"""
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE firewall_rules ADD COLUMN IF NOT EXISTS placement varchar(8) NOT NULL DEFAULT 'end'")


def downgrade() -> None:
    op.execute("ALTER TABLE firewall_rules DROP COLUMN IF EXISTS placement")
