"""Site variables of one host: the values for {{NAME}} placeholders, and which placeholders its objects use."""
import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.alias import FirewallAlias
from app.models.host import OpnsenseHost
from app.models.host_variable import HostVariable
from app.models.one_to_one import OneToOneRule
from app.models.outbound_nat import OutboundNatRule
from app.models.policy import DeviceGroup
from app.models.port_forward import PortForwardRule
from app.models.rule import FirewallRule
from app.services import alias_deploy_service, one_to_one_deploy_service, outbound_nat_deploy_service
from app.services import port_forward_deploy_service, rule_deploy_service, variables_service
from app.services.interface_service import get_interfaces
from app.services.opnsense_client import OpnsenseAPIError
from app.services.policy_hierarchy_target_service import hydrate_hierarchy_targets
from app.services.policy_service import effective_hosts

router = APIRouter(prefix="/hosts/{host_id}/variables", tags=["variables"])
roles_router = APIRouter(prefix="/interface-roles", tags=["variables"])

_KINDS = (
    ("rule", FirewallRule, rule_deploy_service),
    ("alias", FirewallAlias, alias_deploy_service),
    ("port_forward", PortForwardRule, port_forward_deploy_service),
    ("one_to_one", OneToOneRule, one_to_one_deploy_service),
    ("outbound_nat", OutboundNatRule, outbound_nat_deploy_service),
)


class VariablesUpdate(BaseModel):
    variables: dict[str, str] = Field(default_factory=dict, max_length=200)


async def _host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if host is None:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


async def _usage(db: AsyncSession, host: OpnsenseHost) -> dict[str, list[dict]]:
    """name -> objects (that are deployed to this host) which use it."""
    used: dict[str, list[dict]] = {}
    for kind, model, service in _KINDS:
        entities = (await db.execute(
            select(model).options(selectinload(model.targets), selectinload(model.groups).selectinload(DeviceGroup.hosts))
        )).scalars().unique().all()
        await hydrate_hierarchy_targets(db, entities)
        for entity in entities:
            if host.id not in {h.id for h in effective_hosts(entity)}:
                continue
            label = getattr(entity, "description", None) or getattr(entity, "name", None) or str(entity.id)
            for name in variables_service.names_in_payload(service._base_payload(entity)):
                used.setdefault(name, []).append({"kind": kind, "id": str(entity.id), "label": label})
    # Hierarchy rules reach the hosts through the group tree; only their interface can hold a role.
    from app.models.policy_hierarchy_group import PolicyHierarchyGroup
    from app.models.policy_hierarchy_rule import PolicyHierarchyRule
    from app.services import policy_hierarchy_deploy_service, policy_hierarchy_service
    groups = list((await db.scalars(select(PolicyHierarchyGroup).options(selectinload(PolicyHierarchyGroup.hosts)))).all())
    for rule in (await db.scalars(select(PolicyHierarchyRule))).all():
        if host.id not in {h.id for h in policy_hierarchy_service.effective_hosts(rule, groups)}:
            continue
        for name in variables_service.names_in_payload(policy_hierarchy_deploy_service._base_payload(rule)):
            used.setdefault(name, []).append({"kind": "policy_hierarchy_rule", "id": str(rule.id), "label": rule.description})
    return used


async def _read(db: AsyncSession, host: OpnsenseHost) -> dict:
    values = variables_service.host_values(host)
    used = await _usage(db, host)
    names = sorted(set(values) | set(used))
    return {"variables": [
        {"name": n, "value": values.get(n), "used_by": used.get(n, []),
         "role": variables_service.is_role(n),
         # a missing role is not an error: the objects using it are simply not deployed to this host
         "missing": n in used and n not in values and not variables_service.is_role(n)}
        for n in names
    ]}


@router.get("")
async def read(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _read(db, await _host(db, host_id))


@router.put("")
async def replace(host_id: uuid.UUID, payload: VariablesUpdate, db: AsyncSession = Depends(get_db)):
    """Replace this host's variables with exactly the given set."""
    host = await _host(db, host_id)
    for name, value in payload.variables.items():
        if not variables_service.NAME_RE.fullmatch(name):
            raise api_error(422, ErrorCode.VARIABLE_INVALID, reason=f"Name „{name}“: Großbuchstaben, Ziffern und _, beginnend mit einem Buchstaben")
        if not variables_service.VALUE_RE.fullmatch(value):
            raise api_error(422, ErrorCode.VARIABLE_INVALID, reason=f"Wert von {name}: 1–255 Zeichen aus Buchstaben, Ziffern und . : / , @ + - _ Leerzeichen")
    roles = {n: v for n, v in payload.variables.items() if variables_service.is_role(n)}
    for name, value in roles.items():
        if not variables_service.ROLE_NAME_RE.fullmatch(name):
            raise api_error(422, ErrorCode.VARIABLE_INVALID, reason=f"Rolle „{name}“: IF_ gefolgt von Großbuchstaben, Ziffern und _ (höchstens 40 Zeichen)")
        if not variables_service.INTERFACE_RE.fullmatch(value):
            raise api_error(422, ErrorCode.VARIABLE_INVALID, reason=f"Wert von {name}: ein OPNsense-Interface-Bezeichner (z. B. lan, opt3, vlan0.20)")
    if roles:
        try:
            known = {o.name for o in await get_interfaces(host)}
        except OpnsenseAPIError:
            known = None  # the firewall does not answer: the deploy checks again live
        if known is not None:
            for name, value in roles.items():
                if value not in known:
                    raise api_error(422, ErrorCode.VARIABLE_INVALID, reason=f"{name}: Die Firewall „{host.name}“ hat kein Interface „{value}“")
    existing = {v.name: v for v in host.variables}
    for name in set(existing) - set(payload.variables):
        host.variables.remove(existing[name])
    for name, value in payload.variables.items():
        if name in existing:
            existing[name].value = value
        else:
            host.variables.append(HostVariable(name=name, value=value))
    await db.commit()
    await db.refresh(host)
    return await _read(db, host)


@roles_router.get("")
async def list_roles(db: AsyncSession = Depends(get_db)):
    """Every interface role in use anywhere: name -> {host name: interface identifier}. Feeds the interface choice of the forms."""
    rows = (await db.execute(
        select(HostVariable, OpnsenseHost.name).join(OpnsenseHost, OpnsenseHost.id == HostVariable.host_id)
        .where(HostVariable.name.startswith(variables_service.ROLE_PREFIX, autoescape=True)).order_by(HostVariable.name, OpnsenseHost.name)
    )).all()
    roles: dict[str, dict[str, str]] = {}
    for variable, host_name in rows:
        roles.setdefault(variable.name, {})[host_name] = variable.value
    return {"roles": [{"name": name, "hosts": hosts} for name, hosts in roles.items()]}
