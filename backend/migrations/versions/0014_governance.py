"""Aufgabentrennung: dürfen Operatoren selbst ausrollen?

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("governance_settings"):
        return
    op.create_table(
        "governance_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("operators_may_deploy", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS governance_settings")
