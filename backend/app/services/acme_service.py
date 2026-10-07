"""Pushes centrally-authored ACME accounts/certificate-requests
(`AcmeAccount`, `AcmeCertificateRequest` — os-acme-client plugin, v1
scope) to their single target host, plus the two REAL external-network
action triggers (`register`/`sign`) — see the two models' own docstrings
for the full field-mapping writeup and why this entity is deliberately
per-host, not multi-host like IPsec/OpenVPN/WireGuard.

VERIFIED FIELDS / ENDPOINTS (fetched directly from raw.githubusercontent.
com/opnsense/plugins/master/security/acme-client/..., 2026-09-09 — see
opnsense_client.py's "ACME" section for the endpoint list and citations):

- AcmeClient.xml -> accounts.account: `ca` OptionValues letsencrypt/
  letsencrypt_test/zerossl/buypass/buypass_test/google/google_test/
  sslcom/custom. `eab_hmac` never touches Postgres (see
  acme_secret_store.py) — sent as a wire-only value at push time, exactly
  like ManagedCertificate's private key.
- AcmeClient.xml -> certificates.certificate: `altNames` is a
  comma-separated CSVListField (this app's `alt_names`, sent verbatim,
  no sub-array). `account` and `validationMethod` are BOTH Required
  ModelRelationFields. `keyLength` OptionValues are PREFIXED
  ("key_4096" etc, not "4096") — see AcmeCertificateRequest's docstring
  for why this matters. `renewInterval` is DAYS SINCE LAST ISSUANCE, not
  days-before-expiry.
- AccountsController.php / CertificatesController.php: addAction/
  updateAction (NOT setAction — explicitly confirmed different from
  Trust-Store's setAction and every VPN feature's setX naming, per the
  plan's own warning)/delAction/getAction, all confirmed literal PHP
  method names. registerAction($uuid) / signAction($uuid) both return
  `{"result": "failed"}` immediately if the uuid doesn't resolve, else
  `{"response": "<raw configdRun() text>"}` — parsed by
  opnsense_client._acme_action_result(), NOT `_mutation_result` (see that
  module's own citations). registerAction is a REAL, blocking network
  call to the external CA; signAction is the same shape but its
  underlying configd action is documented to fork and return
  immediately — the trigger response is proof-of-acceptance only, never
  proof of actual issuance (see poll_certificate_status() below).

UUID-INJECTION FOR AcmeCertificateRequest.account — mirrors
ipsec_deploy_service.py's `connection`/`local_wire`/`remote_wire`/
`child_wire` pattern EXACTLY, including the SAME no-op-redeploy pitfall
it documents: `build_payload()` never includes the wire-level `account`
uuid (that's deploy-time wiring, filled in fresh from the CALLER-supplied
AcmeAccount at push time, in a shallow copy, onto `last_payload` never).
Instead it includes a leading-underscore `_account_id` bookkeeping key
(THIS app's own FK, not the OPNsense uuid) purely so switching which
account a request is linked to is still detected as a real change to
push — without it, changing account_id alone (with every other field
unchanged) would look like a no-op and silently never re-push the
updated `account` wire value.

DELIBERATELY SIMPLER STATUS MODEL, same reasoning as
managed_certificate_service.py: "draft"/"success"/"error" for the local
add/set/del config write, no restart step to separately fail on. The
`register_status`/`sign_status` fields are a SEPARATE, orthogonal bit of
state — the outcome of the external-CA-reaching trigger actions, which
the UI gates behind an explicit two-step warning (see
AcmeActionConfirm.tsx / README) for a different reason than the VPN
features' no-rollback warning: external-CA-contact / rate-limit risk,
not connectivity loss."""
import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.acme_account import AcmeAccount
from app.models.acme_certificate_request import AcmeCertificateRequest
from app.models.host import OpnsenseHost
from app.services import acme_secret_store, opnsense_client
from app.services.opnsense_client import OpnsenseAPIError


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_epoch_datetime(value: Any) -> datetime | None:
    ts = _to_int(value)
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


# ── Accounts ──────────────────────────────────────────────────────────
def build_account_payload(account: AcmeAccount) -> dict:
    """SECURITY FIX (2026-09-09): originally used the plain `eab_hmac_set`
    boolean as a change-detection stand-in for no-op-push comparison —
    insufficient for the same reason ManagedCertificate's private-key flag
    was (see managed_certificate_service.py's build_payload() docstring):
    the flag stays `True` across a rotation, so a new EAB HMAC value would
    silently never get pushed. Hash the current secret's content instead —
    changes on rotation, never exposes the recoverable value in Postgres."""
    secret = acme_secret_store.read_secret(account.id) or "" if account.eab_hmac_set else ""
    return {
        "enabled": "1" if account.enabled else "0",
        "name": account.name,
        "description": account.description,
        "email": account.email,
        "ca": account.ca,
        "custom_ca": account.custom_ca,
        "eab_kid": account.eab_kid,
        "eab_hmac_hash": hashlib.sha256(secret.encode()).hexdigest() if secret else "",
    }


async def push_account(db: AsyncSession, account: AcmeAccount, host: OpnsenseHost) -> AcmeAccount:
    payload = build_account_payload(account)
    if account.status == "success" and account.last_payload == payload:
        account.message = None
        account.deployed_at = _now()
        await db.commit()
        return account

    wire = {k: v for k, v in payload.items() if k != "eab_hmac_hash"}
    wire["eab_hmac"] = acme_secret_store.read_secret(account.id) or ""

    try:
        if account.opnsense_uuid:
            result = await opnsense_client.update_acme_account(host, account.opnsense_uuid, wire)
        else:
            result = await opnsense_client.add_acme_account(host, wire)
            account.opnsense_uuid = result.get("uuid") if isinstance(result, dict) else None
            if not account.opnsense_uuid:
                raise OpnsenseAPIError("Firewall hat keine Account-UUID zurückgegeben; Stand manuell prüfen")
    except OpnsenseAPIError as exc:
        account.status = "error"
        account.message = str(exc)
        account.deployed_at = _now()
        await db.commit()
        return account

    account.status = "success"
    account.message = None
    account.last_payload = payload
    account.deployed_at = _now()
    await db.commit()
    return account


async def delete_account(db: AsyncSession, account: AcmeAccount, host: OpnsenseHost | None) -> None:
    if host is not None and account.opnsense_uuid:
        await opnsense_client.del_acme_account(host, account.opnsense_uuid)
    acme_secret_store.delete_secret(account.id)
    await db.delete(account)
    await db.commit()


async def register_account(db: AsyncSession, account: AcmeAccount, host: OpnsenseHost) -> AcmeAccount:
    """Triggers the REAL, blocking `register-account` call against the
    configured external CA — see opnsense_client.register_acme_account().
    Never call this against anything but a respx mock / fully-mocked
    write path in tests/verification (see module docstring)."""
    if not account.opnsense_uuid:
        raise OpnsenseAPIError("Account wurde noch nicht auf die Firewall geschrieben")
    try:
        result = await opnsense_client.register_acme_account(host, account.opnsense_uuid)
    except OpnsenseAPIError as exc:
        account.register_status = "error"
        account.register_message = str(exc)
        account.register_at = _now()
        await db.commit()
        raise
    account.register_status = "ok"
    account.register_message = str(result.get("response", ""))[:2000]
    account.register_at = _now()
    await db.commit()
    return account


# ── Certificate requests ──────────────────────────────────────────────
def build_certificate_payload(req: AcmeCertificateRequest) -> dict:
    """Deliberately excludes the wire-level `account` uuid — see module
    docstring's UUID-injection section. `_account_id` is OPNexus-only
    bookkeeping, stripped before the wire send in
    push_certificate_request() below."""
    return {
        "enabled": "1" if req.enabled else "0",
        "name": req.name,
        "altNames": req.alt_names,
        "validationMethod": req.validation_ref,
        "keyLength": req.key_length,
        "ocsp": "1" if req.ocsp else "0",
        "autoRenewal": "1" if req.auto_renewal else "0",
        "renewInterval": str(req.renew_interval),
        "_account_id": str(req.account_id),
    }


async def push_certificate_request(
    db: AsyncSession, req: AcmeCertificateRequest, host: OpnsenseHost, account: AcmeAccount
) -> AcmeCertificateRequest:
    if not account.opnsense_uuid:
        req.status = "error"
        req.message = "Verknüpfter ACME-Account wurde noch nicht auf die Firewall geschrieben"
        req.deployed_at = _now()
        await db.commit()
        return req

    payload = build_certificate_payload(req)
    if req.status == "success" and req.last_payload == payload:
        req.message = None
        req.deployed_at = _now()
        await db.commit()
        return req

    wire = {k: v for k, v in payload.items() if not k.startswith("_")}
    wire["account"] = account.opnsense_uuid

    try:
        if req.opnsense_uuid:
            result = await opnsense_client.update_acme_certificate(host, req.opnsense_uuid, wire)
        else:
            result = await opnsense_client.add_acme_certificate(host, wire)
            req.opnsense_uuid = result.get("uuid") if isinstance(result, dict) else None
            if not req.opnsense_uuid:
                raise OpnsenseAPIError("Firewall hat keine Certificate-UUID zurückgegeben; Stand manuell prüfen")
    except OpnsenseAPIError as exc:
        req.status = "error"
        req.message = str(exc)
        req.deployed_at = _now()
        await db.commit()
        return req

    req.status = "success"
    req.message = None
    req.last_payload = payload
    req.deployed_at = _now()
    await db.commit()
    return req


async def delete_certificate_request(db: AsyncSession, req: AcmeCertificateRequest, host: OpnsenseHost | None) -> None:
    if host is not None and req.opnsense_uuid:
        await opnsense_client.del_acme_certificate(host, req.opnsense_uuid)
    await db.delete(req)
    await db.commit()


async def sign_certificate_request(db: AsyncSession, req: AcmeCertificateRequest, host: OpnsenseHost) -> AcmeCertificateRequest:
    """Triggers the REAL, ASYNCHRONOUS `sign-cert` call against the
    configured external CA. `sign_status="triggered"` is the ONLY thing
    this sets on success — it is deliberately NOT "success", because the
    trigger returning does not mean issuance succeeded (see module
    docstring). Never call this against anything but a respx mock /
    fully-mocked write path in tests/verification."""
    if not req.opnsense_uuid:
        raise OpnsenseAPIError("Zertifikatsanfrage wurde noch nicht auf die Firewall geschrieben")
    try:
        result = await opnsense_client.sign_acme_certificate(host, req.opnsense_uuid)
    except OpnsenseAPIError as exc:
        req.sign_status = "error"
        req.sign_message = str(exc)
        req.sign_triggered_at = _now()
        await db.commit()
        raise
    req.sign_status = "triggered"
    req.sign_message = str(result.get("response", ""))[:2000]
    req.sign_triggered_at = _now()
    await db.commit()
    return req


async def poll_certificate_status(db: AsyncSession, req: AcmeCertificateRequest, host: OpnsenseHost) -> AcmeCertificateRequest:
    """`GET /acmeclient/certificates/get/{uuid}` — refreshes
    remote_status_code/remote_status_last_update/remote_last_update from
    OPNsense's own model fields (statusCode/statusLastUpdate/lastUpdate).
    The only way to learn the real outcome of an async sign() trigger —
    call this after sign_certificate_request(), and again later/on
    refresh, per the plan (OPNexus never claims success from the trigger
    call alone)."""
    if not req.opnsense_uuid:
        raise OpnsenseAPIError("Zertifikatsanfrage wurde noch nicht auf die Firewall geschrieben")
    data = await opnsense_client.get_acme_certificate(host, req.opnsense_uuid)
    node = data.get("certificate", {}) if isinstance(data, dict) else {}
    req.remote_status_code = _to_int(node.get("statusCode"))
    req.remote_status_last_update = _to_epoch_datetime(node.get("statusLastUpdate"))
    req.remote_last_update = _to_epoch_datetime(node.get("lastUpdate"))
    await db.commit()
    return req
