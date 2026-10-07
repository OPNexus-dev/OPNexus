import uuid
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models import OpnsenseHost
from app.models.restore_operation import RestoreOperation
from app.services import backup_store, opnsense_client as client, audit_service, restore_service
from app.services.policy_service import policy_lock

router = APIRouter(prefix="/hosts/{host_id}/restore", tags=["restore"])

PREVIEW_LIFETIME = timedelta(minutes=15)


class Preview(BaseModel):
    backup_id: str = Field(min_length=1, max_length=255)


class Handoff(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=120)


class Confirm(BaseModel):
    confirm_name: str = Field(min_length=1, max_length=120)
    message: str | None = Field(default=None, max_length=1000)


async def host_or_404(db, host_id):
    h = await db.get(OpnsenseHost, host_id)
    if not h:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return h


async def op_or_404(db, host_id, operation_id):
    op = await db.get(RestoreOperation, operation_id)
    if not op or op.host_id != host_id:
        raise api_error(404, ErrorCode.RESTORE_VORGANG_NICHT_GEFUNDEN)
    return op


def read_operation(op: RestoreOperation) -> dict:
    return {
        "id": op.id, "host_id": op.host_id, "host_name": op.host_name, "actor": op.actor,
        "backup_id": op.backup_id, "status": op.status, "created_at": op.created_at,
        "expires_at": op.expires_at, "manifest": op.manifest, "warnings": op.warnings,
        "handed_off_at": op.handed_off_at, "confirmed_at": op.confirmed_at,
        "cancelled_at": op.cancelled_at, "message": op.message,
    }


@router.get("")
async def list_operations(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await host_or_404(db, host_id)
    ops = (await db.scalars(
        select(RestoreOperation)
        .where(RestoreOperation.host_id == host_id, RestoreOperation.status != "preview")
        .order_by(RestoreOperation.created_at.desc())
        .limit(20)
    )).all()
    return [read_operation(o) for o in ops]


@router.get("/{operation_id}")
async def get_operation(host_id: uuid.UUID, operation_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await host_or_404(db, host_id)
    op = await op_or_404(db, host_id, operation_id)
    return read_operation(op)


@router.post("/preview")
async def preview(host_id: uuid.UUID, payload: Preview, request: Request, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await host_or_404(db, host_id)

    # Best-effort fresh safety snapshot before previewing — same call the
    # existing "Jetzt sichern" button already makes. If it fails we still
    # let the preview proceed (the chosen backup is unaffected), just warn.
    safety_warning = None
    try:
        content = await client.download_backup(host)
        backup_store.save_snapshot(host_id, content)
    except client.OpnsenseAPIError as exc:
        safety_warning = f"Frische Sicherheitssicherung vor dem Restore fehlgeschlagen: {exc}"

    raw = backup_store.read_snapshot(host_id, payload.backup_id)
    if raw is None:
        raise api_error(404, ErrorCode.SICHERUNG_NICHT_GEFUNDEN)

    try:
        manifest = restore_service.parse_and_redact(raw)
    except restore_service.InvalidBackupError as exc:
        raise api_error(400, ErrorCode(exc.code), **exc.params) from exc

    warnings = restore_service.diff_against_host(manifest, host)
    if safety_warning:
        warnings.append(safety_warning)

    op = RestoreOperation(
        host_id=host.id, host_name=host.name, actor=request.state.user.username,
        backup_id=payload.backup_id, expires_at=datetime.now(timezone.utc) + PREVIEW_LIFETIME,
        fingerprint=restore_service.fingerprint(raw), manifest=manifest, warnings=warnings,
    )
    db.add(op)
    await db.commit()
    await audit_service.persist(
        kind="restore", action="preview", status="preview", host_id=str(host.id), host_name=host.name,
        entity_id=str(op.id), details={"backup_id": payload.backup_id, "manifest": manifest, "warnings": warnings},
    )
    return read_operation(op)


@router.post("/{operation_id}/handoff")
async def handoff(host_id: uuid.UUID, operation_id: uuid.UUID, payload: Handoff, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await host_or_404(db, host_id)
    op = await op_or_404(db, host_id, operation_id)
    if op.status != "preview":
        raise api_error(409, ErrorCode.KEIN_OFFENER_RESTORE_VORSCHAU_VORGANG)
    if op.expires_at <= datetime.now(timezone.utc):
        raise api_error(409, ErrorCode.VORSCHAU_ABGELAUFEN_BITTE_ERNEUT_OEFFNEN)
    if payload.confirm_name != host.name:
        raise api_error(409, ErrorCode.HOSTNAMEN_EXAKT_EINGEBEN)

    raw = backup_store.read_snapshot(host_id, op.backup_id)
    if raw is None or restore_service.fingerprint(raw) != op.fingerprint:
        raise api_error(409, ErrorCode.SICHERUNG_WURDE_SEIT_DER_VORSCHAU_VERAENDERT_ODER_IST_2732C4DD)

    op.status = "handed_off"
    op.handed_off_at = datetime.now(timezone.utc)
    op.confirm_name = payload.confirm_name
    op.message = (
        "Zur manuellen Wiederherstellung übergeben. Sicherung jetzt im OPNsense-Webinterface "
        "importieren (System → Konfiguration → Historie/Wiederherstellen), Interface-/"
        "Konsolenzuordnung prüfen, danach hier bestätigen."
    )
    await db.commit()
    await audit_service.persist(
        kind="restore", action="handoff", status="handed_off", host_id=str(host.id), host_name=host.name,
        entity_id=str(op.id), message=op.message,
    )
    return read_operation(op)


@router.post("/{operation_id}/confirm")
async def confirm(host_id: uuid.UUID, operation_id: uuid.UUID, payload: Confirm, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await host_or_404(db, host_id)
    op = await op_or_404(db, host_id, operation_id)
    if op.status != "handed_off":
        raise api_error(409, ErrorCode.KEIN_OFFENER_UEBERGEBENER_RESTORE_VORGANG)
    if payload.confirm_name != host.name:
        raise api_error(409, ErrorCode.HOSTNAMEN_EXAKT_EINGEBEN)

    op.status = "verified"
    op.confirmed_at = datetime.now(timezone.utc)
    op.message = (
        (payload.message or "").strip() or "Wiederherstellung manuell geprüft und bestätigt."
    ) + " Hinweis: Zentral verwaltete Regeln/Aliase/NAT für diesen Host vor dem nächsten Push neu abgleichen."
    await db.commit()
    await audit_service.persist(
        kind="restore", action="confirm", status="verified", host_id=str(host.id), host_name=host.name,
        entity_id=str(op.id), message=op.message,
    )
    return read_operation(op)


@router.post("/{operation_id}/cancel")
async def cancel(host_id: uuid.UUID, operation_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    host = await host_or_404(db, host_id)
    op = await op_or_404(db, host_id, operation_id)
    if op.status not in ("preview", "handed_off"):
        raise api_error(409, ErrorCode.VORGANG_KANN_NICHT_MEHR_ABGEBROCHEN_WERDEN)
    op.status = "cancelled"
    op.cancelled_at = datetime.now(timezone.utc)
    await db.commit()
    await audit_service.persist(
        kind="restore", action="cancel", status="cancelled", host_id=str(host.id), host_name=host.name,
        entity_id=str(op.id),
    )
    return read_operation(op)
