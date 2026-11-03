"""Pair management from the settings (docs/DESIGN-HA-UI.md section 5): what the card in Settings > System does on this node.

Everything here is administration of the installation itself (admin accounts only, like the whole /api/system area), and the steps that
hand data to another machine or take this one out of service ask for the account's password again: a stolen session must not be enough
to copy every firewall credential to a machine of the attacker's choosing. The work is done by the updater of this node (the agent);
the backend only validates, asks again and passes on.
"""
import logging
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.user import AdminUser
from app.routers.auth import _account_failure, _account_success, _check_account_budget, current_user
from app.services import auth_service, updater_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system/ha", tags=["ha"])

IPV4 = re.compile(r"^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$")
ACTIONS = ("sync", "switch", "rebuild", "dissolve", "dissolve_local")


def _admin(user: AdminUser) -> AdminUser:
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)
    return user


async def _reauth(user: AdminUser, password: str, action: str) -> None:
    """The account's own password once more (same budget and lock-out as the other security endpoints)."""
    if user.auth_source != "local":
        raise api_error(403, ErrorCode.HA_LOCAL_ACCOUNT_REQUIRED)
    _check_account_budget(user, action)
    if not await auth_service.verify_password_async(user.password_hash, password):
        _account_failure(user, action)
        raise api_error(401, ErrorCode.AKTUELLES_PASSWORT_IST_FALSCH)
    _account_success(user, action)


async def _call(coroutine):
    try:
        return await coroutine
    except updater_client.UpdaterUnavailable:
        raise api_error(503, ErrorCode.UPDATER_UNAVAILABLE)
    except updater_client.UpdaterRejected as rejected:
        mapped = {"busy": (409, ErrorCode.UPDATER_BUSY), "not_primary": (409, ErrorCode.HA_NOT_PRIMARY),
                  "already_in_pair": (409, ErrorCode.HA_ALREADY_IN_PAIR), "bad_request": (400, ErrorCode.HA_INPUT_INVALID),
                  "prepare_failed": (502, ErrorCode.HA_PREPARE_FAILED)}.get(rejected.code)
        if mapped:
            raise api_error(*mapped)
        raise api_error(409, ErrorCode.UPDATER_REJECTED, reason=rejected.code)


class SuggestRequest(BaseModel):
    target: str | None = Field(default=None, max_length=15)


@router.post("/suggest")
async def suggest(payload: SuggestRequest, user: AdminUser = Depends(current_user)) -> dict:
    """This node's own address and interface towards a target (default route): the wizard's starting values."""
    _admin(user)
    if payload.target and not IPV4.fullmatch(payload.target):
        raise api_error(400, ErrorCode.HA_INPUT_INVALID)
    return await _call(updater_client.ha_local_suggest(payload.target))


class PrepareRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    ip: str = Field(max_length=15)


@router.post("/prepare")
async def prepare(payload: PrepareRequest, user: AdminUser = Depends(current_user)) -> dict:
    """Make this node the future standby: switch the gate on and show a pairing code (30 minutes, single use).
    The code is returned once and written nowhere."""
    _admin(user)
    if not IPV4.fullmatch(payload.ip):
        raise api_error(400, ErrorCode.HA_INPUT_INVALID)
    await _reauth(user, payload.password, "ha-prepare")
    logger.info("HA: '%s' prepares this node as the standby partner (%s)", user.username, payload.ip)
    return await _call(updater_client.ha_local_prepare(payload.ip))


class InitRequest(BaseModel):
    password: str = Field(default="", max_length=1024)
    code: str = Field(min_length=10, max_length=200)
    vip: str = Field(max_length=18)
    node: str | None = Field(default=None, max_length=32)
    peer_node: str | None = Field(default=None, max_length=32)
    node_ip: str | None = Field(default=None, max_length=15)
    vip_dev: str | None = Field(default=None, max_length=32)
    check: bool = False


@router.post("/init", status_code=202)
async def init(payload: InitRequest, user: AdminUser = Depends(current_user)) -> dict:
    """Set the pair up with this node as the primary (`check`: only the preflight, nothing is sent or changed, no password needed)."""
    _admin(user)
    if not payload.check:
        await _reauth(user, payload.password, "ha-init")
    body = {"code": payload.code, "vip": payload.vip, "check": payload.check}
    for key in ("node", "peer_node", "node_ip", "vip_dev"):
        if getattr(payload, key):
            body[key] = getattr(payload, key)
    logger.info("HA: '%s' %s the pair (VIP %s)", user.username, "checks" if payload.check else "sets up", payload.vip)
    return await _call(updater_client.ha_local_init(body))


class ActionRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    action: str = Field(max_length=20)


@router.post("/action", status_code=202)
async def action(payload: ActionRequest, user: AdminUser = Depends(current_user)) -> dict:
    """Sync now, switch to the partner, rebuild the standby, dissolve the pair (all on the primary)."""
    _admin(user)
    if payload.action not in ACTIONS:
        raise api_error(400, ErrorCode.HA_INPUT_INVALID)
    await _reauth(user, payload.password, f"ha-{payload.action}")
    logger.info("HA: '%s' runs '%s'", user.username, payload.action)
    return await _call(updater_client.ha_local_action(payload.action))


@router.get("/job")
async def job(user: AdminUser = Depends(current_user)) -> dict:
    """The last pair job of this node with its log (never contains the pairing code)."""
    _admin(user)
    status = await _call(updater_client.ha_local_status())
    return {"job": status.get("job"), "log": status.get("log") or []}
