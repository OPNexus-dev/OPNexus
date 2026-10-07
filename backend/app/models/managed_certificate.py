import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ManagedCertificate(Base):
    """A centrally-authored Trust-Store certificate (OPNsense core,
    `Trust/Api/CertController.php` — no plugin involved), deliberately
    modeled per-host (a plain `host_id` FK), NOT with the
    DeviceGroup/`effective_hosts` multi-target pattern `policy_service.py`
    provides for rules/IPsec/OpenVPN/WireGuard. Reason (per the plan): a
    certificate's `caref` and its own OPNsense-assigned `refid`/uuid are
    host-local — an internally-signed cert or CSR on host A has no
    meaningful counterpart UUID on host B, so a "push identical payload to
    N hosts" model would be actively misleading here, unlike a firewall
    rule or a PSK-based IPsec tunnel where the same payload legitimately
    IS the same intent on every target.

    Field names verified directly against Trust/Cert.xml (fetched
    2026-09-09): the model's DescriptionField wire key is `descr`, NOT
    `description` (this app's own `description` column is translated to
    `descr` in managed_certificate_service.py's payload builder). The
    plaintext-in fields are `crt_payload`/`csr_payload`/`prv_payload` —
    CertController::setBaseHook() base64-encodes these into `crt`/`csr`/
    `prv` server-side, so this app always sends plain PEM/CSR text, never
    base64 itself. `key_type` OptionValues are RSA sizes ("512".."8192")
    or EC curve names ("prime256v1"/"secp384r1"/"secp521r1") — this app
    exposes a curated subset (2048/3072/4096 RSA + the three EC curves).
    `cert_type` (Required, Default "usr_cert") is a verified field beyond
    what the plan enumerated — exposed here with a "server_cert" default
    since TLS server certs are this project's realistic use case, whereas
    OPNsense's own UI default targets client certs.

    No deploy/apply step exists for Trust-Store writes at all (confirmed
    in the plan, `opnsense/core#9399`) — a push IS the add/set API call,
    immediately live, no follow-up reconfigure/restart. `status` is
    therefore only "draft" (never pushed) / "success" / "error" — even
    simpler than IPsec/OpenVPN/WireGuard's already-reduced model, and this
    entity needs NO warning gate in the UI (no lockout-shaped risk exists
    here — see README).

    The private key (`action="import"` only, user-supplied) is NEVER
    stored in Postgres — disk-only, one file per entity id, mode 0600 (see
    cert_key_store.py, mirroring ipsec_psk_store.py exactly).
    `private_key_set` just tracks whether a file exists.

    Deleting an in-use certificate is blocked by OPNsense itself
    (`checkAndThrowValueInUse()` inside `CertController::delAction()`) —
    this app does not pre-check that condition, it surfaces OPNsense's own
    error message verbatim (see managed_certificate_service.py)."""

    __tablename__ = "managed_certificates"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True
    )
    # Cert.action OptionValues, verified against Cert.xml: import/internal/
    # external/sign_csr/import_csr/reissue/manual.
    action: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(255))  # -> wire "descr"
    # DN fields, only meaningful for action in {internal, external, reissue}
    # (CertStore::createCert()/reIssueCert()'s $node->dn() argument).
    commonname: Mapped[str] = mapped_column(String(255), default="")
    country: Mapped[str] = mapped_column(String(8), default="DE")
    state: Mapped[str] = mapped_column(String(255), default="")
    city: Mapped[str] = mapped_column(String(255), default="")
    organization: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    key_type: Mapped[str] = mapped_column(String(16), default="2048")
    digest: Mapped[str] = mapped_column(String(16), default="sha256")
    cert_type: Mapped[str] = mapped_column(String(32), default="server_cert")
    lifetime: Mapped[int] = mapped_column(Integer, default=397)
    # Optional, manually-entered internal-CA UUID already existing on this
    # host (host-local — see class docstring for why this can't be a
    # cross-host reference).
    caref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    csr_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    crt_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_key_set: Mapped[bool] = mapped_column(default=False)

    opnsense_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # The OPNsense-shaped payload last successfully written — compared
    # against a fresh push's payload to detect a true no-op re-push. The
    # private key is deliberately never part of this (see cert_key_store.py
    # module docstring for why this deviates from IPsec/OpenVPN/WireGuard's
    # precedent of embedding secret material in last_payload).
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
