"""The live order of the rules on a firewall, and moving them (on probation, see services/rule_order_service.py).

    GET  /rule-order/hosts/{id}            everybody signed in   the rules in firewall order, with origin and movability
    GET  /rule-order/open                  everybody signed in   every open (unconfirmed / failed) move
    POST /rule-order/hosts/{id}/move       operator, admin       (governance: operators_may_deploy), no read token
    POST /rule-order/hosts/{id}/confirm    operator, admin       BROWSER SESSION ONLY, never a token
    POST /rule-order/hosts/{id}/revert     operator, admin       (governance: operators_may_deploy)

Who may do what is enforced twice: AuthMiddleware (services/api_token_service.py: _SESSION_ONLY, _ROLLOUT_POST)
and here. MCP stays read-only.
"""
import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.models.rule_deployment import RuleDeployment
from app.models.rule_order import OPEN_STATUSES, RuleOrderChange
from app.models.user import AdminUser
from app.routers import governance
from app.routers.auth import resolve_user
from app.schemas.rule_order import HostOrderRead, MoveRequest, OrderChangeRead
from app.services import rule_order_service as service
from app.services.opnsense_client import OpnsenseAPIError

router = APIRouter(prefix="/rule-order", tags=["rule-order"])


async def _host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


def _writer(user: AdminUser, request: Request) -> None:
    if user.role == "read_only":
        raise api_error(403, ErrorCode.READ_ONLY_ACCESS)
    token = getattr(request.state, "api_token", None)
    if token is not None and token.scope == "read":
        raise api_error(403, ErrorCode.API_TOKEN_SCOPE_FORBIDDEN, scope=token.scope)


async def _deployer(user: AdminUser, request: Request, db: AsyncSession) -> None:
    _writer(user, request)
    if user.role == "admin":
        return
    if user.role == "operator" and (await governance.get_settings(db)).operators_may_deploy:
        return
    raise api_error(403, ErrorCode.DEPLOY_NEEDS_ADMIN)


def _read(change: RuleOrderChange, host_name: str | None) -> dict:
    return {"id": change.id, "host_id": change.host_id, "host_name": host_name, "rule_uuid": change.rule_uuid,
            "description": change.description, "status": change.status, "message": change.message, "actor": change.actor,
            "created_at": change.created_at, "revert_deadline": change.revert_deadline}


async def _reads(db: AsyncSession, changes) -> list[dict]:
    names = {}
    for change in changes:
        if change.host_id not in names:
            host = await db.get(OpnsenseHost, change.host_id)
            names[change.host_id] = host.name if host else None
    return [_read(c, names[c.host_id]) for c in changes]


@router.get("/hosts/{host_id}", response_model=HostOrderRead)
async def host_order(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    host = await _host(db, host_id)
    try:
        rows = await service.live_rows(host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.ORDER_HOST_UNREACHABLE, host=host.name, reason=str(exc)[:200]) from exc
    rows.sort(key=service.order_of)
    central = {d.opnsense_rule_uuid: d.rule_id for d in (await db.scalars(select(RuleDeployment).where(
        RuleDeployment.host_id == host.id, RuleDeployment.opnsense_rule_uuid.is_not(None)))).all()}
    hierarchy = await service.hierarchy_uuids(db, host)
    out = []
    for r in rows:
        u = r["uuid"]
        model_rule = service.is_model_rule(r)
        origin = "system" if not model_rule else "hierarchy" if u in hierarchy else "central" if u in central else "local"
        out.append({
            "uuid": u, "description": r.get("description") or "", "action": r.get("action") or "",
            "action_label": r.get("%action") or r.get("action") or "", "direction": r.get("direction") or "",
            "direction_label": r.get("%direction") or r.get("direction") or "", "interface": r.get("interface") or None,
            "interface_label": r.get("%interface") or r.get("interface") or None, "protocol": r.get("protocol") or None,
            "protocol_label": r.get("%protocol") or r.get("protocol") or None, "source_net": r.get("source_net") or "any",
            "source_port": r.get("source_port") or None, "destination_net": r.get("destination_net") or "any",
            "destination_port": r.get("destination_port") or None, "enabled": str(r.get("enabled")) in ("1", "True", "true"),
            "is_automatic": service._truthy(r.get("is_automatic")), "prio_group": service.group_of(r),
            "sort_order": str(r.get("sort_order") or ""), "movable": model_rule and origin != "hierarchy", "origin": origin,
            "central_rule_id": central.get(u)})
    open_changes = await service.open_changes(db, [host.id])
    return {"host_id": host.id, "rows": out, "open": await _reads(db, open_changes)}


@router.get("/open", response_model=list[OrderChangeRead])
async def open_list(db: AsyncSession = Depends(get_db)):
    changes = list((await db.scalars(select(RuleOrderChange).where(RuleOrderChange.status.in_(OPEN_STATUSES))
                                     .order_by(RuleOrderChange.created_at))).all())
    return await _reads(db, changes)


@router.post("/hosts/{host_id}/move", response_model=OrderChangeRead | None)
async def move(host_id: uuid.UUID, payload: MoveRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = await resolve_user(request, db)
    await _deployer(user, request, db)
    host = await _host(db, host_id)
    change = await service.move_rule(db, host, actor=user.username, rule_uuid=payload.rule_uuid, before_uuid=payload.before_uuid)
    return None if change is None else (await _reads(db, [change]))[0]


@router.post("/hosts/{host_id}/confirm", response_model=list[OrderChangeRead])
async def confirm(host_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    user = await resolve_user(request, db)
    _writer(user, request)
    if getattr(request.state, "api_token", None) is not None:
        raise api_error(403, ErrorCode.API_TOKEN_SCOPE_FORBIDDEN, scope=request.state.api_token.scope)
    host = await _host(db, host_id)
    return await _reads(db, await service.confirm_host(db, host))


@router.post("/hosts/{host_id}/revert", response_model=list[OrderChangeRead])
async def revert(host_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    user = await resolve_user(request, db)
    await _deployer(user, request, db)
    host = await _host(db, host_id)
    return await _reads(db, await service.revert_host(db, host))
