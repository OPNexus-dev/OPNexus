"""Geplante Wartung mit automatischer CARP-Übergabe: Option, Phase und Fortschritt.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE scheduled_maintenance ADD COLUMN IF NOT EXISTS carp_handover boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE scheduled_maintenance ADD COLUMN IF NOT EXISTS phase varchar(16)")
    op.execute("ALTER TABLE scheduled_maintenance ADD COLUMN IF NOT EXISTS phase_since timestamp with time zone")
    # The default only fills existing rows; like every JSON column here the model has no server default.
    op.execute("ALTER TABLE scheduled_maintenance ADD COLUMN IF NOT EXISTS progress json NOT NULL DEFAULT '{}'::json")
    op.execute("ALTER TABLE scheduled_maintenance ALTER COLUMN progress DROP DEFAULT")


def downgrade() -> None:
    for column in ("progress", "phase_since", "phase", "carp_handover"):
        op.execute(f"ALTER TABLE scheduled_maintenance DROP COLUMN IF EXISTS {column}")
