"""Scheduled backups: status for everybody, settings and offsite test for admins, "run now" for operators."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.schemas.backup_settings import BackupSettingsRead, BackupSettingsUpdate, BackupStatusRead
from app.services import backup_diff_service, backup_store
from app.services import backup_schedule_service as svc
from app.services import notification_secret_store
from app.services.s3_client import S3Error

router = APIRouter(tags=["backups"])


def _require_admin(user: AdminUser) -> None:
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)


@router.get("/backups/status", response_model=BackupStatusRead)
async def status(db: AsyncSession = Depends(get_db)):
    return await svc.get_settings(db)


@router.post("/backups/run")
async def run_now(db: AsyncSession = Depends(get_db)):
    """Back up every enabled host now (and copy offsite if configured)."""
    return await svc.run_due(db, force=True)


@router.get("/settings/backups", response_model=BackupSettingsRead)
async def read(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    return await svc.get_settings(db)


@router.put("/settings/backups", response_model=BackupSettingsRead)
async def update(payload: BackupSettingsUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    _require_admin(user)
    row = await svc.get_settings(db)
    for field in ("scheduled_enabled", "interval_hours", "keep_count", "offsite_enabled", "s3_region", "s3_path_style"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    for field in ("s3_endpoint", "s3_bucket", "s3_access_key", "s3_prefix"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value.strip().rstrip("/") if field == "s3_endpoint" else value.strip())
    if payload.clear_s3_secret:
        notification_secret_store.delete_secret(svc.S3_SECRET)
        row.s3_secret_set = False
    elif payload.s3_secret_key is not None:
        notification_secret_store.save_secret(svc.S3_SECRET, payload.s3_secret_key)
        row.s3_secret_set = True
    if payload.passphrase is not None:
        notification_secret_store.save_secret(svc.PASSPHRASE, payload.passphrase)
        row.passphrase_set = True
    await db.commit()
    return row


@router.post("/settings/backups/test-offsite")
async def test_offsite(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    """Write, read back and delete a probe object in the bucket."""
    _require_admin(user)
    cfg = await svc.get_settings(db)
    try:
        key = "/".join(p for p in (cfg.s3_prefix.strip("/"), f".opnexus-test-{int(datetime.now(timezone.utc).timestamp())}") if p)
        await svc.s3_for(cfg).test(key)
    except S3Error as exc:
        raise api_error(502, ErrorCode.OFFSITE_TEST_FAILED, reason=str(exc)) from exc
    return {"success": True}


@router.get("/hosts/{host_id}/backup-diff")
async def backup_diff(
    host_id: uuid.UUID,
    a: str | None = Query(default=None, description="Older backup id (default: the one before b)"),
    b: str | None = Query(default=None, description="Newer backup id (default: the newest)"),
    db: AsyncSession = Depends(get_db),
):
    """What changed between two stored backups of this host. Secrets are replaced by hashes."""
    if await db.get(OpnsenseHost, host_id) is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    snapshots = [s["id"] for s in backup_store.list_snapshots(host_id)]  # newest first
    new_id = b or (snapshots[0] if snapshots else None)
    old_id = a or (snapshots[snapshots.index(new_id) + 1] if new_id in snapshots and snapshots.index(new_id) + 1 < len(snapshots) else None)
    if not new_id or not old_id:
        raise api_error(404, ErrorCode.BACKUP_NICHT_GEFUNDEN)
    old, new = backup_store.read_snapshot(host_id, old_id), backup_store.read_snapshot(host_id, new_id)
    if old is None or new is None:
        raise api_error(404, ErrorCode.BACKUP_NICHT_GEFUNDEN)
    try:
        result = backup_diff_service.diff(old, new)
    except backup_diff_service.InvalidConfig as exc:
        raise api_error(422, ErrorCode.BACKUP_UNLESBAR, reason=str(exc)) from exc
    return {"a": old_id, "b": new_id, **result}
