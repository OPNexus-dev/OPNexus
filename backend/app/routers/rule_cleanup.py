"""Rule cleanup (1.31, B4 part 2): switch hand-made rules off, delete them later, restore them (DESIGN-1.31 §4).

    POST /hosts/{id}/rule-cleanup/preview                 operator, admin       prepare (writes nothing to a firewall)
    POST /hosts/{id}/rule-cleanup/{op}/cancel             operator, admin       the preview's own actor
    POST /hosts/{id}/rule-cleanup/{op}/deploy             ADMIN, token scope admin
    POST /hosts/{id}/rule-cleanup/{op}/confirm            operator, admin       BROWSER SESSION ONLY, never a token
    POST /hosts/{id}/rule-cleanup/{op}/revert             admin; operator only with operators_may_deploy
    POST /hosts/{id}/rule-cleanup/{op}/retry              ADMIN, token scope admin
    POST /hosts/{id}/rule-cleanup/items/{item}/restore    ADMIN, token scope admin
    GET  /hosts/{id}/rule-cleanup, /{op}                  everybody signed in (read tokens too)

Who may do what is enforced twice: AuthMiddleware (services/api_token_service.py: _ADMIN_ONLY_CLEANUP,
_SESSION_ONLY, _ROLLOUT_POST) and here, so a gap in one layer does not open the route. The audit trail is the
service's (one ``rule_cleanup`` entry per step and rule) plus the middleware's request entry (kind ``host``,
action = last path segment; ``/preview`` is not logged by it, the service writes its own entry).
The services raise ``api_error``; nothing is translated here. MCP stays read-only (no tool, ``api_get`` is GET).
"""
import uuid

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.models.user import AdminUser
from app.routers import governance
from app.routers.auth import resolve_user
from app.schemas.rule_cleanup import CleanupOperationRead, CleanupPreviewRead, ConfirmName, PreviewRequest
from app.services import rule_cleanup_service as service
from app.services.rule_cleanup_service import ACK_TEXT_VERSION

router = APIRouter(prefix="/hosts/{host_id}/rule-cleanup", tags=["rule-cleanup"])


async def _host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


def _token(request: Request):
    return getattr(request.state, "api_token", None)


def _writer(user: AdminUser, request: Request) -> None:
    """Any write: not for a read-only account, not with a read-scope token."""
    if user.role == "read_only":
        raise api_error(403, ErrorCode.READ_ONLY_ACCESS)
    token = _token(request)
    if token is not None and token.scope == "read":
        raise api_error(403, ErrorCode.API_TOKEN_SCOPE_FORBIDDEN, scope=token.scope)


def _admin_only(user: AdminUser, request: Request) -> None:
    """Deploy, retry, restore: an admin account, and with a token only one of scope admin."""
    _writer(user, request)
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)
    token = _token(request)
    if token is not None and token.scope != "admin":
        raise api_error(403, ErrorCode.API_TOKEN_SCOPE_FORBIDDEN, scope=token.scope)


async def _deployer(user: AdminUser, request: Request, db: AsyncSession) -> None:
    """Revert: an admin, or an operator while the governance switch operators_may_deploy is on."""
    _writer(user, request)
    if user.role == "admin":
        return
    if user.role == "operator" and (await governance.get_settings(db)).operators_may_deploy:
        return
    raise api_error(403, ErrorCode.DEPLOY_NEEDS_ADMIN)


def _session_only(user: AdminUser, request: Request) -> None:
    """Confirm: "I still have access" is a human decision in a browser session, never a token's."""
    _writer(user, request)
    token = _token(request)
    if token is not None:
        raise api_error(403, ErrorCode.API_TOKEN_SCOPE_FORBIDDEN, scope=token.scope)


def _full_view(request: Request) -> bool:
    """Admins (session or admin-scope token) see the management sources/ports and firewall addresses in a plan."""
    from app.routers.audit import full_view
    return full_view(request)


async def _names(db: AsyncSession, operations: list[dict], request: Request) -> list[dict]:
    """The firewall names the UI shows next to the ids; for non-admins without the security-relevant plan parts."""
    if not _full_view(request):
        operations = [service.redact(op) for op in operations]
    ids = {uuid.UUID(op["host_id"]) for op in operations} | {uuid.UUID(i["host_id"]) for op in operations for i in op["items"]}
    names = {h.id: h.name for h in (await db.scalars(select(OpnsenseHost).where(OpnsenseHost.id.in_(ids)))).all()} if ids else {}
    for op in operations:
        op["host_name"] = names.get(uuid.UUID(op["host_id"]))
        for item in op["items"]:
            item["host_name"] = names.get(uuid.UUID(item["host_id"]))
    return operations


async def _one(db: AsyncSession, operation: dict, request: Request) -> dict:
    return (await _names(db, [operation], request))[0]


@router.get("", response_model=list[CleanupOperationRead])
async def list_operations(host_id: uuid.UUID, request: Request, limit: int = Query(50, ge=1, le=100),
                          db: AsyncSession = Depends(get_db)):
    """The newest operations of this host (and its cluster peers) with their items; unexpired previews included."""
    host = await _host(db, host_id)
    return await _names(db, await service.list_operations(db, host, limit), request)


@router.post("/preview", response_model=CleanupPreviewRead)
async def preview(host_id: uuid.UUID, payload: PreviewRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Recompute the plan from the live firewall(s) and store a 5-minute preview bound to the caller."""
    user = await resolve_user(request, db)
    _writer(user, request)
    host = await _host(db, host_id)
    kind, uuids, acknowledge = payload.resolved()
    if kind is None:
        raise api_error(422, ErrorCode.BEREINIGUNG_AUSWAHL_UNGUELTIG, field="kind")
    result = await service.preview(
        db, host, actor=user.username, actor_id=user.id, kind=kind, uuids=uuids, acknowledge=acknowledge,
        **({"unused_days": payload.unused_days} if payload.unused_days else {}),
        ack_text_version=payload.ack_text_version or ACK_TEXT_VERSION)
    result["operation"] = await _one(db, await service.get_operation(db, host, uuid.UUID(result["preview_id"])), request)
    return result if _full_view(request) else service.redact(result)


@router.get("/{op_id}", response_model=CleanupOperationRead)
async def get_operation(host_id: uuid.UUID, op_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    host = await _host(db, host_id)
    return await _one(db, await service.get_operation(db, host, op_id), request)


@router.post("/{op_id}/cancel", response_model=CleanupOperationRead)
async def cancel(host_id: uuid.UUID, op_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Drop an unused preview (only its own actor can)."""
    user = await resolve_user(request, db)
    _writer(user, request)
    host = await _host(db, host_id)
    return await _one(db, await service.cancel(db, host, op_id, actor=user.username), request)


@router.post("/{op_id}/deploy", response_model=CleanupOperationRead)
async def deploy(host_id: uuid.UUID, op_id: uuid.UUID, payload: ConfirmName, request: Request,
                 db: AsyncSession = Depends(get_db)):
    """Execute a preview. Admin only; the live state must still give the fingerprint of the preview."""
    user = await resolve_user(request, db)
    _admin_only(user, request)
    host = await _host(db, host_id)
    return await _one(db, await service.deploy(db, host, op_id, actor=user.username, confirm_name=payload.confirm_name), request)


@router.post("/{op_id}/confirm", response_model=CleanupOperationRead)
async def confirm(host_id: uuid.UUID, op_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """"I still have access": re-reads every node first. Browser session only, never with an API token."""
    user = await resolve_user(request, db)
    _session_only(user, request)
    host = await _host(db, host_id)
    return await _one(db, await service.confirm(db, host, op_id), request)


@router.post("/{op_id}/revert", response_model=CleanupOperationRead)
async def revert(host_id: uuid.UUID, op_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Take the change back now (the same code the sweeper runs when the window passes)."""
    user = await resolve_user(request, db)
    await _deployer(user, request, db)
    host = await _host(db, host_id)
    return await _one(db, await service.revert_now(db, host, op_id), request)


@router.post("/{op_id}/retry", response_model=CleanupOperationRead)
async def retry(host_id: uuid.UUID, op_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Revert again after revert_failed, or read the firewall again for a delete/restore with an unclear item."""
    user = await resolve_user(request, db)
    _admin_only(user, request)
    host = await _host(db, host_id)
    return await _one(db, await service.retry(db, host, op_id), request)


@router.post("/items/{item_id}/restore", response_model=CleanupOperationRead)
async def restore(host_id: uuid.UUID, item_id: uuid.UUID, payload: ConfirmName, request: Request,
                  db: AsyncSession = Depends(get_db)):
    """Bring a deleted rule back, switched OFF, at its old position (a new UUID). The result is the restore run;
    ``warnings`` can hold BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR."""
    user = await resolve_user(request, db)
    _admin_only(user, request)
    host = await _host(db, host_id)
    return await _one(db, await service.restore_item(
        db, host, item_id, actor=user.username, actor_id=user.id, confirm_name=payload.confirm_name), request)
