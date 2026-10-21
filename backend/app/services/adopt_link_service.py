"""Links a hand-made object that was found on a firewall to a central object that already exists, instead
of creating a second central object for it ("Mit vorhandenem zentralem Objekt verknüpfen").

Typical case: the same rule/alias/NAT entry was made by hand on several firewalls (or on both nodes of an HA
pair). The first adoption creates the central object; every further firewall is LINKED to it: the host
becomes another target of that object and gets a deployment row holding the live UUID (remote_uuid) and
the payload OPNexus would send. Like adopting, linking writes NOTHING to the firewall. The live state is
read fresh from the host at link time (the browser only names the central object), so the preview right
afterwards says "unchanged" and a later rollout updates the one object on every host without a duplicate.

A central object is an EXACT match when every field OPNexus manages is equal after the object's
{{VARIABLE}} placeholders are replaced with this host's values (variables_service.resolve). An object with
the same identity (alias name / rule description) but different content is only shown as a hint with the
differing fields and is NEVER linked automatically.

Callers hold policy_lock (same serialisation as adopting), which also makes a repeated link idempotent.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup
from app.services import variables_service
from app.services.adopt_common import HostAlreadyHasObject, LinkTargetBusy, LinkTargetChanged, LinkTargetNotFound
from app.services.policy_hierarchy_target_service import hydrate_hierarchy_targets
from app.services.policy_service import effective_hosts

LINK_MESSAGE = "Mit vorhandenem Objekt verknüpft"
BUSY_STATUSES = ("pending", "revert_failed")  # the central object waits for a confirmation / failed to roll back


@dataclass(frozen=True)
class LinkSpec:
    model: type
    deployment: type
    fk: str  # deployment column pointing at the central object
    uuid_attr: str  # deployment column holding the OPNsense UUID (remote_uuid)
    identity: str  # attribute naming the object: "name" (alias) or "description"
    payload: Callable[[Any, OpnsenseHost], dict]
    revision: Callable


@dataclass
class Match:
    obj: Any
    diff: list[dict]  # empty = exact match
    busy: bool = False  # the central object waits for a confirmation: linking is refused until that is settled


def _norm(value) -> Any:
    if value is None:
        return ""
    return value if isinstance(value, bool) else str(value)


def _show(value) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else ("" if value is None else str(value))


def diff_fields(obj, values: dict, host: OpnsenseHost) -> list[dict]:
    """Fields in which the central object (resolved for `host`) differs from the live `values`."""
    raw = {key: getattr(obj, key) for key in values}
    try:
        central = variables_service.resolve(raw, host)
    except variables_service.MissingVariables as exc:
        # Cannot be compared (and could not be deployed to this host either): report, never match.
        return [{"field": "variables", "central": ", ".join(exc.missing[host.name]), "live": ""}]
    return [{"field": key, "central": _show(central[key]), "live": _show(values[key])}
            for key in values if _norm(central[key]) != _norm(values[key])]


def _options(spec: LinkSpec):
    return [
        selectinload(spec.model.targets),
        selectinload(spec.model.groups).selectinload(DeviceGroup.hosts),
        selectinload(spec.model.deployments),
    ]


async def load_central(db: AsyncSession, spec: LinkSpec) -> list:
    objs = list((await db.scalars(select(spec.model).options(*_options(spec)))).unique().all())
    await hydrate_hierarchy_targets(db, objs)
    return objs


def find(objs: list, spec: LinkSpec, host: OpnsenseHost, values: dict) -> tuple[list[Match], list[Match]]:
    """(exact matches, same-identity objects that differ). Objects that already have a deployment on
    this host are not candidates: linking would collide with that deployment."""
    exact: list[Match] = []
    similar: list[Match] = []
    for obj in objs:
        if any(d.host_id == host.id for d in obj.deployments):
            continue
        if _norm(getattr(obj, spec.identity)) != _norm(values[spec.identity]):
            continue
        d = diff_fields(obj, values, host)
        busy = any(dep.status in BUSY_STATUSES for dep in obj.deployments)
        (similar if d else exact).append(Match(obj, d, busy))
    # prefer the object that already reaches the most hosts (the "real" one), then a stable order
    exact.sort(key=lambda m: (-len(effective_hosts(m.obj)), str(m.obj.id)))
    return exact, similar


def describe(spec: LinkSpec, match: Match) -> dict:
    return {
        "id": match.obj.id,
        "name": getattr(match.obj, spec.identity),
        "targets": [h.name for h in effective_hosts(match.obj)],
        "diff": match.diff,
    }


async def link(db: AsyncSession, spec: LinkSpec, host: OpnsenseHost, object_id, remote_uuid: str, values: dict):
    """Links the live object `remote_uuid` (whose normalised fields are `values`, read fresh by the caller)
    to the central object `object_id`. Returns it; a repeated call for the same pair changes nothing."""
    obj = (await db.execute(
        select(spec.model).where(spec.model.id == object_id).options(*_options(spec))
        .execution_options(populate_existing=True)
    )).unique().scalar_one_or_none()
    if obj is None:
        raise LinkTargetNotFound(str(object_id))
    await hydrate_hierarchy_targets(db, [obj])

    existing = next((d for d in obj.deployments if d.host_id == host.id), None)
    if existing is not None:
        if getattr(existing, spec.uuid_attr) == remote_uuid:
            return obj  # already linked: nothing to do
        raise HostAlreadyHasObject(str(object_id))
    if any(d.status in BUSY_STATUSES for d in obj.deployments):
        raise LinkTargetBusy(str(object_id))
    changed = diff_fields(obj, values, host)
    if changed:
        raise LinkTargetChanged(changed)

    if host.id not in {h.id for h in effective_hosts(obj)}:
        obj.targets.append(host)
    # an association-only change does not touch the row; stale edit forms must still be refused (see edit_rule)
    obj.updated_at = datetime.now(timezone.utc)
    await db.flush()
    spec.revision(db, obj, f"Mit {host.name} verknüpft")
    db.add(spec.deployment(**{
        spec.fk: obj.id, "host_id": host.id, "status": "success", "message": LINK_MESSAGE,
        spec.uuid_attr: remote_uuid, "last_payload": spec.payload(obj, host), "previous_payload": None,
    }))
    await db.flush()
    return obj
