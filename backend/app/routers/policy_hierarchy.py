import uuid

from fastapi import APIRouter, Body, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.policy import PolicyHierarchyRuleRevision
from app.models.policy_hierarchy_group import PolicyHierarchyGroup
from app.models.policy_hierarchy_rule import PolicyHierarchyRule
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.schemas.rule import DeploymentResult
from app.schemas.policy_hierarchy import (
    PolicyHierarchyDeployApproval,
    PolicyHierarchyMoveDirection,
    PolicyHierarchyReorder,
    PolicyHierarchyRuleCreate,
    PolicyHierarchyRuleEdit,
    PolicyHierarchyRuleRead,
)
from app.services.alias_service import check_alias_consistency, describe_alias_contents, ensure_aliases_settled
from app.services.interface_service import check_interface_consistency
from app.services.opnsense_client import OpnsenseAPIError
from app.services.policy_hierarchy_deploy_service import (
    confirm_hierarchy_rule,
    deploy_hierarchy_rule,
    reassert_group_ordering,
    revert_hierarchy_deployment,
    undeploy_hierarchy_rule,
)
from app.services.policy_hierarchy_service import (
    HIERARCHY_RULE_FIELDS,
    effective_hosts,
    ensure_editable,
    policy_lock,
    preview,
    revision,
    workflow,
)

router = APIRouter(prefix="/policy-hierarchy", tags=["policy-hierarchy"])


def _rule_load_options():
    return [selectinload(PolicyHierarchyRule.deployments).selectinload(PolicyHierarchyRuleDeployment.host)]


async def _load_all_groups(db: AsyncSession) -> list[PolicyHierarchyGroup]:
    return list((await db.scalars(
        select(PolicyHierarchyGroup).options(selectinload(PolicyHierarchyGroup.hosts))
    )).all())


async def _load_rule(db: AsyncSession, rule_id: uuid.UUID) -> PolicyHierarchyRule:
    await policy_lock(db)
    # populate_existing=True: without it, a rule already in this request's
    # identity map (e.g. right after create_rule's db.add()+commit(), or a
    # second _load_rule call later in the same request) is returned as-is
    # by db.get() and the selectinload(deployments) option is silently
    # skipped — leaving `.deployments` unloaded and triggering a
    # synchronous lazy-load attempt outside any greenlet context later.
    rule = await db.get(PolicyHierarchyRule, rule_id, options=_rule_load_options(), populate_existing=True)
    if not rule:
        raise api_error(404, ErrorCode.REGEL_NICHT_GEFUNDEN)
    return rule


def _to_read(rule: PolicyHierarchyRule, all_groups: list[PolicyHierarchyGroup]) -> PolicyHierarchyRuleRead:
    hosts_by_id = {h.id: h for h in effective_hosts(rule, all_groups)}
    hosts_by_id.update({d.host_id: d.host for d in rule.deployments if d.host is not None})
    state, counts = workflow(rule, rule.deployments, all_groups)
    return PolicyHierarchyRuleRead(
        workflow_state=state,
        workflow_counts=counts,
        id=rule.id,
        hierarchy_group_id=rule.hierarchy_group_id,
        rulebase=rule.rulebase,
        sequence=rule.sequence,
        description=rule.description,
        interface=rule.interface,
        direction=rule.direction,
        ipprotocol=rule.ipprotocol,
        protocol=rule.protocol,
        action=rule.action,
        source_net=rule.source_net,
        source_port=rule.source_port,
        destination_net=rule.destination_net,
        destination_port=rule.destination_port,
        enabled=rule.enabled,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        effective_host_ids=[h.id for h in effective_hosts(rule, all_groups)],
        deployments=[
            DeploymentResult(
                host_id=d.host_id,
                host_name=hosts_by_id[d.host_id].name if d.host_id in hosts_by_id else "?",
                status=d.status,
                message=d.message,
                revert_deadline=d.revert_deadline,
            )
            for d in rule.deployments
        ],
    )


async def _get_group_or_400(db: AsyncSession, group_id: uuid.UUID) -> PolicyHierarchyGroup:
    group = await db.get(PolicyHierarchyGroup, group_id)
    if not group:
        raise api_error(400, ErrorCode.POLICY_HIERARCHIE_GRUPPE_NICHT_GEFUNDEN)
    return group


@router.get("/rules", response_model=list[PolicyHierarchyRuleRead])
async def list_rules(db: AsyncSession = Depends(get_db)):
    all_groups = await _load_all_groups(db)
    rules = (await db.scalars(select(PolicyHierarchyRule).options(*_rule_load_options()))).all()
    return [_to_read(rule, all_groups) for rule in rules]


@router.post("/rules", response_model=PolicyHierarchyRuleRead, status_code=201)
async def create_rule(payload: PolicyHierarchyRuleCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    await _get_group_or_400(db, payload.hierarchy_group_id)
    # New rules are appended to the end of their (group, rulebase) list;
    # atomic drag-and-drop and up/down controls handle reordering later.
    max_sequence = await db.scalar(
        select(PolicyHierarchyRule.sequence).where(
            PolicyHierarchyRule.hierarchy_group_id == payload.hierarchy_group_id,
            PolicyHierarchyRule.rulebase == payload.rulebase,
        ).order_by(PolicyHierarchyRule.sequence.desc()).limit(1)
    )
    rule = PolicyHierarchyRule(
        hierarchy_group_id=payload.hierarchy_group_id,
        rulebase=payload.rulebase,
        sequence=(max_sequence + 1) if max_sequence is not None else 0,
        description=payload.description,
        interface=payload.interface,
        direction=payload.direction,
        ipprotocol=payload.ipprotocol,
        protocol=payload.protocol,
        action=payload.action,
        source_net=payload.source_net,
        source_port=payload.source_port,
        destination_net=payload.destination_net,
        destination_port=payload.destination_port,
        enabled=payload.enabled,
    )
    db.add(rule)
    await db.flush()
    revision(db, rule, "Regel angelegt")
    await db.commit()
    return _to_read(await _load_rule(db, rule.id), await _load_all_groups(db))


@router.put("/rules/{rule_id}", response_model=PolicyHierarchyRuleRead)
async def edit_rule(rule_id: uuid.UUID, payload: PolicyHierarchyRuleEdit, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    ensure_editable(rule)
    if rule.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.REGEL_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_BF51B6B0)
    await _get_group_or_400(db, payload.hierarchy_group_id)
    if not await db.scalar(select(PolicyHierarchyRuleRevision.id).where(PolicyHierarchyRuleRevision.policy_hierarchy_id == rule.id).limit(1)):
        revision(db, rule, "Vor erster Bearbeitung")
    for field in HIERARCHY_RULE_FIELDS:
        setattr(rule, field, getattr(payload, field))
    rule.hierarchy_group_id = payload.hierarchy_group_id
    rule.rulebase = payload.rulebase
    from datetime import datetime, timezone
    rule.updated_at = datetime.now(timezone.utc)
    revision(db, rule, "Entwurf gespeichert")
    await db.commit()
    return _to_read(rule, await _load_all_groups(db))


@router.put("/groups/{group_id}/rules/{rulebase}/order", response_model=list[PolicyHierarchyRuleRead])
async def reorder_rules(
    group_id: uuid.UUID,
    rulebase: str,
    payload: PolicyHierarchyReorder,
    db: AsyncSession = Depends(get_db),
):
    """Atomically persist a complete drag-and-drop order.

    The complete sibling set and every optimistic timestamp must still
    match. This prevents two administrators from silently overwriting each
    other's order and avoids the partial state produced by several
    sequential up/down requests.
    """
    if rulebase not in ("pre", "post"):
        raise api_error(404, ErrorCode.RULEBASE_NICHT_GEFUNDEN)
    await policy_lock(db)
    await _get_group_or_400(db, group_id)
    siblings = list((await db.scalars(
        select(PolicyHierarchyRule).where(
            PolicyHierarchyRule.hierarchy_group_id == group_id,
            PolicyHierarchyRule.rulebase == rulebase,
        ).options(*_rule_load_options()).order_by(PolicyHierarchyRule.sequence, PolicyHierarchyRule.id)
    )).all())
    if len({item.id for item in payload.items}) != len(payload.items):
        raise api_error(422, ErrorCode.EINE_REGEL_DARF_IN_DER_REIHENFOLGE_NUR_EINMAL_F05DF47C)
    if {rule.id for rule in siblings} != {item.id for item in payload.items}:
        raise api_error(409, ErrorCode.REGELLISTE_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_ERNEUT_529D7203)
    expected = {item.id: item.expected_updated_at for item in payload.items}
    for rule in siblings:
        ensure_editable(rule)
    if any(rule.updated_at != expected[rule.id] for rule in siblings):
        raise api_error(409, ErrorCode.MINDESTENS_EINE_REGEL_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_932925BD)
    by_id = {rule.id: rule for rule in siblings}
    from datetime import datetime, timezone
    changed_at = datetime.now(timezone.utc)
    for sequence, item in enumerate(payload.items):
        rule = by_id[item.id]
        if rule.sequence != sequence:
            rule.sequence = sequence
            rule.updated_at = changed_at
            revision(db, rule, "Reihenfolge per Drag-and-drop geändert")
    await db.commit()
    all_groups = await _load_all_groups(db)
    return [_to_read(await _load_rule(db, item.id), all_groups) for item in payload.items]


@router.post("/rules/{rule_id}/move", response_model=PolicyHierarchyRuleRead)
async def move_rule(rule_id: uuid.UUID, payload: PolicyHierarchyMoveDirection, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    ensure_editable(rule)
    siblings = list((await db.scalars(
        select(PolicyHierarchyRule).where(
            PolicyHierarchyRule.hierarchy_group_id == rule.hierarchy_group_id,
            PolicyHierarchyRule.rulebase == rule.rulebase,
        ).order_by(PolicyHierarchyRule.sequence, PolicyHierarchyRule.id)
    )).all())
    index = next(i for i, r in enumerate(siblings) if r.id == rule.id)
    neighbor_index = index - 1 if payload.direction == "up" else index + 1
    if 0 <= neighbor_index < len(siblings):
        neighbor = siblings[neighbor_index]
        rule.sequence, neighbor.sequence = neighbor.sequence, rule.sequence
        revision(db, rule, "Reihenfolge geändert")
    await db.commit()
    return _to_read(await _load_rule(db, rule_id), await _load_all_groups(db))


@router.post("/rules/{rule_id}/deploy", response_model=PolicyHierarchyRuleRead)
async def deploy(rule_id: uuid.UUID, approval: PolicyHierarchyDeployApproval | None = Body(default=None), db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    all_groups = await _load_all_groups(db)
    plan = preview(rule, all_groups)
    if approval and approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    if any(d.status in ("pending", "revert_failed") for d in rule.deployments):
        raise api_error(409, ErrorCode.OFFENES_DEPLOYMENT_ZUERST_BESTAETIGEN_ODER_ROLLBACK_KLAEREN)
    hosts = [h for h in effective_hosts(rule, all_groups) if h.enabled]
    if not hosts:
        raise api_error(400, ErrorCode.REGEL_HAT_KEINE_ZIEL_HOSTS)
    if len(hosts) > 1:
        labels = await check_interface_consistency(hosts, rule.interface)
        distinct = set(labels.values())
        if len(distinct) > 1:
            detail = ", ".join(f"{name}={label}" for name, label in sorted(labels.items()))
            raise api_error(409, ErrorCode.INTERFACE_MEANING_DIFFERS, interface=rule.interface, detail=detail)
        for field_label, value in (
            ("Source", rule.source_net),
            ("Destination", rule.destination_net),
            ("Source-Port", rule.source_port),
            ("Destination-Port", rule.destination_port),
        ):
            if not value:
                continue
            contents = await check_alias_consistency(hosts, value)
            if len(set(contents.values())) > 1:
                detail = describe_alias_contents(contents)
                raise api_error(409, ErrorCode.ALIAS_CONTENT_DIFFERS, field_label=field_label, alias=value, detail=detail)
    await ensure_aliases_settled(db, hosts, [rule.source_net, rule.destination_net, rule.source_port, rule.destination_port])
    await deploy_hierarchy_rule(db, rule, hosts)
    await db.refresh(rule, attribute_names=["deployments"])
    for d in rule.deployments:
        await db.refresh(d, attribute_names=["host"])
    return _to_read(rule, await _load_all_groups(db))


@router.post("/rules/{rule_id}/confirm", response_model=PolicyHierarchyRuleRead)
async def confirm(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    await confirm_hierarchy_rule(db, rule)
    return _to_read(rule, await _load_all_groups(db))


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    if any(d.status in ("pending", "revert_failed") for d in rule.deployments):
        raise api_error(409, ErrorCode.OFFENES_DEPLOYMENT_ZUERST_KLAEREN)
    try:
        await undeploy_hierarchy_rule(db, rule)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc
    await db.refresh(rule, attribute_names=["deployments"])
    await db.delete(rule)
    await db.commit()


@router.get("/rules/{rule_id}/preview")
async def preview_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    return preview(rule, await _load_all_groups(db))


@router.get("/rules/{rule_id}/revisions")
async def list_revisions(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _load_rule(db, rule_id)
    rows = (await db.scalars(
        select(PolicyHierarchyRuleRevision).where(PolicyHierarchyRuleRevision.policy_hierarchy_id == rule_id)
        .order_by(PolicyHierarchyRuleRevision.created_at.desc(), PolicyHierarchyRuleRevision.id)
    )).all()
    return [{"id": r.id, "created_at": r.created_at, "note": r.note, "snapshot": r.snapshot} for r in rows]


@router.post("/rules/{rule_id}/deployments/{host_id}/remove", status_code=204)
async def remove_previous_target(rule_id: uuid.UUID, host_id: uuid.UUID, approval: PolicyHierarchyDeployApproval, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    all_groups = await _load_all_groups(db)
    plan = preview(rule, all_groups)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ZIELE_ODER_DEPLOYMENT_WURDEN_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_remove"]:
        raise api_error(409, ErrorCode.NUR_FRUEHERE_AKTIVE_ZIELE_OHNE_OFFENEN_ROLLBACK_KOENNEN_06DE0ADB)
    deployment = next(d for d in rule.deployments if d.host_id == host_id)
    from app.services import opnsense_client
    try:
        await opnsense_client.del_rule(deployment.host, deployment.opnsense_policy_hierarchy_uuid)
        await opnsense_client.apply(deployment.host)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        await db.commit()
        raise api_error(502, ErrorCode.ENTFERNEN_FEHLGESCHLAGEN_DIE_ZUORDNUNG_BLEIBT_FUER_EINEN_NEUEN_4CF90E4C) from exc
    await db.delete(deployment)
    await db.commit()


@router.post("/rules/{rule_id}/deployments/{host_id}/retry-rollback", status_code=204)
async def retry_rollback(rule_id: uuid.UUID, host_id: uuid.UUID, approval: PolicyHierarchyDeployApproval, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    all_groups = await _load_all_groups(db)
    plan = preview(rule, all_groups)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.DEPLOYMENT_WURDE_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_retry_rollback"]:
        raise api_error(409, ErrorCode.FUER_DIESES_ZIEL_LIEGT_KEIN_WIEDERHOLBARER_ROLLBACK_VOR)
    deployment = next(d for d in rule.deployments if d.host_id == host_id)
    reverted = await revert_hierarchy_deployment(db, deployment)
    await db.commit()
    if not reverted:
        raise api_error(502, ErrorCode.ROLLBACK_ERNEUT_FEHLGESCHLAGEN_PRUEFE_DIE_ERREICHBARKEIT_DER_FIREWALL)


@router.post("/groups/{group_id}/reassert-ordering")
async def reassert_ordering(group_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Explicit ordering repair for a group subtree.

    Normal OPNexus rule/alias/NAT deploys also reassert automatically; this
    action remains useful after changing the desired order or after manual
    edits made directly on a firewall.
    """
    await policy_lock(db)
    group = await db.get(PolicyHierarchyGroup, group_id)
    if not group:
        raise api_error(404, ErrorCode.GRUPPE_NICHT_GEFUNDEN)
    errors = await reassert_group_ordering(db, group)
    if errors:
        raise api_error(502, ErrorCode.ORDERING_FAILED, reason='; '.join(errors))
    return {"status": "ok"}
