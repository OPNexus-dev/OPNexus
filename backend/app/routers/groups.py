import uuid
from app.errors import ErrorCode, api_error
from app.models.port_forward import PortForwardRule
from app.models.policy import port_forward_groups
from app.models.one_to_one import OneToOneRule
from app.models.policy import one_to_one_groups
from app.models.outbound_nat import OutboundNatRule
from app.models.policy import outbound_nat_groups
import hashlib
import json

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup, rule_groups, alias_groups
from app.models.rule import FirewallRule
from app.models.alias import FirewallAlias
from app.services.policy_service import policy_lock, effective_hosts

router = APIRouter(prefix="/groups", tags=["device-groups"])


class GroupWrite(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    host_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Name darf nicht leer sein")
        return value.strip()


class GroupUpdate(GroupWrite):
    preview_token: str | None = Field(default=None, min_length=64, max_length=64)


def read(group):
    return {"id": group.id, "name": group.name, "description": group.description,
            "host_ids": [h.id for h in group.hosts]}


@router.get("")
async def list_groups(db: AsyncSession = Depends(get_db)):
    groups = (await db.scalars(select(DeviceGroup).options(selectinload(DeviceGroup.hosts)).order_by(DeviceGroup.name))).all()
    return [read(g) for g in groups]


async def save(db, payload, group=None):
    duplicate = await db.scalar(select(DeviceGroup).where(DeviceGroup.name == payload.name))
    if duplicate and (group is None or duplicate.id != group.id):
        raise api_error(409, ErrorCode.GRUPPENNAME_BEREITS_VERGEBEN)
    hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.id.in_(payload.host_ids)))).all())
    if len(hosts) != len(set(payload.host_ids)):
        raise api_error(400, ErrorCode.ZIEL_HOST_NICHT_GEFUNDEN)
    if group is None:
        group = DeviceGroup()
        db.add(group)
    group.name, group.description, group.hosts = payload.name, payload.description, hosts
    await db.commit()
    return read(group)


@router.post("", status_code=201)
async def create_group(payload: GroupWrite, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    return await save(db, payload)


@router.put("/{group_id}")
async def update_group(group_id: uuid.UUID, payload: GroupUpdate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    group = await db.get(DeviceGroup, group_id, options=[selectinload(DeviceGroup.hosts)])
    if not group:
        raise api_error(404, ErrorCode.GRUPPE_NICHT_GEFUNDEN)
    impact = await group_impact(db, group, payload)
    if payload.preview_token and payload.preview_token != impact["token"]:
        raise api_error(409, ErrorCode.GRUPPENMITGLIEDER_ODER_REGELN_WURDEN_GEAENDERT_AUSWIRKUNGEN_ERNEUT_PRUEFEN)
    return await save(db, payload, group)


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    group = await db.get(DeviceGroup, group_id)
    if not group:
        raise api_error(404, ErrorCode.GRUPPE_NICHT_GEFUNDEN)
    if await db.scalar(select(rule_groups.c.rule_id).where(rule_groups.c.group_id == group_id).limit(1)):
        raise api_error(409, ErrorCode.GRUPPE_WIRD_NOCH_VON_REGELN_VERWENDET)
    if await db.scalar(select(alias_groups.c.alias_id).where(alias_groups.c.group_id == group_id).limit(1)):
        raise api_error(409, ErrorCode.GRUPPE_WIRD_NOCH_VON_ALIASEN_VERWENDET)
    for assignment in (port_forward_groups, one_to_one_groups, outbound_nat_groups):
        if await db.scalar(select(assignment.c.group_id).where(assignment.c.group_id == group_id).limit(1)):
            raise api_error(409, ErrorCode.GRUPPE_WIRD_NOCH_VON_NAT_REGELN_VERWENDET)
    from app.models.rollout import rollout_wave_groups
    if await db.scalar(select(rollout_wave_groups.c.group_id).where(rollout_wave_groups.c.group_id == group_id).limit(1)):
        raise api_error(409, ErrorCode.ROLLOUT_GROUP_IN_USE)
    await db.delete(group)
    await db.commit()


def _impact_rows(entities, group, proposed, opnsense_uuid_attr):
    impacts = []
    for entity in entities:
        before = {h.id for h in effective_hosts(entity)}
        after = {h.id for h in entity.targets}
        for target_group in entity.groups:
            after.update(h.id for h in (proposed if target_group.id == group.id else target_group.hosts))
        removed = before - after
        deployed = {d.host_id for d in entity.deployments if getattr(d, opnsense_uuid_attr)}
        impacts.append({"added_host_ids": sorted(map(str, after - before)),
                         "removed_host_ids": sorted(map(str, removed)),
                         "retained_host_ids": sorted(map(str, removed & deployed))})
    return impacts


async def group_impact(db, group, payload):
    proposed = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.id.in_(payload.host_ids)))).all())
    if len(proposed) != len(set(payload.host_ids)):
        raise api_error(400, ErrorCode.ZIEL_HOST_NICHT_GEFUNDEN)
    rules = (await db.scalars(select(FirewallRule).where(FirewallRule.groups.any(DeviceGroup.id == group.id)).options(
        selectinload(FirewallRule.targets),
        selectinload(FirewallRule.groups).selectinload(DeviceGroup.hosts),
        selectinload(FirewallRule.deployments),
    ).order_by(FirewallRule.id))).all()
    aliases = (await db.scalars(select(FirewallAlias).where(FirewallAlias.groups.any(DeviceGroup.id == group.id)).options(
        selectinload(FirewallAlias.targets),
        selectinload(FirewallAlias.groups).selectinload(DeviceGroup.hosts),
        selectinload(FirewallAlias.deployments),
    ).order_by(FirewallAlias.id))).all()
    rule_impacts = [{"rule_id": str(r.id), "description": r.description, **row}
                     for r, row in zip(rules, _impact_rows(rules, group, proposed, "opnsense_rule_uuid"))]
    alias_impacts = [{"alias_id": str(a.id), "description": a.name, **row}
                      for a, row in zip(aliases, _impact_rows(aliases, group, proposed, "opnsense_alias_uuid"))]
    nat_impacts = []
    for model, kind in ((PortForwardRule, "port_forward"), (OneToOneRule, "one_to_one"), (OutboundNatRule, "outbound_nat")):
        entities = (await db.scalars(select(model).where(model.groups.any(DeviceGroup.id == group.id)).options(
            selectinload(model.targets), selectinload(model.groups).selectinload(DeviceGroup.hosts),
            selectinload(model.deployments),
        ).order_by(model.id))).all()
        nat_impacts.extend({"id": str(e.id), "kind": kind, "description": e.description, **row}
                           for e, row in zip(entities, _impact_rows(entities, group, proposed, f"opnsense_{kind}_uuid")))
    old_ids = {h.id for h in group.hosts}
    new_ids = {h.id for h in proposed}
    result = {"added_members": [{"id": str(h.id), "name": h.name} for h in sorted(proposed, key=lambda h: str(h.id)) if h.id not in old_ids],
              "removed_members": [{"id": str(h.id), "name": h.name} for h in sorted(group.hosts, key=lambda h: str(h.id)) if h.id not in new_ids],
              "rules": rule_impacts,
              "aliases": alias_impacts, "nat": nat_impacts}
    state = {"current": {"name": group.name, "description": group.description, "host_ids": sorted(map(str, old_ids))},
             "proposed": {"name": payload.name, "description": payload.description, "host_ids": sorted(map(str, new_ids))},
             "impact": result}
    result["token"] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
    return result


@router.post("/{group_id}/preview")
async def preview_group(group_id: uuid.UUID, payload: GroupWrite, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    group = await db.get(DeviceGroup, group_id, options=[selectinload(DeviceGroup.hosts)])
    if not group:
        raise api_error(404, ErrorCode.GRUPPE_NICHT_GEFUNDEN)
    return await group_impact(db, group, payload)
