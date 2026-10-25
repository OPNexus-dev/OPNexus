from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import leader
from app.config import settings
from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.services import ha_service, rollout_engine
from app.services import update_check_service, updater_client

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/version-check")
async def version_check(db: AsyncSession = Depends(get_db)) -> dict:
    """Ist eine neuere OPNexus-Version veröffentlicht? Aus, solange
    UPDATE_CHECK_URL nicht gesetzt ist (dann ohne ausgehende Anfrage)."""
    return await update_check_service.check(db)


def _admin(user: AdminUser) -> AdminUser:
    # Der Updater ist Administration der Installation selbst — auch das reine
    # Lesen (Logs, Rollback-Optionen) bleibt Admins vorbehalten.
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)
    return user


def _major(version: str | None) -> int | None:
    try:
        return int((version or "").split(".")[0])
    except ValueError:
        return None


class UpdateCheckRead(BaseModel):
    enabled: bool
    feed_url: str
    managed_by_server: bool


class UpdateCheckUpdate(BaseModel):
    enabled: bool | None = None
    feed_url: str | None = Field(default=None, max_length=500)


async def _update_check_settings(db: AsyncSession) -> UpdateCheckRead:
    row = await update_check_service.get_settings(db)
    managed = update_check_service.managed_by_server()
    return UpdateCheckRead(
        enabled=True if managed else row.enabled,
        feed_url=settings.update_check_url.strip() if managed else row.feed_url,
        managed_by_server=managed,
    )


@router.get("/update-check", response_model=UpdateCheckRead)
async def read_update_check(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    """Versionsprüfung für „Einstellungen › OPNexus aktualisieren“ (nur Admins)."""
    _admin(user)
    return await _update_check_settings(db)


@router.put("/update-check", response_model=UpdateCheckRead)
async def update_update_check(payload: UpdateCheckUpdate, db: AsyncSession = Depends(get_db),
                              user: AdminUser = Depends(current_user)):
    _admin(user)
    if update_check_service.managed_by_server():
        raise api_error(409, ErrorCode.UPDATE_CHECK_MANAGED_BY_SERVER)
    row = await update_check_service.get_settings(db)
    if payload.feed_url is not None:
        url = payload.feed_url.strip()
        if not update_check_service.valid_feed_url(url):
            raise api_error(422, ErrorCode.UPDATE_CHECK_URL_INVALID)
        row.feed_url = url
    if payload.enabled is not None:
        row.enabled = payload.enabled
    await db.commit()
    update_check_service.reset_cache()
    return await _update_check_settings(db)


class UpdateRequest(BaseModel):
    version: str = Field(min_length=5, max_length=20)


@router.get("/updater")
async def updater_status(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)) -> dict:
    """Zustand für den Abschnitt "OPNexus aktualisieren" in den Einstellungen:
    Versionshinweis des Release-Feeds plus (falls aktiviert) der Updater."""
    _admin(user)
    check = await update_check_service.check(db)
    result: dict = {
        "available": False, "reason": None, "mode": None,
        "current": check["current"], "latest": check["latest"], "update_available": check["update_available"],
        "release_url": check["release_url"], "feed_enabled": check["enabled"],
        "feed_error": check["error"], "checked_at": check["checked_at"],
        "can_update": False, "major_change": False,
        "job": None, "phases": [], "log": [], "services": {},
        "rollback_available": False, "rollback_loses_data": False, "rollback_version": None,
        # A restart in the middle of a firmware rollout is safe (its state is in the database), but the
        # UI warns: the run pauses its 15-second steps for as long as OPNexus is down.
        "active_rollouts": await rollout_engine.count_active(db),
        # One node of an HA pair: click updates are refused by the updater, the UI points to `opnexus ha update`.
        "ha": ha_service.configured(),
    }
    try:
        updater = await updater_client.status()
    except updater_client.UpdaterUnavailable as exc:
        result["reason"] = str(exc)
        # Without the updater the install mode is still known from the image registry setting; the UI uses it
        # to not offer enabling the updater on an install that builds from source (it could not update anyway).
        result["mode"] = "release" if settings.opnexus_image_repo.strip() else "local"
        return result
    except updater_client.UpdaterRejected:
        result["reason"] = "unreachable"
        return result

    mode = updater.get("mode")
    running = (updater.get("job") or {}).get("state") == "running"
    result.update(
        available=True, mode=mode, job=updater.get("job"), phases=updater.get("phases") or [],
        log=updater.get("log") or [], services=updater.get("services") or {},
        rollback_available=bool(updater.get("rollback_available")),
        rollback_loses_data=bool(updater.get("rollback_loses_data")),
        rollback_version=((updater.get("last_update") or {}).get("prev_version")),
    )
    # Hauptversionssprünge nur bewusst über die Kommandozeile (Release-Notes lesen).
    result["major_change"] = bool(check["latest"]) and _major(check["latest"]) != _major(check["current"])
    result["can_update"] = (
        mode == "release" and not running and bool(check["update_available"]) and not result["major_change"]
        and not result["ha"] and not updater.get("ha_role")
    )
    return result


@router.post("/updater/update", status_code=202)
async def updater_update(payload: UpdateRequest, db: AsyncSession = Depends(get_db),
                         user: AdminUser = Depends(current_user)) -> dict:
    """Aktualisiert OPNexus auf die vom Release-Feed gemeldete neueste stabile
    Version. Beliebige Versionen werden bewusst nicht angenommen."""
    _admin(user)
    check = await update_check_service.check(db)
    if not check["update_available"] or payload.version.lstrip("v") != check["latest"]:
        raise api_error(409, ErrorCode.UPDATE_NOT_OFFERED)
    if _major(check["latest"]) != _major(check["current"]):
        raise api_error(409, ErrorCode.UPDATE_MAJOR_CHANGE)
    return await _forward(updater_client.start_update(check["latest"]))


@router.post("/updater/rollback", status_code=202)
async def updater_rollback(user: AdminUser = Depends(current_user)) -> dict:
    _admin(user)
    return await _forward(updater_client.start_rollback())


async def _forward(call) -> dict:
    try:
        return await call
    except updater_client.UpdaterUnavailable:
        raise api_error(503, ErrorCode.UPDATER_UNAVAILABLE)
    except updater_client.UpdaterRejected as rejected:
        if rejected.code == "busy":
            raise api_error(409, ErrorCode.UPDATER_BUSY)
        raise api_error(409, ErrorCode.UPDATER_REJECTED, reason=rejected.code)


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


@router.get("/ha")
async def ha_status(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)) -> dict:
    """Read-only state of the HA pair for the card in Settings › System (no promote button on purpose: fencing the old
    primary needs control over that host, so a switch is done with `opnexus ha promote`)."""
    _admin(user)
    if not ha_service.configured():
        return {"configured": False}
    snap = await ha_service.collect(db)
    probe = leader.http_partner(settings.ha_peer_ip)
    view = await probe() if probe else None
    now = ha_service._now()
    gate = leader.gate
    return {
        "configured": True,
        "node": settings.ha_node,
        "peer_ip": settings.ha_peer_ip,
        "vip": settings.ha_vip.split("/")[0] or None,
        "db_role": snap.db_role,
        "backend_role": leader.current_role(),
        "split_brain": bool(gate is not None and gate.split_brain),
        "epoch": snap.epoch,
        "primary_node": snap.primary_node,
        "promoted_at": _iso(snap.promoted_at),
        "last_sync_at": _iso(snap.last_sync_at),
        "sync_age_seconds": None if snap.last_sync_at is None else max(0, int((now - snap.last_sync_at).total_seconds())),
        "replication": {
            "state": snap.replication_state, "lag_bytes": snap.replication_lag_bytes,
            "lag_seconds": snap.replication_lag_seconds,
        },
        "slot": {
            "present": snap.slot_present, "active": snap.slot_active, "wal_status": snap.slot_wal_status,
            "retained_bytes": snap.slot_retained_bytes, "limit_bytes": snap.slot_limit_bytes,
        },
        # The standby runs no proxy, so it only answers while it is a full node (a primary, a fenced node).
        "peer": {"reachable": view is not None, "role": view[0] if view else None, "epoch": view[1] if view else None},
    }
