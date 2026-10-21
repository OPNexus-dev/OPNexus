"""GUI-Zugriff über das gui-gateway (1.27): Einstellungen, Einmal-Tickets, Gateway-Sitzungen.

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # A pre-Alembic install is bridged with create_all() first (app/migrate.py), which already made the tables.
    if sa.inspect(op.get_bind()).has_table("gui_access_settings"):
        return
    op.create_table(
        "gui_access_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("idle_timeout_minutes", sa.Integer(), server_default="15", nullable=False),
        sa.Column("absolute_timeout_minutes", sa.Integer(), server_default="60", nullable=False),
        sa.Column("slot_count", sa.Integer(), server_default="4", nullable=False),
        sa.Column("updated_by", sa.String(150), nullable=True),
        sa.Column("updated_at", TS, nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "gui_tickets",
        sa.Column("id", UUID, nullable=False),
        sa.Column("ticket_hash", sa.String(64), nullable=False),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("host_id", UUID, nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("redeemed_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["admin_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ticket_hash"),
    )
    op.create_index("ix_gui_tickets_session_hash", "gui_tickets", ["session_hash"])
    op.create_index("ix_gui_tickets_user_id", "gui_tickets", ["user_id"])
    op.create_index("ix_gui_tickets_host_id", "gui_tickets", ["host_id"])
    op.create_table(
        "gui_sessions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("session_hash", sa.String(64), nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("host_id", UUID, nullable=False),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("client_ip", sa.String(45), nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("last_checked_at", TS, nullable=False),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("end_reason", sa.String(32), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["admin_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_gui_sessions_session_hash", "gui_sessions", ["session_hash"])
    op.create_index("ix_gui_sessions_user_id", "gui_sessions", ["user_id"])
    op.create_index("ix_gui_sessions_host_id", "gui_sessions", ["host_id"])


def downgrade() -> None:
    op.drop_table("gui_sessions")
    op.drop_table("gui_tickets")
    op.drop_table("gui_access_settings")
