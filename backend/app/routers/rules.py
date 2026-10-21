import json
import uuid

from fastapi import APIRouter, Depends, Body
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.errors import ErrorCode, api_error
from app.database import get_db
from app.models.host import OpnsenseHost
from app.models.policy import DeviceGroup, RuleRevision
from app.services.policy_service import effective_hosts, snapshot, revision, preview, policy_lock, ensure_editable, RULE_FIELDS, workflow
from app.models.rule import FirewallRule
from app.models.rule_deployment import RuleDeployment
from app.schemas.rule import DeploymentResult, RuleCreate, RuleRead, RuleEdit, DeployApproval
from app.services.alias_service import check_alias_consistency, describe_alias_contents, ensure_aliases_settled
from app.services.interface_service import check_interface_consistency
from app.services.opnsense_client import OpnsenseAPIError
from app.services.rule_deploy_service import confirm_rule, deploy_rule, undeploy_rule, revert_deployment
from app.services import rule_adopt_service
from app.services.adopt_common import LINK_ERRORS, link_api_error
from app.schemas.rule import AdoptRequest, UnmanagedRule

router = APIRouter(prefix="/rules", tags=["rules"])


def _load_options():
    return [
        selectinload(FirewallRule.targets),
        selectinload(FirewallRule.groups).selectinload(DeviceGroup.hosts),
        selectinload(FirewallRule.deployments).selectinload(RuleDeployment.host),
    ]


async def _load_rule(db: AsyncSession, rule_id: uuid.UUID) -> FirewallRule:
    await policy_lock(db)
    rule = await db.get(FirewallRule, rule_id, options=_load_options())
    if not rule:
        raise api_error(404, ErrorCode.REGEL_NICHT_GEFUNDEN)
    return rule


def _to_read(
    rule: FirewallRule,
    hosts: list[OpnsenseHost],
    deployments: list[RuleDeployment],
) -> RuleRead:
    hosts_by_id = {host.id: host for host in effective_hosts(rule)}
    hosts_by_id.update({d.host_id: d.host for d in deployments if d.host is not None})
    state, counts = workflow(rule, deployments)
    return RuleRead(
        workflow_state=state,
        workflow_counts=counts,        id=rule.id,
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
        log=rule.log,
        created_at=rule.created_at,
        updated_at=rule.updated_at,
        target_host_ids=[host.id for host in rule.targets],
        target_group_ids=[g.id for g in rule.groups],
        effective_host_ids=[h.id for h in effective_hosts(rule)],
        deployments=[
            DeploymentResult(
                host_id=d.host_id,
                host_name=hosts_by_id[d.host_id].name if d.host_id in hosts_by_id else "?",
                status=d.status,
                message=d.message,
                revert_deadline=d.revert_deadline,
            )
            for d in deployments
        ],
    )


@router.get("", response_model=list[RuleRead])
async def list_rules(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(FirewallRule).options(*_load_options()))
    rules = result.scalars().unique().all()
    return [_to_read(rule, rule.targets, rule.deployments) for rule in rules]


@router.get("/unmanaged", response_model=list[UnmanagedRule])
async def list_unmanaged_rules(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Rules found on the host that OPNexus does not manage yet (live read)."""
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    try:
        return await rule_adopt_service.list_unmanaged(db, host)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.HOST_REQUEST_FAILED, host=host.name) from exc


@router.post("/adopt", response_model=RuleRead, status_code=201)
async def adopt_rule(payload: AdoptRequest, db: AsyncSession = Depends(get_db)):
    """Takes over a hand-made rule. Writes nothing to the firewall."""
    await policy_lock(db)
    host = await db.get(OpnsenseHost, payload.host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    try:
        rule = await rule_adopt_service.adopt(db, host, payload.rule_uuid, link_to=payload.link_to)
    except rule_adopt_service.RuleNotFoundOnHost as exc:
        raise api_error(404, ErrorCode.REGEL_NICHT_GEFUNDEN) from exc
    except rule_adopt_service.NotAdoptable as exc:
        raise api_error(409, ErrorCode.RULE_NOT_ADOPTABLE, reasons=", ".join(exc.reasons),
                        reason_details=json.dumps(exc.details, ensure_ascii=False)) from exc
    except LINK_ERRORS as exc:
        raise link_api_error(exc) from exc
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.HOST_REQUEST_FAILED, host=host.name) from exc
    await db.commit()
    # The new object sits in the session without its relationships loaded; db.get() would return it as-is.
    rule = (await db.execute(
        select(FirewallRule).where(FirewallRule.id == rule.id).options(*_load_options())
        .execution_options(populate_existing=True)
    )).scalar_one()
    return _to_read(rule, rule.targets, rule.deployments)


@router.post("", response_model=RuleRead, status_code=201)
async def create_rule(payload: RuleCreate, db: AsyncSession = Depends(get_db)):
    await policy_lock(db)
    hosts, groups = await resolve_targets(db, payload)

    rule = FirewallRule(
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
        log=payload.log,
        targets=hosts,
        groups=groups,
    )
    db.add(rule)
    await db.flush()
    revision(db, rule, "Regel angelegt")
    await db.commit()
    # All attributes we need (id, created_at, ...) were set client-side
    # before commit, and `hosts`/an empty deployment list are already in
    # hand — no need to touch any lazy relationship on `rule` here.
    return _to_read(rule, hosts, [])


@router.post("/{rule_id}/deploy", response_model=RuleRead)
async def deploy(rule_id: uuid.UUID, approval: DeployApproval | None = Body(default=None), db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    plan = preview(rule)
    if approval and approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    if any(d.status in ("pending", "revert_failed") for d in rule.deployments):
        raise api_error(409, ErrorCode.OFFENES_DEPLOYMENT_ZUERST_BESTAETIGEN_ODER_ROLLBACK_KLAEREN)
    hosts = [h for h in effective_hosts(rule) if h.enabled]
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
    await deploy_rule(db, rule, hosts)
    await db.refresh(rule, attribute_names=["deployments"])
    for d in rule.deployments:
        await db.refresh(d, attribute_names=["host"])
    return _to_read(rule, hosts, rule.deployments)


@router.post("/{rule_id}/confirm", response_model=RuleRead)
async def confirm(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    # Mirrors the deploy endpoint below: confirm_rule mutates the same
    # RuleDeployment objects already loaded onto rule.deployments (same
    # session, same identity map), so it's visible immediately with no
    # reload — deliberately avoiding an expire+db.get() round trip here,
    # which previously surfaced a MissingGreenlet bug under pool_pre_ping
    # (see rule_deploy_service history).
    rule = await _load_rule(db, rule_id)
    await confirm_rule(db, rule)
    return _to_read(rule, rule.targets, rule.deployments)


@router.delete("/{rule_id}", status_code=204)
async def delete_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    if any(d.status in ("pending", "revert_failed") for d in rule.deployments):
        raise api_error(409, ErrorCode.OFFENES_DEPLOYMENT_ZUERST_KLAEREN)
    try:
        await undeploy_rule(db, rule)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)) from exc
    await db.refresh(rule, attribute_names=["deployments"])
    await db.delete(rule)
    await db.commit()


async def resolve_targets(db, payload):
    hosts = list((await db.scalars(select(OpnsenseHost).where(OpnsenseHost.id.in_(payload.target_host_ids)))).all())
    groups = list((await db.scalars(select(DeviceGroup).where(DeviceGroup.id.in_(payload.target_group_ids)).options(selectinload(DeviceGroup.hosts)))).all())
    if len(hosts) != len(set(payload.target_host_ids)) or len(groups) != len(set(payload.target_group_ids)):
        raise api_error(400, ErrorCode.EIN_ODER_MEHRERE_ZIELE_WURDEN_NICHT_GEFUNDEN)
    return hosts, groups


@router.put("/{rule_id}", response_model=RuleRead)
async def edit_rule(rule_id: uuid.UUID, payload: RuleEdit, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    ensure_editable(rule)
    if rule.updated_at != payload.expected_updated_at:
        raise api_error(409, ErrorCode.REGEL_WURDE_INZWISCHEN_GEAENDERT_NEU_LADEN_UND_AENDERUNGEN_BF51B6B0)
    hosts, groups = await resolve_targets(db, payload)
    if not await db.scalar(select(RuleRevision.id).where(RuleRevision.rule_id == rule.id).limit(1)):
        revision(db, rule, "Vor erster Bearbeitung")
    for field in RULE_FIELDS:
        setattr(rule, field, getattr(payload, field))
    rule.targets, rule.groups = hosts, groups
    # Association-only changes must also invalidate optimistic edits.
    from datetime import datetime, timezone
    rule.updated_at = datetime.now(timezone.utc)
    revision(db, rule, "Entwurf gespeichert")
    await db.commit()
    return _to_read(rule, hosts, rule.deployments)


@router.get("/{rule_id}/preview")
async def preview_rule(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return preview(await _load_rule(db, rule_id))


@router.get("/{rule_id}/revisions")
async def list_revisions(rule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _load_rule(db, rule_id)
    rows = (await db.scalars(select(RuleRevision).where(RuleRevision.rule_id == rule_id).order_by(RuleRevision.created_at.desc(), RuleRevision.id))).all()
    return [{"id": r.id, "created_at": r.created_at, "note": r.note, "snapshot": r.snapshot} for r in rows]


@router.post("/{rule_id}/deployments/{host_id}/remove", status_code=204)
async def remove_previous_target(rule_id: uuid.UUID, host_id: uuid.UUID, approval: DeployApproval, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    plan = preview(rule)
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
        await opnsense_client.del_rule(deployment.host, deployment.opnsense_rule_uuid)
        await opnsense_client.apply(deployment.host)
    except OpnsenseAPIError as exc:
        deployment.status = "error"
        deployment.message = str(exc)
        await db.commit()
        raise api_error(502, ErrorCode.ENTFERNEN_FEHLGESCHLAGEN_DIE_ZUORDNUNG_BLEIBT_FUER_EINEN_NEUEN_4CF90E4C) from exc
    await db.delete(deployment)
    await db.commit()


@router.post("/{rule_id}/deployments/{host_id}/retry-rollback", status_code=204)
async def retry_rollback(rule_id: uuid.UUID, host_id: uuid.UUID, approval: DeployApproval, db: AsyncSession = Depends(get_db)):
    rule = await _load_rule(db, rule_id)
    plan = preview(rule)
    if approval.preview_token != plan["token"]:
        raise api_error(409, ErrorCode.DEPLOYMENT_WURDE_GEAENDERT_VORSCHAU_AKTUALISIEREN)
    entry = next((h for h in plan["hosts"] if h["host_id"] == str(host_id)), None)
    if entry is None:
        raise api_error(404, ErrorCode.DEPLOYMENT_NICHT_GEFUNDEN)
    if not entry["can_retry_rollback"]:
        raise api_error(409, ErrorCode.FUER_DIESES_ZIEL_LIEGT_KEIN_WIEDERHOLBARER_ROLLBACK_VOR)
    deployment = next(d for d in rule.deployments if d.host_id == host_id)
    reverted = await revert_deployment(db, deployment)
    await db.commit()
    if not reverted:
        raise api_error(502, ErrorCode.ROLLBACK_ERNEUT_FEHLGESCHLAGEN_PRUEFE_DIE_ERREICHBARKEIT_DER_FIREWALL)
