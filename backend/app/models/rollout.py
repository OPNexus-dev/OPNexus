"""Firmware rollouts in waves (1.25): plans (what, in which order, when) and runs (what happened).

A plan's waves point at device groups ("rings": canary -> pilot -> prod). A run freezes the plan
(``plan_snapshot``) and the resolved hosts (``RolloutRunHost``) when it starts, so later edits of
the plan or of a group never change a run that is under way. The run never fires firmware actions
itself: every host step is an ordinary ScheduledMaintenance row (origin="rollout",
auto_verify=True) linked from ``RolloutRunHost.scheduled_maintenance_id`` — see
services/rollout_service.py.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, Table, Text,
                        UniqueConstraint)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


rollout_wave_groups = Table(
    "rollout_wave_groups", Base.metadata,
    Column("wave_id", PGUUID(as_uuid=True), ForeignKey("rollout_plan_waves.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", PGUUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), primary_key=True),
)


class RolloutPlan(Base):
    __tablename__ = "rollout_plans"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String(16))  # update | major_update
    window_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("maintenance_windows.id", ondelete="SET NULL"), nullable=True, index=True)
    # Start a run by itself at every occurrence of the window. Only for "update" and only while the plan
    # is activated AND unchanged since (activated_fingerprint).
    auto_start: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    gate_checks: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    compliance_mode: Mapped[str] = mapped_column(String(8), nullable=False, default="warn", server_default="warn")
    activated_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Why the last automatic start did not happen: {"occurrence", "code", "params", "at"} (notification source).
    auto_start_error: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    waves: Mapped[list["RolloutPlanWave"]] = relationship(
        "RolloutPlanWave", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
        order_by="RolloutPlanWave.position")


class RolloutPlanWave(Base):
    __tablename__ = "rollout_plan_waves"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("rollout_plans.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(60))
    soak_minutes: Mapped[int] = mapped_column(Integer, default=30)
    carp_handover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Wait for a human "release" before this wave starts (not used for the first wave: starting is the release).
    require_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    groups: Mapped[list["DeviceGroup"]] = relationship("DeviceGroup", secondary=rollout_wave_groups, lazy="selectin")  # noqa: F821


class RolloutRun(Base):
    __tablename__ = "rollout_runs"
    __table_args__ = (UniqueConstraint("plan_id", "window_occurrence", name="uq_rollout_run_occurrence"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("rollout_plans.id", ondelete="SET NULL"), nullable=True, index=True)
    plan_name: Mapped[str] = mapped_column(String(120))
    plan_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    action: Mapped[str] = mapped_column(String(16))
    # waiting_window | preflight | wave_running | soaking | gate | awaiting_approval | resuming | paused
    # | succeeded | cancelled
    status: Mapped[str] = mapped_column(String(20), index=True)
    paused_from: Mapped[str | None] = mapped_column(String(20), nullable=True)
    current_wave: Mapped[int] = mapped_column(Integer, default=0)
    target_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pause_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    pause_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_by: Mapped[str] = mapped_column(String(150))
    trigger: Mapped[str] = mapped_column(String(8))  # manual | window
    window_occurrence: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optimistic lock: every state change bumps it; an engine step computed on an older revision is dropped.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    progress: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    step_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    waves: Mapped[list["RolloutRunWave"]] = relationship(
        "RolloutRunWave", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin", order_by="RolloutRunWave.index")
    hosts: Mapped[list["RolloutRunHost"]] = relationship(
        "RolloutRunHost", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin",
        order_by=lambda: [RolloutRunHost.wave_index, RolloutRunHost.order])


class RolloutRunWave(Base):
    __tablename__ = "rollout_run_waves"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("rollout_runs.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)
    label: Mapped[str] = mapped_column(String(60))
    # pending | awaiting_approval | preflight | running | soaking | gate | passed | skipped | cancelled
    status: Mapped[str] = mapped_column(String(20), default="pending")
    baseline: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    gate_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    soak_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gate_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    approved_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RolloutRunHost(Base):
    __tablename__ = "rollout_run_hosts"
    __table_args__ = (UniqueConstraint("run_id", "host_id", name="uq_rollout_run_host"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("rollout_runs.id", ondelete="CASCADE"), index=True)
    wave_index: Mapped[int] = mapped_column(Integer)
    host_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="SET NULL"), nullable=True, index=True)
    host_name: Mapped[str] = mapped_column(String(120))
    cluster_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    # queued | running | verifying | succeeded | skipped | failed | blocked | cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued")
    scheduled_maintenance_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("scheduled_maintenance.id", ondelete="SET NULL"), nullable=True, index=True)
    version_before: Mapped[str | None] = mapped_column(String(40), nullable=True)
    version_after: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # {"code": ..., "params": {...}} — translated in the UI (rollouts.hostNote.*)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
