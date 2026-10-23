"""Trust-Store (`ManagedCertificate`) + ACME (`AcmeAccount`,
`AcmeCertificateRequest`) management routers. Deliberately simpler shape
than ipsec.py/openvpn.py/wireguard.py: single-host (no target
resolution), no preview/revision/workflow machinery (no multi-host
no-op/drift concept applies to a single fixed host), and — for
Trust-Store only — no deploy warning gate (see managed_certificate_
service.py's module docstring for why). ACME's `register`/`sign` DO get
a distinct warning in the frontend (AcmeActionConfirm.tsx) even though
the backend endpoints here are plain POSTs — the two-step confirmation is
a UI-level gate, the same architectural choice ipsec.py/openvpn.py/
wireguard.py make for their own "push" action.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.acme_account import AcmeAccount
from app.models.acme_certificate_request import AcmeCertificateRequest
from app.models.host import OpnsenseHost
from app.models.managed_certificate import ManagedCertificate
from app.schemas.certificates import (
    AcmeAccountCreate, AcmeAccountEdit, AcmeAccountRead,
    AcmeCertificateRequestCreate, AcmeCertificateRequestEdit, AcmeCertificateRequestRead,
    ManagedCertificateCreate, ManagedCertificateEdit, ManagedCertificateRead,
)
from app.services import acme_secret_store, acme_service, cert_key_store, managed_certificate_service
from app.services.host_maintenance_service import ensure_no_maintenance
from app.services.opnsense_client import OpnsenseAPIError
from app.services.policy_service import policy_lock

managed_router = APIRouter(prefix="/certificates/managed", tags=["certificates"])
acme_accounts_router = APIRouter(prefix="/acme/accounts", tags=["acme"])
acme_certificates_router = APIRouter(prefix="/acme/certificates", tags=["acme"])


def _ensure_host_unchanged_once_deployed(obj, new_host_id: uuid.UUID) -> None:
    """The firewall-local UUID, the stored payload and the success status all belong to the host the object was pushed
    to. Re-pointing it would report success for a host that never received it and orphan the object on the old one."""
    if new_host_id != obj.host_id and obj.opnsense_uuid:
        raise api_error(409, ErrorCode.ZIEL_HOST_NACH_AUSROLLEN_FEST)


async def _get_host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(400, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


async def _get_enabled_host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await _get_host(db, host_id)
    if not host.enabled:
        raise api_error(400, ErrorCode.HOST_IST_DEAKTIVIERT)
    return host


# ── Trust-Store certificates ──────────────────────────────────────────
@managed_router.get("", response_model=list[ManagedCertificateRead])
async def list_managed_certificates(db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(ManagedCertificate).order_by(ManagedCertificate.created_at.desc()))).all()
    return rows


@managed_router.post("", response_model=ManagedCertificateRead, status_code=201)
async def create_managed_certificate(payload: ManagedCertificateCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    await _get_host(db, payload.host_id)
    data = payload.model_dump(exclude={"private_key"})
    cert = ManagedCertificate(**data)
    db.add(cert)
    await db.flush()
    if payload.private_key:
        cert_key_store.save_secret(cert.id, payload.private_key)
        cert.private_key_set = True
    await db.commit()
    return cert


@managed_router.put("/{cert_id}", response_model=ManagedCertificateRead)
async def edit_managed_certificate(cert_id: uuid.UUID, payload: ManagedCertificateEdit, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    cert = await db.get(ManagedCertificate, cert_id)
    if not cert:
        raise api_error(404, ErrorCode.ZERTIFIKAT_NICHT_GEFUNDEN)
    if cert.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.EINTRAG_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_B5A5CE87)
    await _get_host(db, payload.host_id)
    _ensure_host_unchanged_once_deployed(cert, payload.host_id)
    for field, value in payload.model_dump(exclude={"expected_updated_at", "private_key"}).items():
        setattr(cert, field, value)
    if payload.private_key:
        cert_key_store.save_secret(cert.id, payload.private_key)
        cert.private_key_set = True
    await db.commit()
    return cert


@managed_router.post("/{cert_id}/push", response_model=ManagedCertificateRead)
async def push_managed_certificate(cert_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    cert = await db.get(ManagedCertificate, cert_id)
    if not cert:
        raise api_error(404, ErrorCode.ZERTIFIKAT_NICHT_GEFUNDEN)
    host = await _get_enabled_host(db, cert.host_id)
    await ensure_no_maintenance(db, [host])
    return await managed_certificate_service.push_certificate(db, cert, host)


@managed_router.delete("/{cert_id}", status_code=204)
async def delete_managed_certificate(cert_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    cert = await db.get(ManagedCertificate, cert_id)
    if not cert:
        raise api_error(404, ErrorCode.ZERTIFIKAT_NICHT_GEFUNDEN)
    host = await db.get(OpnsenseHost, cert.host_id)
    try:
        await managed_certificate_service.delete_certificate(db, cert, host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc


# ── ACME accounts ──────────────────────────────────────────────────────
@acme_accounts_router.get("", response_model=list[AcmeAccountRead])
async def list_acme_accounts(db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(AcmeAccount).order_by(AcmeAccount.created_at.desc()))).all()
    return rows


@acme_accounts_router.post("", response_model=AcmeAccountRead, status_code=201)
async def create_acme_account(payload: AcmeAccountCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    await _get_host(db, payload.host_id)
    data = payload.model_dump(exclude={"eab_hmac"})
    account = AcmeAccount(**data)
    db.add(account)
    await db.flush()
    if payload.eab_hmac:
        acme_secret_store.save_secret(account.id, payload.eab_hmac)
        account.eab_hmac_set = True
    await db.commit()
    return account


@acme_accounts_router.put("/{account_id}", response_model=AcmeAccountRead)
async def edit_acme_account(account_id: uuid.UUID, payload: AcmeAccountEdit, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    account = await db.get(AcmeAccount, account_id)
    if not account:
        raise api_error(404, ErrorCode.ACCOUNT_NICHT_GEFUNDEN)
    if account.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.EINTRAG_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_B5A5CE87)
    await _get_host(db, payload.host_id)
    _ensure_host_unchanged_once_deployed(account, payload.host_id)
    if payload.host_id != account.host_id and await db.scalar(
        select(AcmeCertificateRequest.id).where(AcmeCertificateRequest.account_id == account.id).limit(1)
    ):
        raise api_error(409, ErrorCode.ACME_KONTO_WIRD_VON_ANFRAGEN_VERWENDET)
    for field, value in payload.model_dump(exclude={"expected_updated_at", "eab_hmac"}).items():
        setattr(account, field, value)
    if payload.eab_hmac:
        acme_secret_store.save_secret(account.id, payload.eab_hmac)
        account.eab_hmac_set = True
    await db.commit()
    return account


@acme_accounts_router.post("/{account_id}/push", response_model=AcmeAccountRead)
async def push_acme_account(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    account = await db.get(AcmeAccount, account_id)
    if not account:
        raise api_error(404, ErrorCode.ACCOUNT_NICHT_GEFUNDEN)
    host = await _get_enabled_host(db, account.host_id)
    await ensure_no_maintenance(db, [host])
    return await acme_service.push_account(db, account, host)


@acme_accounts_router.post("/{account_id}/register", response_model=AcmeAccountRead)
async def register_acme_account(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """REAL, blocking network call to the configured external CA — see
    acme_service.register_account()'s docstring. The frontend gates this
    behind an explicit two-step warning (AcmeActionConfirm.tsx); this
    endpoint itself fires immediately on request, same as ipsec.py's
    deploy endpoint firing immediately once the frontend has armed it."""
    account = await db.get(AcmeAccount, account_id)
    if not account:
        raise api_error(404, ErrorCode.ACCOUNT_NICHT_GEFUNDEN)
    if not account.opnsense_uuid:
        raise api_error(400, ErrorCode.ACCOUNT_MUSS_ZUERST_AUF_DIE_FIREWALL_GESCHRIEBEN_WERDEN_EEEA04A9)
    host = await _get_enabled_host(db, account.host_id)
    try:
        return await acme_service.register_account(db, account, host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc


@acme_accounts_router.delete("/{account_id}", status_code=204)
async def delete_acme_account(account_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    account = await db.get(AcmeAccount, account_id)
    if not account:
        raise api_error(404, ErrorCode.ACCOUNT_NICHT_GEFUNDEN)
    if await db.scalar(select(AcmeCertificateRequest.id).where(AcmeCertificateRequest.account_id == account_id).limit(1)):
        raise api_error(409, ErrorCode.ACCOUNT_WIRD_NOCH_VON_MINDESTENS_EINER_ZERTIFIKATSANFRAGE_REFERENZIERT)
    host = await db.get(OpnsenseHost, account.host_id)
    try:
        await acme_service.delete_account(db, account, host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc


# ── ACME certificate requests ─────────────────────────────────────────
@acme_certificates_router.get("", response_model=list[AcmeCertificateRequestRead])
async def list_acme_certificate_requests(db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(AcmeCertificateRequest).order_by(AcmeCertificateRequest.created_at.desc()))).all()
    return rows


async def _get_account(db: AsyncSession, account_id: uuid.UUID, host_id: uuid.UUID) -> AcmeAccount:
    account = await db.get(AcmeAccount, account_id)
    if not account:
        raise api_error(400, ErrorCode.ACME_ACCOUNT_NICHT_GEFUNDEN)
    if account.host_id != host_id:
        raise api_error(400, ErrorCode.ACME_ACCOUNT_GEHOERT_ZU_EINEM_ANDEREN_HOST)
    return account


@acme_certificates_router.post("", response_model=AcmeCertificateRequestRead, status_code=201)
async def create_acme_certificate_request(payload: AcmeCertificateRequestCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    await _get_host(db, payload.host_id)
    await _get_account(db, payload.account_id, payload.host_id)
    req = AcmeCertificateRequest(**payload.model_dump())
    db.add(req)
    await db.commit()
    return req


@acme_certificates_router.put("/{request_id}", response_model=AcmeCertificateRequestRead)
async def edit_acme_certificate_request(request_id: uuid.UUID, payload: AcmeCertificateRequestEdit, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    req = await db.get(AcmeCertificateRequest, request_id)
    if not req:
        raise api_error(404, ErrorCode.ZERTIFIKATSANFRAGE_NICHT_GEFUNDEN)
    if req.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.EINTRAG_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_B5A5CE87)
    await _get_host(db, payload.host_id)
    _ensure_host_unchanged_once_deployed(req, payload.host_id)
    await _get_account(db, payload.account_id, payload.host_id)
    for field, value in payload.model_dump(exclude={"expected_updated_at"}).items():
        setattr(req, field, value)
    await db.commit()
    return req


@acme_certificates_router.post("/{request_id}/push", response_model=AcmeCertificateRequestRead)
async def push_acme_certificate_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    req = await db.get(AcmeCertificateRequest, request_id)
    if not req:
        raise api_error(404, ErrorCode.ZERTIFIKATSANFRAGE_NICHT_GEFUNDEN)
    host = await _get_enabled_host(db, req.host_id)
    account = await db.get(AcmeAccount, req.account_id)
    if not account:
        raise api_error(400, ErrorCode.VERKNUEPFTER_ACME_ACCOUNT_NICHT_GEFUNDEN)
    await ensure_no_maintenance(db, [host])
    return await acme_service.push_certificate_request(db, req, host, account)


@acme_certificates_router.post("/{request_id}/sign", response_model=AcmeCertificateRequestRead)
async def sign_acme_certificate_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """REAL, asynchronous network call to the configured external CA — see
    acme_service.sign_certificate_request()'s docstring. Frontend-gated
    the same way register_acme_account() above is."""
    req = await db.get(AcmeCertificateRequest, request_id)
    if not req:
        raise api_error(404, ErrorCode.ZERTIFIKATSANFRAGE_NICHT_GEFUNDEN)
    if not req.opnsense_uuid:
        raise api_error(400, ErrorCode.ZERTIFIKATSANFRAGE_MUSS_ZUERST_AUF_DIE_FIREWALL_GESCHRIEBEN_WERDEN_F684C449)
    host = await _get_enabled_host(db, req.host_id)
    try:
        return await acme_service.sign_certificate_request(db, req, host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc


@acme_certificates_router.post("/{request_id}/poll", response_model=AcmeCertificateRequestRead)
async def poll_acme_certificate_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    req = await db.get(AcmeCertificateRequest, request_id)
    if not req:
        raise api_error(404, ErrorCode.ZERTIFIKATSANFRAGE_NICHT_GEFUNDEN)
    if not req.opnsense_uuid:
        raise api_error(400, ErrorCode.ZERTIFIKATSANFRAGE_WURDE_NOCH_NICHT_AUF_DIE_FIREWALL_GESCHRIEBEN)
    host = await _get_host(db, req.host_id)
    try:
        return await acme_service.poll_certificate_status(db, req, host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REQUEST_FAILED) from exc


@acme_certificates_router.delete("/{request_id}", status_code=204)
async def delete_acme_certificate_request(request_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    req = await db.get(AcmeCertificateRequest, request_id)
    if not req:
        raise api_error(404, ErrorCode.ZERTIFIKATSANFRAGE_NICHT_GEFUNDEN)
    host = await db.get(OpnsenseHost, req.host_id)
    try:
        await acme_service.delete_certificate_request(db, req, host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc
