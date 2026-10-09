"""Regeln: Protokollieren-Flag (log).

Bisher schrieb jeder Push fest log=0. Damit eine von Hand angelegte Regel mit
Protokollierung übernommen und danach bearbeitet werden kann, ohne dass das
Flag still verloren geht, ist es jetzt ein Feld der Regel.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-09
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE firewall_rules ADD COLUMN IF NOT EXISTS log boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("ALTER TABLE firewall_rules DROP COLUMN IF EXISTS log")
