"""Shared inherited targeting for resources attached to the policy tree.

Firewall rules have their own hierarchy entity because ordering is part of
their semantics.  Aliases and NAT rules only need inherited *targeting*:
an optional authoring group expands to every host in that group's subtree.
The resolved hosts are attached transiently so the existing preview/deploy
state machines can keep using policy_service.effective_hosts without
duplicating those safety-critical workflows.
"""
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.services.policy_hierarchy_service import descendants


async def validate_hierarchy_group(db: AsyncSession, group_id):
    if group_id is None:
        return
    if await db.get(PolicyHierarchyGroup, group_id) is None:
        raise api_error(400, ErrorCode.POLICY_HIERARCHIE_GRUPPE_NICHT_GEFUNDEN)


async def hydrate_hierarchy_targets(db: AsyncSession, resources: Iterable[object]) -> None:
    resources = list(resources)
    group_ids = {
        resource.policy_hierarchy_group_id
        for resource in resources
        if getattr(resource, "policy_hierarchy_group_id", None) is not None
    }
    if not group_ids:
        for resource in resources:
            resource._policy_hierarchy_hosts = []
        return

    groups = list((await db.scalars(
        select(PolicyHierarchyGroup).options(selectinload(PolicyHierarchyGroup.hosts))
    )).all())
    by_group = {}
    for group_id in group_ids:
        hosts = {}
        for group in descendants(group_id, groups):
            hosts.update({host.id: host for host in group.hosts})
        by_group[group_id] = list(hosts.values())
    for resource in resources:
        resource._policy_hierarchy_hosts = by_group.get(
            getattr(resource, "policy_hierarchy_group_id", None), []
        )
