from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.errors import ErrorCode, api_error
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.services import update_check_service, updater_client

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/version-check")
async def version_check() -> dict:
    """Ist eine neuere OPNexus-Version veröffentlicht? Aus, solange
    UPDATE_CHECK_URL nicht gesetzt ist (dann ohne ausgehende Anfrage)."""
    return await update_check_service.check()


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


class UpdateRequest(BaseModel):
    version: str = Field(min_length=5, max_length=20)


@router.get("/updater")
async def updater_status(user: AdminUser = Depends(current_user)) -> dict:
    """Zustand für den Abschnitt "OPNexus aktualisieren" in den Einstellungen:
    Versionshinweis des Release-Feeds plus (falls aktiviert) der Updater."""
    _admin(user)
    check = await update_check_service.check()
    result: dict = {
        "available": False, "reason": None, "mode": None,
        "current": check["current"], "latest": check["latest"], "update_available": check["update_available"],
        "release_url": check["release_url"], "feed_enabled": check["enabled"],
        "feed_error": check["error"], "checked_at": check["checked_at"],
        "can_update": False, "major_change": False,
        "job": None, "phases": [], "log": [], "services": {},
        "rollback_available": False, "rollback_loses_data": False, "rollback_version": None,
    }
    try:
        updater = await updater_client.status()
    except updater_client.UpdaterUnavailable as exc:
        result["reason"] = str(exc)
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
    )
    return result


@router.post("/updater/update", status_code=202)
async def updater_update(payload: UpdateRequest, user: AdminUser = Depends(current_user)) -> dict:
    """Aktualisiert OPNexus auf die vom Release-Feed gemeldete neueste stabile
    Version. Beliebige Versionen werden bewusst nicht angenommen."""
    _admin(user)
    check = await update_check_service.check()
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
