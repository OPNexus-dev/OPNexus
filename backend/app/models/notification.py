"""Delivery config (singleton row) and open/resolved problem tracking for
the notification feature. Secrets (SMTP password, webhook signing secret)
never live here — see services/notification_secret_store.py, mirroring how
OPNsense API secrets stay off the database entirely.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


DEFAULT_SERVICE_WATCHLIST = (
    "unbound", "dnsmasq", "dhcpd", "dhcpd6", "kea-dhcp4", "kea-dhcp6", "ntpd", "openvpn", "wireguard", "strongswan",
)

CHANNEL_TYPES = ("ntfy", "gotify", "telegram", "teams", "matrix")


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

    # Monitoring alerts (1.24). The gateway alert is on by default (a WAN outage is the one thing every
    # operator wants to hear about); resource thresholds and service checks are off, like compliance alerts:
    # switching them on for an existing fleet may report long-standing conditions all at once.
    gateway_down_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    resource_threshold_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    service_down_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # An API key that cannot read gateway / service state makes those alerts silently blind (1.29): say so.
    api_rights_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Gateways whose alert is switched off, "<host_id>:<gateway name>" (1.29), e.g. an idle second WAN.
    gateway_alert_muted: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Rollouts (1.25): a run paused (gate, failure, window...) or an automatic start did not happen.
    rollout_problems_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # Gateway: OPNsense' own verdict (down / loss / delay) decides "offline"; these two add an
    # OPNexus threshold on the measured values (0 = off).
    gateway_loss_threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=20, server_default="20")
    gateway_delay_threshold_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=500, server_default="500")
    # The disk OPNexus itself runs on (1.34): a full disk stops the database, and with it every alert. On by default.
    server_disk_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    server_disk_threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=90, server_default="90")
    # Resources (0 = that threshold is off). Load is per CPU thread.
    disk_threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=90, server_default="90")
    memory_threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=95, server_default="95")
    swap_threshold_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    load_per_cpu_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=2.0, server_default="2.0")
    # Debouncing: consecutive 60 s resource samples that must agree before a problem opens or closes.
    alert_confirm_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    resource_confirm_samples: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    # OPNsense service names that are reported when they stop (the API cannot tell a crash from a stop on purpose).
    # NULL = the default list (an old row never got one).
    service_watchlist: Mapped[list | None] = mapped_column(JSON, nullable=True, default=lambda: list(DEFAULT_SERVICE_WATCHLIST))

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


class NotificationChannel(Base):
    """An additional delivery channel (ntfy, Gotify, Telegram, Microsoft Teams, Matrix) next to the SMTP
    and generic-webhook settings above. Several channels of one type are allowed. ``config`` holds only
    non-secret parameters (server URL, topic, chat id, room id); the credential — token, or the whole
    webhook URL for Teams — lives in notification_secret_store under ``channel_<id>``, never here."""

    __tablename__ = "notification_channels"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    channel_type: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    secret_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
