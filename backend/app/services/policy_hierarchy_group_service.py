"""Cycle protection for the PolicyHierarchyGroup tree.

Kept as its own small module (like alias_policy_service.py/one_to_one_policy_service.py
are split out from the main *_service.py for their entity) since it's a
narrow, self-contained concern used only from routers/policy_hierarchy_groups.py.
"""
import uuid

from app.errors import ErrorCode, api_error
from app.models.policy_hierarchy_group import PolicyHierarchyGroup


async def assert_no_cycle(db, group_id: uuid.UUID | None, proposed_parent_id: uuid.UUID | None) -> None:
    """Raises 409 if setting `group_id`'s parent to `proposed_parent_id`
    would put `group_id` in its own ancestor chain. `group_id` is None when
    creating a brand-new group — a fresh node has no children yet, so no
    cycle is possible regardless of parent."""
    if proposed_parent_id is None or group_id is None:
        return
    if proposed_parent_id == group_id:
        raise api_error(409, ErrorCode.GROUP_CANNOT_BE_OWN_PARENT)
    current_id: uuid.UUID | None = proposed_parent_id
    seen: set[uuid.UUID] = set()
    while current_id is not None:
        if current_id == group_id:
            raise api_error(409, ErrorCode.GROUP_HIERARCHY_CYCLE)
        if current_id in seen:
            break
        seen.add(current_id)
        parent = await db.get(PolicyHierarchyGroup, current_id)
        current_id = parent.parent_id if parent else None
