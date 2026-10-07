"""Pushes a centrally-authored Trust-Store certificate (`ManagedCertificate`,
v1 scope, see the model's own docstring) to its single target host — no
multi-host targeting (host-local `caref`/uuid, see the plan and
ManagedCertificate's docstring), no restart/apply step (Trust-Store writes
are immediately live — confirmed in the plan, `opnsense/core#9399`).

VERIFIED FIELDS / ENDPOINTS (fetched directly from raw.githubusercontent.
com/opnsense/core/master/..., 2026-09-09 — see opnsense_client.py's
"Trust-Store certificates" section for the endpoint list and citations):

- Trust/Cert.xml: the DescriptionField wire key is `descr` (this app's own
  `description` column is translated here, NOT sent verbatim under that
  name). Plaintext-in fields are `crt_payload`/`csr_payload`/`prv_payload`
  — CertController::setBaseHook() base64-encodes these server-side into
  `crt`/`csr`/`prv`; this app always sends plain PEM/CSR text.
  `private_key_location` (Required, Default "firewall") is deliberately
  NEVER sent by this app — relying on the model default means OPNsense
  itself generates AND stores the private key server-side for
  action=internal/reissue, so this app never needs to capture key
  material for those two actions at all. The ONE case this app DOES
  manage key material for is action="import", where the operator brings
  their own key (see cert_key_store.py) — sent as `prv_payload` at push
  time only, in a wire-only shallow copy, never persisted to Postgres
  (not even in `last_payload` — see cert_key_store.py's module docstring
  for why this deliberately does NOT follow the pre-existing IPsec/
  OpenVPN/WireGuard precedent of embedding secret material there).

DELIBERATELY SIMPLER STATUS MODEL THAN every multi-host entity in this
app: "draft" (never pushed) / "success" / "error" — there is no
reconfigure/apply/restart step to separately fail on, so there is no
"config written but reload unconfirmed" middle state either. No warning
gate exists in the frontend for this entity (see README) — the plan is
explicit that no lockout-shaped risk applies here.

Deleting an in-use certificate is blocked by OPNsense itself
(checkAndThrowValueInUse() inside CertController::delAction(), confirmed
reading the controller source directly) — this app does NOT pre-check
that condition; delete_certificate() below lets OpnsenseAPIError (which
will carry OPNsense's own "<module> - <description> {<reference>}"
message) propagate uncaught to the router, which surfaces it verbatim."""
import hashlib
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.managed_certificate import ManagedCertificate
from app.services import cert_key_store, opnsense_client
from app.services.opnsense_client import OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_payload(cert: ManagedCertificate) -> dict:
    """The OPNsense-shaped payload, EXCLUDING private key material (see
    module docstring) — this is what gets persisted as `last_payload` for
    no-op-push comparison.

    SECURITY FIX (2026-09-09): originally used the plain
    `private_key_set` boolean as a change-detection stand-in. That's
    insufficient — the flag stays `True` before AND after a key
    *rotation* (only the disk-stored content changes), so re-uploading a
    new key for an already-`success` certificate would have silently
    no-op'd and never actually pushed the new key to the firewall, while
    the UI kept showing "success". Fixed by hashing the current secret's
    content (SHA-256) instead: the hash changes on rotation, still never
    exposes the recoverable key itself in Postgres."""
    key_hash = ""
    if cert.action == "import" and cert.private_key_set:
        secret = cert_key_store.read_secret(cert.id) or ""
        key_hash = hashlib.sha256(secret.encode()).hexdigest() if secret else ""
    return {
        "action": cert.action,
        "descr": cert.description,
        "key_type": cert.key_type,
        "digest": cert.digest,
        "cert_type": cert.cert_type,
        "lifetime": str(cert.lifetime),
        "caref": cert.caref or "",
        "commonname": cert.commonname,
        "country": cert.country,
        "state": cert.state,
        "city": cert.city,
        "organization": cert.organization,
        "email": cert.email,
        "csr_payload": cert.csr_payload or "",
        "crt_payload": cert.crt_payload or "",
        "private_key_hash": key_hash,
    }


async def push_certificate(db: AsyncSession, cert: ManagedCertificate, host: OpnsenseHost) -> ManagedCertificate:
    payload = build_payload(cert)
    if cert.status == "success" and cert.last_payload == payload:
        # Nothing actually changed — re-affirm without touching the
        # firewall at all, same no-op rule as every other deploy service
        # in this app.
        cert.message = None
        cert.deployed_at = _now()
        await db.commit()
        return cert

    wire = {k: v for k, v in payload.items() if k != "private_key_hash"}
    if cert.action == "import" and cert.private_key_set:
        wire["prv_payload"] = cert_key_store.read_secret(cert.id) or ""

    try:
        if cert.opnsense_uuid:
            result = await opnsense_client.set_trust_cert(host, cert.opnsense_uuid, wire)
        else:
            result = await opnsense_client.add_trust_cert(host, wire)
            cert.opnsense_uuid = result.get("uuid") if isinstance(result, dict) else None
            if not cert.opnsense_uuid:
                raise OpnsenseAPIError("Firewall hat keine Zertifikat-UUID zurückgegeben; Stand manuell prüfen")
    except OpnsenseAPIError as exc:
        cert.status = "error"
        cert.message = str(exc)
        cert.deployed_at = _now()
        await db.commit()
        return cert

    cert.status = "success"
    cert.message = None
    cert.last_payload = payload
    cert.deployed_at = _now()
    await db.commit()
    return cert


async def delete_certificate(db: AsyncSession, cert: ManagedCertificate, host: OpnsenseHost | None) -> None:
    """Explicit action, not gated by anything (no lockout state to gate
    on for this entity). If a push was ever made (opnsense_uuid set) AND
    the host still exists, deletes it there first — OpnsenseAPIError
    (including OPNsense's own "in use" message) propagates uncaught, same
    as every del* flow in this app; the caller decides the HTTP status."""
    if host is not None and cert.opnsense_uuid:
        await opnsense_client.del_trust_cert(host, cert.opnsense_uuid)
    cert_key_store.delete_secret(cert.id)
    await db.delete(cert)
    await db.commit()
