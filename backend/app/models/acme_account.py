import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AcmeAccount(Base):
    """A centrally-authored ACME account (`os-acme-client` plugin,
    `AcmeClient.xml` -> `accounts.account`, `AccountsController.php`) —
    per-host like ManagedCertificate, same reasoning: the account's
    OPNsense-assigned uuid (referenced by AcmeCertificateRequest.account at
    deploy time — see acme_service.py's UUID-injection, mirroring
    ipsec_deploy_service.py's `connection` UUID pattern) is host-local.

    Field names verified directly against AcmeClient.xml (fetched
    2026-09-09): `ca` OptionValues are letsencrypt/letsencrypt_test/zerossl/
    buypass/buypass_test/google/google_test/sslcom/custom (Default
    "letsencrypt"). `custom_ca` only applies when ca="custom".

    `eab_hmac` is sensitive External-Account-Binding material — like
    ManagedCertificate's private key, it NEVER touches Postgres as
    plaintext, only disk (see acme_secret_store.py, mirroring
    ipsec_psk_store.py). `eab_hmac_set` tracks whether a value has been
    provided. `eab_kid` is a plain (non-secret) key identifier, stored
    directly.

    Local add/set (create/edit) is a purely local config write, same
    "no restart, no lockout risk" status model as ManagedCertificate
    (status: draft/success/error). The SEPARATE `register` action
    (AccountsController::registerAction(), `POST
    /acmeclient/accounts/register/{uuid}`) is a REAL, blocking network
    call to the configured external CA — this is what needs the two-step
    UI warning gate (see AcmeActionConfirm.tsx / README), not the local
    add/set. register_status/register_message/register_at record the last
    attempt's outcome for display; they are NOT re-derived from OPNsense
    (there is no "is this account registered" flag on the model itself to
    poll — see acme_service.py)."""

    __tablename__ = "acme_accounts"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    ca: Mapped[str] = mapped_column(String(32), default="letsencrypt")
    custom_ca: Mapped[str] = mapped_column(String(255), default="")
    eab_kid: Mapped[str] = mapped_column(String(255), default="")
    eab_hmac_set: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=True)

    opnsense_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Excludes eab_hmac's actual value (see acme_secret_store.py) — a
    # boolean flag stands in for it so no-op-push detection still notices
    # when the secret changes without ever persisting it.
    last_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    deployed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    register_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    register_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    register_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc)
    )

    host: Mapped["OpnsenseHost"] = relationship("OpnsenseHost")
