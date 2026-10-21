"""Firmware-Rollouts in Wellen: Pläne, Läufe, Hosts je Lauf; Benachrichtigung bei Rollout-Problemen (1.25).

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.execute("ALTER TABLE notification_settings ADD COLUMN IF NOT EXISTS rollout_problems_enabled boolean NOT NULL DEFAULT true")
    # A pre-Alembic install is bridged with create_all() first (app/migrate.py), which already made the tables.
    if sa.inspect(op.get_bind()).has_table("rollout_plans"):
        return
    op.create_table(
        "rollout_plans",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("window_id", UUID, nullable=True),
        sa.Column("auto_start", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("gate_checks", sa.JSON(), nullable=False),
        sa.Column("compliance_mode", sa.String(8), server_default="warn", nullable=False),
        sa.Column("activated_by", sa.String(150), nullable=True),
        sa.Column("activated_at", TS, nullable=True),
        sa.Column("activated_fingerprint", sa.String(64), nullable=True),
        sa.Column("auto_start_error", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.String(150), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["window_id"], ["maintenance_windows.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_rollout_plans_window_id", "rollout_plans", ["window_id"])
    op.create_table(
        "rollout_plan_waves",
        sa.Column("id", UUID, nullable=False),
        sa.Column("plan_id", UUID, nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("soak_minutes", sa.Integer(), nullable=False),
        sa.Column("carp_handover", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("require_approval", sa.Boolean(), server_default="false", nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["rollout_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rollout_plan_waves_plan_id", "rollout_plan_waves", ["plan_id"])
    op.create_table(
        "rollout_wave_groups",
        sa.Column("wave_id", UUID, nullable=False),
        sa.Column("group_id", UUID, nullable=False),
        sa.ForeignKeyConstraint(["wave_id"], ["rollout_plan_waves.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["device_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("wave_id", "group_id"),
    )
    op.create_table(
        "rollout_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("plan_id", UUID, nullable=True),
        sa.Column("plan_name", sa.String(120), nullable=False),
        sa.Column("plan_snapshot", sa.JSON(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("paused_from", sa.String(20), nullable=True),
        sa.Column("current_wave", sa.Integer(), nullable=False),
        sa.Column("target_version", sa.String(40), nullable=True),
        sa.Column("pause_reason", sa.String(40), nullable=True),
        sa.Column("pause_detail", sa.JSON(), nullable=True),
        sa.Column("started_by", sa.String(150), nullable=False),
        sa.Column("trigger", sa.String(8), nullable=False),
        sa.Column("window_occurrence", TS, nullable=True),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("step_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["rollout_plans.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "window_occurrence", name="uq_rollout_run_occurrence"),
    )
    op.create_index("ix_rollout_runs_plan_id", "rollout_runs", ["plan_id"])
    op.create_index("ix_rollout_runs_status", "rollout_runs", ["status"])
    op.create_table(
        "rollout_run_waves",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("baseline", sa.JSON(), nullable=True),
        sa.Column("gate_result", sa.JSON(), nullable=True),
        sa.Column("soak_until", TS, nullable=True),
        sa.Column("gate_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("approved_by", sa.String(150), nullable=True),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["rollout_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rollout_run_waves_run_id", "rollout_run_waves", ["run_id"])
    op.create_table(
        "rollout_run_hosts",
        sa.Column("id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("wave_index", sa.Integer(), nullable=False),
        sa.Column("host_id", UUID, nullable=True),
        sa.Column("host_name", sa.String(120), nullable=False),
        sa.Column("cluster_name", sa.String(120), nullable=True),
        sa.Column("order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("scheduled_maintenance_id", UUID, nullable=True),
        sa.Column("version_before", sa.String(40), nullable=True),
        sa.Column("version_after", sa.String(40), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("finished_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["rollout_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["host_id"], ["opnsense_hosts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["scheduled_maintenance_id"], ["scheduled_maintenance.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "host_id", name="uq_rollout_run_host"),
    )
    op.create_index("ix_rollout_run_hosts_run_id", "rollout_run_hosts", ["run_id"])
    op.create_index("ix_rollout_run_hosts_host_id", "rollout_run_hosts", ["host_id"])
    op.create_index("ix_rollout_run_hosts_scheduled_maintenance_id", "rollout_run_hosts", ["scheduled_maintenance_id"])


def downgrade() -> None:
    for table in ("rollout_run_hosts", "rollout_run_waves", "rollout_runs", "rollout_wave_groups", "rollout_plan_waves", "rollout_plans"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("ALTER TABLE notification_settings DROP COLUMN IF EXISTS rollout_problems_enabled")
