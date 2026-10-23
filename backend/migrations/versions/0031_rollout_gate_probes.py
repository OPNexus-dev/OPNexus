"""Rollout-Gate: Erreichbarkeitsproben von der Firewall aus (1.32, B5).

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-23
"""
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: a pre-Alembic install is bridged with create_all() first, which may already have the column.
    op.execute("ALTER TABLE rollout_plans ADD COLUMN IF NOT EXISTS gate_probes json")
    op.execute("UPDATE rollout_plans SET gate_probes = '[]'::json WHERE gate_probes IS NULL")
    op.execute("ALTER TABLE rollout_plans ALTER COLUMN gate_probes SET NOT NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE rollout_plans DROP COLUMN IF EXISTS gate_probes")
