"""OIDC: Standard-Scopes ohne "groups" (Keycloak und Entra ID lehnen den Scope als ungültig ab).

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-20
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only rows still carrying the old default; a value somebody typed on purpose stays.
    op.execute("UPDATE oidc_settings SET scopes = 'openid profile email' WHERE scopes = 'openid profile email groups'")


def downgrade() -> None:
    pass
