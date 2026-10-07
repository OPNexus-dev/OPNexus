import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AcmeCertificateRequest(Base):
    """A centrally-authored ACME certificate request (`AcmeClient.xml` ->
    `certificates.certificate`, `CertificatesController.php`) — per-host,
    same reasoning as AcmeAccount/ManagedCertificate.

    Field names verified directly against AcmeClient.xml (fetched
    2026-09-09): `name` is the CN/FQDN (Required). `altNames` is a
    CSVListField (comma-separated on the wire) — modeled here as a plain
    `alt_names` string, NOT a sub-array, matching OPNsense's own
    convention. `account` and `validationMethod` are BOTH Required
    ModelRelationFields on the wire — `account` is filled in by THIS app
    at deploy time (injecting the linked AcmeAccount's own per-host
    OPNsense uuid — see acme_service.py's build_payload(), the same
    UUID-injection pattern ipsec_deploy_service.py documents for its own
    `connection` field, including the SAME no-op-redeploy pitfall: the
    injected uuid must never be baked into the dict compared/stored as
    `last_payload`). `validationMethod` (this app's `validation_ref`) is
    plain, OPNexus-unvalidated: the plan is explicit that DNS-provider
    validation config (`validations.validation`) is entirely out of v1
    scope — the operator configures it themselves in OPNsense's own GUI
    and types the resulting UUID in here; OPNexus never validates the
    UUID's content, only that a target host actually has one selected
    (same trust model IpsecTunnel's local_address/remote_address use).

    `keyLength` OptionValues are PREFIXED (`key_2048`/`key_3072`/
    `key_4096`/`key_ec256`/`key_ec384`), NOT bare numbers — a real
    field-shape trap the plan's prose summary ("2048/3072/4096/ec256/
    ec384") would have produced a silently-rejected payload for if not
    re-verified against the model XML directly.

    `renew_interval` (IntegerField, Default 60): DAYS SINCE LAST
    ISSUANCE, NOT days-before-expiry (verified in the plan against
    AcmeClient's own cron semantics) — this is the exact semantic trap
    the plan calls out; see the UI label in AcmeCertificateForm.tsx and
    the README for the same warning surfaced to the operator.

    Local add/set is a purely local config write (status:
    draft/success/error, no restart/lockout risk — same model as
    ManagedCertificate/AcmeAccount). The separate `sign` action
    (`POST /acmeclient/certificates/sign/{uuid}`) is asynchronous
    (configd forks and returns immediately) AND reaches the real external
    CA — sign_status/sign_triggered_at record only that a trigger was
    sent, never a claimed outcome; remote_status_code/
    remote_status_last_update/remote_last_update are populated by polling
    `GET /acmeclient/certificates/get/{uuid}` afterwards (see
    acme_service.py's poll_certificate_status())."""

    __tablename__ = "acme_certificate_requests"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    alt_names: Mapped[str] = mapped_column(String(2000), default="")
    account_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("acme_accounts.id"))
    validation_ref: Mapped[str] = mapped_column(String(64))
    key_length: Mapped[str] = mapped_column(String(16), default="key_4096")
    ocsp: Mapped[bool] = mapped_column(default=False)
    auto_renewal: Mapped[bool] = mapped_column(default=True)
    renew_interval: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(default=True)

    opnsense_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sign_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sign_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    sign_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_status_last_update: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_last_update: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
    account: Mapped["AcmeAccount"] = relationship("AcmeAccount")
