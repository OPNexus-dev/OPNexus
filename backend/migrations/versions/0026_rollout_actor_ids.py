"""Rollouts: die verantwortliche Person zusätzlich als stabile Benutzer-ID (1.29).

Bisher stand nur der Benutzername im Plan (``activated_by``) und im Lauf (``started_by``). Wird ein Konto gelöscht
und ein neues mit gleichem Namen angelegt, hätte der automatische Start unter der Rechten der neuen Person
weitergelaufen. Die ID ändert sich nie. Vorhandene Zeilen werden über den Benutzernamen zugeordnet (soweit das Konto
noch existiert); ohne Treffer bleibt die ID leer und der Name gilt wie bisher.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-22
"""
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS: a pre-Alembic install is bridged with create_all() first, which may already have the columns.
    op.execute("ALTER TABLE rollout_plans ADD COLUMN IF NOT EXISTS activated_by_id uuid")
    op.execute("ALTER TABLE rollout_runs ADD COLUMN IF NOT EXISTS started_by_id uuid")
    # No foreign key on purpose: a deleted account must stay recognisably gone (SET NULL would fall back to the name).
    op.execute("UPDATE rollout_plans p SET activated_by_id = u.id FROM admin_users u "
               "WHERE p.activated_by_id IS NULL AND p.activated_by IS NOT NULL AND u.username = p.activated_by")
    op.execute("UPDATE rollout_runs r SET started_by_id = u.id FROM admin_users u "
               "WHERE r.started_by_id IS NULL AND u.username = r.started_by")


def downgrade() -> None:
    op.execute("ALTER TABLE rollout_runs DROP COLUMN IF EXISTS started_by_id")
    op.execute("ALTER TABLE rollout_plans DROP COLUMN IF EXISTS activated_by_id")
