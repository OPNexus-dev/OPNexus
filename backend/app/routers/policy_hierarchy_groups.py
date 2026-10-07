import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.alias import FirewallAlias
from app.models.one_to_one import OneToOneRule
from app.models.outbound_nat import OutboundNatRule
from app.models.port_forward import PortForwardRule
from app.schemas.policy_hierarchy import PolicyHierarchyGroupRead, PolicyHierarchyGroupWrite
from app.services.policy_hierarchy_group_service import assert_no_cycle
from app.services.policy_hierarchy_service import policy_lock

router = APIRouter(prefix="/policy-hierarchy/groups", tags=["policy-hierarchy"])


def _to_read(group: PolicyHierarchyGroup) -> PolicyHierarchyGroupRead:
    return PolicyHierarchyGroupRead(
        id=group.id, name=group.name, description=group.description, parent_id=group.parent_id,
        created_at=group.created_at, host_ids=[h.id for h in group.hosts],
    )


async def _get_or_404(db: AsyncSession, group_id: uuid.UUID) -> PolicyHierarchyGroup:
    # populate_existing=True: without it, a group already in this request's
    # identity map (e.g. right after create_group's db.add()+commit()) is
    # returned as-is by db.get() and the `hosts` selectinload option is
    # silently skipped — leaving `.hosts` unloaded and triggering a
    # synchronous lazy-load attempt outside any greenlet context later.
    group = await db.get(
        PolicyHierarchyGroup, group_id,
        options=[selectinload(PolicyHierarchyGroup.hosts)], populate_existing=True,
    )
    if not group:
        raise api_error(404, ErrorCode.GRUPPE_NICHT_GEFUNDEN)
    return group


@router.get("", response_model=list[PolicyHierarchyGroupRead])
async def list_groups(db: AsyncSession = Depends(get_db)):
    groups = (await db.scalars(
        select(PolicyHierarchyGroup).options(selectinload(PolicyHierarchyGroup.hosts)).order_by(PolicyHierarchyGroup.name)
    )).all()
    return [_to_read(g) for g in groups]


@router.post("", response_model=PolicyHierarchyGroupRead, status_code=201)
async def create_group(payload: PolicyHierarchyGroupWrite, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    if await db.scalar(select(PolicyHierarchyGroup.id).where(PolicyHierarchyGroup.name == payload.name)):
        raise api_error(409, ErrorCode.GRUPPENNAME_BEREITS_VERGEBEN)
    if payload.parent_id and not await db.get(PolicyHierarchyGroup, payload.parent_id):
        raise api_error(400, ErrorCode.UEBERGEORDNETE_GRUPPE_NICHT_GEFUNDEN)
    group = PolicyHierarchyGroup(name=payload.name, description=payload.description, parent_id=payload.parent_id)
    db.add(group)
    await db.commit()
    return _to_read(await _get_or_404(db, group.id))


@router.put("/{group_id}", response_model=PolicyHierarchyGroupRead)
async def update_group(group_id: uuid.UUID, payload: PolicyHierarchyGroupWrite, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    group = await _get_or_404(db, group_id)
    duplicate = await db.scalar(select(PolicyHierarchyGroup.id).where(
        PolicyHierarchyGroup.name == payload.name, PolicyHierarchyGroup.id != group_id))
    if duplicate:
        raise api_error(409, ErrorCode.GRUPPENNAME_BEREITS_VERGEBEN)
    if payload.parent_id and not await db.get(PolicyHierarchyGroup, payload.parent_id):
        raise api_error(400, ErrorCode.UEBERGEORDNETE_GRUPPE_NICHT_GEFUNDEN)
    await assert_no_cycle(db, group_id, payload.parent_id)
    group.name, group.description, group.parent_id = payload.name, payload.description, payload.parent_id
    await db.commit()
    return _to_read(await _get_or_404(db, group_id))


@router.delete("/{group_id}", status_code=204)
async def delete_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    group = await db.get(PolicyHierarchyGroup, group_id)
    if not group:
        raise api_error(404, ErrorCode.GRUPPE_NICHT_GEFUNDEN)
    if await db.scalar(select(PolicyHierarchyGroup.id).where(PolicyHierarchyGroup.parent_id == group_id).limit(1)):
        raise api_error(409, ErrorCode.GRUPPE_HAT_NOCH_UNTERGRUPPEN_DIESE_ZUERST_VERSCHIEBEN_ODER_9FFFD853)
    if await db.scalar(select(PolicyHierarchyRule.id).where(PolicyHierarchyRule.hierarchy_group_id == group_id).limit(1)):
        raise api_error(409, ErrorCode.GRUPPE_HAT_NOCH_ZUGEORDNETE_REGELN_DIESE_ZUERST_VERSCHIEBEN_2CCBC48B)
    inherited_resources = (
        (FirewallAlias, "Aliase"),
        (PortForwardRule, "Port-Forward-Regeln"),
        (OneToOneRule, "1:1-NAT-Regeln"),
        (OutboundNatRule, "Outbound-NAT-Regeln"),
    )
    for model, label in inherited_resources:
        if await db.scalar(select(model.id).where(model.policy_hierarchy_group_id == group_id).limit(1)):
            raise api_error(409, ErrorCode.GROUP_HAS_ASSIGNED_ITEMS, label=label)
    # Hosts assigned directly to this group are unassigned (ondelete="SET
    # NULL" on OpnsenseHost.policy_hierarchy_group_id), not blocked — a
    # group with only host members and no children/rules is safe to remove.
    await db.delete(group)
    await db.commit()
