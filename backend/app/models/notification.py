"""Delivery config (singleton row) and open/resolved problem tracking for
the notification feature. Secrets (SMTP password, webhook signing secret)
never live here — see services/notification_secret_store.py, mirroring how
OPNsense API secrets stay off the database entirely.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class NotificationSettings(Base):
    """Single row, id fixed at 1 — see notification_settings_service.get_settings
    for the get-or-create helper. The only admin-editable operational config
    in the codebase today; everywhere else config is env-var only, but this
    feature is meant for future non-technical users without compose access."""

    __tablename__ = "notification_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    webhook_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    webhook_signing_secret_set: Mapped[bool] = mapped_column(Boolean, default=False)

    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    # "none" | "starttls" | "ssl"
    smtp_security: Mapped[str] = mapped_column(String(10), default="starttls")
    smtp_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_password_set: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_from_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_to_addresses: Mapped[list] = mapped_column(JSON, default=list)

    host_offline_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    cert_expiring_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rule_drift_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    revert_failed_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    maintenance_unknown_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    carp_health_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    nat_drift_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    network_drift_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    carp_auto_revert_failed_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    scheduled_maintenance_failed_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    backup_failed_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Off by default: switching it on for an existing fleet reports every long-standing finding at once.
    compliance_alerts_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class NotificationProblem(Base):
    """Open/resolved state per detected problem, keyed by (problem_type,
    dedup_key) so a still-open problem is never re-notified every poll —
    only transitions (newly open, newly resolved) trigger delivery.

    Delivery is best-effort, not an audit trail: last_notified_at/
    resolved_notified_at are set once per transition and never retried.
    audit_events remains the real immutable record for deploy actions;
    this table is a convenience signal layered on top, not a new source
    of truth.
    """

    __tablename__ = "notification_problems"
    __table_args__ = (UniqueConstraint("problem_type", "dedup_key", name="uq_notification_problem_key"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # host_offline | cert_expiry | rule_drift | revert_failed | maintenance_unknown | ... (see ProblemType)
    problem_type: Mapped[str] = mapped_column(String(32))
    dedup_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | resolved
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    first_detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
