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
from app.services.policy_hierarchy_target_service import hydrate_hierarchy_targets
from app.services.policy_service import effective_hosts

router = APIRouter(prefix="/hosts/{host_id}/variables", tags=["variables"])

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
    return used


async def _read(db: AsyncSession, host: OpnsenseHost) -> dict:
    values = variables_service.host_values(host)
    used = await _usage(db, host)
    names = sorted(set(values) | set(used))
    return {"variables": [
        {"name": n, "value": values.get(n), "used_by": used.get(n, []), "missing": n in used and n not in values}
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
