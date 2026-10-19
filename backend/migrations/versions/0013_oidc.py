"""Single Sign-on (OIDC): Einstellungen, Anmeldeversuche, Herkunft der Konten.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    op.execute("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS auth_source varchar(10) NOT NULL DEFAULT 'local'")
    op.execute("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS oidc_subject varchar(600)")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_admin_users_oidc_subject ON admin_users (oidc_subject)")
    if not inspector.has_table("oidc_settings"):
        op.create_table(
            "oidc_settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("display_name", sa.String(length=60), nullable=False),
            sa.Column("issuer", sa.String(length=500), nullable=True),
            sa.Column("client_id", sa.String(length=255), nullable=True),
            sa.Column("client_secret_set", sa.Boolean(), nullable=False),
            sa.Column("scopes", sa.String(length=255), nullable=False),
            sa.Column("groups_claim", sa.String(length=64), nullable=False),
            sa.Column("admin_group", sa.String(length=255), nullable=True),
            sa.Column("operator_group", sa.String(length=255), nullable=True),
            sa.Column("read_only_group", sa.String(length=255), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    if not inspector.has_table("oidc_login_states"):
        op.create_table(
            "oidc_login_states",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("state_hash", sa.String(length=64), nullable=False),
            sa.Column("nonce", sa.String(length=64), nullable=False),
            sa.Column("code_verifier", sa.String(length=128), nullable=False),
            sa.Column("redirect_uri", sa.String(length=500), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_oidc_login_states_state_hash", "oidc_login_states", ["state_hash"], unique=True)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS oidc_login_states")
    op.execute("DROP TABLE IF EXISTS oidc_settings")
    op.execute("DROP INDEX IF EXISTS ix_admin_users_oidc_subject")
    op.execute("ALTER TABLE admin_users DROP COLUMN IF EXISTS oidc_subject")
    op.execute("ALTER TABLE admin_users DROP COLUMN IF EXISTS auth_source")
