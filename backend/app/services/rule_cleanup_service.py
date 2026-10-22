"""Rule cleanup (1.31, B4 part 2; DESIGN-1.31): switch hand-made rules off, delete them later, restore them.

Every write to a firewall goes through preview -> deploy -> confirm with an automatic revert, like the other
write paths (services/deploy_flow.py), but per rule and per cluster node instead of per managed object.

Interface for the router (package 6; every function raises ``api_error`` and can be returned 1:1):

    preview(db, host, actor=, actor_id=, kind=, uuids=, unused_days=, acknowledge=, ack_text_version=)
        kind "disable" (candidates of the analysis), "enable" (re-enable a rule the cleanup switched off and the
        admin confirmed, or a restored one) or "delete" (rules that have been switched off long enough).
        Operator and admin. Stores a 5-minute preview bound to the actor and a fingerprint of the live state.
    cancel(db, host, op_id, actor=)                          only the preview's own actor
    deploy(db, host, op_id, actor=, confirm_name=)           ADMIN ONLY (router + api_token_service, §4)
    confirm(db, host, op_id)                                 operator and admin; ONLY FROM A BROWSER SESSION, never
                                                             with an API token (coordinator's decision: a script must
                                                             not be able to confirm blindly). The service has no
                                                             special path for that - the router enforces it.
    revert_now(db, host, op_id)                              admin; operator only with operators_may_deploy
    retry(db, host, op_id)                                   ADMIN ONLY: revert_failed -> revert again; delete/restore
                                                             with an unresolved item -> read the firewall again
    restore_item(db, host, item_id, actor=, actor_id=, confirm_name=)   ADMIN ONLY
    list_operations(db, host) / get_operation(db, host, op_id)          read
    sweep_expired_cleanups(db)                               background loop (main.py)
    has_open_cleanup(db, host_ids)                           for the maintenance/host-deletion locks (package 6)

Safety rules (DESIGN-1.31 §2/§3), each with a test and a mutation check (tests/test_rule_cleanup*.py):
* The plan is recomputed from the LIVE firewall at preview and again at deploy; the deploy writes nothing unless the
  fingerprint is identical (rule rows incl. position and ``enabled``, covering rule, CARP roles, protection
  results, categories and confidences, external changes, options). It samples the counters first, so a hit since
  the preview changes the plan.
* Management access: without configured management sources everything is locked; a rule that could match any
  "management source -> firewall API/GUI" packet (or whose match is unknown) is never switched off
  (rule_analysis/protection.py). CARP/pfsync rules are never offered.
* Confidence (§3.1): "unknown" never; "probable" only with an explicit acknowledgement per rule; "unused" is never
  "sure" below a 30-day window, and never "sure" for a block/reject rule; an external change in the window caps at
  "probable".
* Cluster: the same UUID on every node (v1; otherwise refused), exactly one MASTER, every node readable. Writes go
  BACKUP first, reverts MASTER first. "unused" only when it holds on every node (rule_hit_service.ClusterHit).
* Durable intent before every remote call: the operation is ``running`` with a heartbeat and an armed revert
  deadline before the first toggle; each item is committed as ``disabling`` before its toggle. A crash leaves an
  operation the sweeper reverts once its heartbeat is stale or its deadline passed.
* After an error nothing more is written forward, but the rules already switched are applied (otherwise the next
  foreign apply would make them live unnoticed), and the deadline stays armed.
* Every apply resets pf's counters (live, see rule_hit_service): the counters are sampled right before each apply.
* Confirm re-reads every node; an unreachable node refuses the confirmation (that is the lock-out signal).
* State transitions that two processes could race for are conditional UPDATEs (preview -> running,
  awaiting_confirm -> confirmed / -> reverting, abandoned running -> reverting only with the heartbeat seen);
  ``policy_lock`` serialises the rest, the partial unique index ``uq_cleanup_rule_open`` keeps one unfinished item
  per rule and node.
* Delete: only rules switched off by a confirmed cleanup at least ``cleanup_delete_min_days`` ago (or seen
  disabled and unchanged for the window, "disabled_long"), unchanged since; the full ``getRule`` snapshot and the
  position anchors are committed BEFORE ``delRule``. A disabled rule is not loaded in pf (live, P4), so deleting it
  changes nothing in pf and has no confirmation window.
* Restore: ``addRule`` from the snapshot, always DISABLED (a new UUID: the history chain is linked through the
  items and the audit, ``restored_from``), then ``moveRuleBefore`` the next model rule of the same prio_group;
  a position that cannot be restored is only a warning. Re-enabling is an "enable" operation with a window.

Live evidence (2026-10-22, OPNsense 26.7.7, lab firewalls only, scratch rules `opnexus-b4-probe*`, removed after):
* P4: after toggleRule/{uuid}/0 the rule's label is still in pfStatistics/rules until apply; after apply it is gone
  from pfStatistics and from ruleStats. Deleting the disabled rule and applying left the pf ruleset identical (same
  81 lines before and after). After toggleRule/{uuid}/1 + apply the label is back.
* P5: an apply without any pending change zeroes every ruleStats counter (see rule_hit_service).
* P6: on the lab HA pair the UUIDs of the rules are identical, sort_order/sequence are not (pairing by UUID only).
  The pair has no config sync target (synchronizetoip empty), so "does a sync overwrite a backup-only change" was
  not observable; the write order (backup first) and the refusal to confirm a half-switched pair do not rely on it.
* End to end (this module with a throwaway database, cluster test-fw-1 MASTER / test-fw-2 BACKUP): management rule
  refused, stale preview refused without a write, disable backup->master, labels gone, history origin "opnexus",
  sweeper revert master->backup, disable + confirm, delete refused before the waiting time and done after it,
  snapshot + anchor, restore on both nodes disabled right before the anchor (getRule identical except
  uuid/sequence/sort_order/audit), enable + confirm.

Item state names describe the disable case; for "enable" ``disabling``/``disabled`` mean "switching"/"switched".
Item and operation ``message`` hold an ErrorCode name (``errors.<CODE>`` in the UI) or the firewall's own text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid as uuid_lib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.config import settings
from app.errors import ErrorCode, api_error
from app.models.governance import GovernanceSettings
from app.models.host import OpnsenseHost
from app.models.policy_hierarchy_rule_deployment import PolicyHierarchyRuleDeployment
from app.models.rule_cleanup import OPEN_ITEM_STATES, RuleCleanupItem, RuleCleanupOperation
from app.models.rule_history import LiveRuleState, RuleChangeEvent
from app.services import audit_service
from app.services import opnsense_client as client
from app.services import rule_history_service as rhs
from app.services import rule_hit_service
from app.services.opnsense_client import OpnsenseAmbiguousError, OpnsenseAPIError, OpnsenseNotFoundError
from app.services.rule_analysis import cleanup as cleanup_mod
from app.services.rule_analysis import protection
from app.services.rule_analysis import service as analysis
from app.services.rule_analysis.model import NormRule
from app.services.rule_analysis.normalize import _order

logger = logging.getLogger(__name__)

KINDS = ("disable", "enable", "delete")
SWITCH_KINDS = ("disable", "enable")
PREVIEW_TTL = timedelta(minutes=5)
# "unused" is never "sure" with a shorter window: monthly and quarterly jobs (DESIGN-1.31 §3.1).
MIN_SURE_UNUSED_DAYS = 30
MAX_RULES_CEILING = 25
# Version of the fixed acknowledgement text the UI shows for a "probable" rule (cleanup.ack.*).
ACK_TEXT_VERSION = "1"
# Operations that block every other change of the host/cluster until they are resolved.
OPEN_OP_STATUSES = ("running", "awaiting_confirm", "partial", "apply_failed", "reverting", "revert_failed")
# Swept once their revert deadline passed.
SWEEP_STATUSES = ("awaiting_confirm", "partial", "apply_failed")
REVERTIBLE_STATUSES = SWEEP_STATUSES
# Item states that may still differ from the rule's original state (a revert switches them back).
_CHANGED = ("disabling", "disabled", "unknown", "reverting", "revert_failed")
HEARTBEAT_GRACE = timedelta(seconds=60)
_UUID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RANK = {"unknown": 0, "probable": 1, "sure": 2}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _lock(db: AsyncSession) -> None:
    from app.services.policy_service import policy_lock
    await policy_lock(db)


async def _commit(db: AsyncSession) -> None:
    """Commit (makes the state durable) and take the lock again at once, like deploy_flow._checkpoint."""
    await db.commit()
    await _lock(db)


def _window() -> timedelta:
    return timedelta(seconds=settings.deploy_confirm_window_seconds)


def _budget(items: int, nodes: int) -> timedelta:
    """Upper bound for the remote calls of one run: a toggle and a read-back per item, a sample, an apply and a
    read per node, each at most one client timeout."""
    return timedelta(seconds=(2 * items + 3 * nodes) * settings.opnsense_timeout)


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true", "yes", "on")


def _row_fp(row: dict) -> str:
    return rhs.fingerprint(rhs.clean_row(row))


def _content_fp(row: dict, enabled: bool | None = None) -> str:
    """The rule's own content: without its UUID and position (other rules being inserted renumber it) and, when
    given, with ``enabled`` set to that value."""
    clean = {k: v for k, v in rhs.clean_row(row).items() if k not in rhs.ORDER_FIELDS and k != "uuid"}
    if enabled is not None:
        clean["enabled"] = "1" if enabled else "0"
    return rhs.fingerprint(clean)


def _group(row: dict) -> str:
    raw = str(row.get("prio_group") or "")
    if raw:
        return raw
    return str(row.get("sort_order") or "").partition(".")[0]


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _cap(level: str | None, cap: str) -> str | None:
    if level is None:
        return None
    return level if _RANK[level] <= _RANK[cap] else cap


# ── Settings, locks ───────────────────────────────────────────────────────

@dataclass
class Limits:
    max_rules: int
    delete_min_days: int
    sources: list
    ports: list = field(default_factory=list)


async def _limits(db: AsyncSession) -> Limits:
    row = await db.get(GovernanceSettings, 1)
    max_rules = int(getattr(row, "cleanup_max_rules", None) or 10)
    min_days = int(getattr(row, "cleanup_delete_min_days", None) or 7)
    ports = sorted({int(p) for p in (getattr(row, "cleanup_management_ports", None) or [])
                    if str(p).isdigit() and 1 <= int(p) <= 65535})
    return Limits(max(1, min(MAX_RULES_CEILING, max_rules)), max(1, min_days),
                  list(getattr(row, "cleanup_management_sources", None) or []), ports)


async def _fleet(db: AsyncSession, host: OpnsenseHost) -> list[OpnsenseHost]:
    from app.services.host_maintenance_service import peers
    return await peers(db, host)


async def has_open_cleanup(db: AsyncSession, host_ids, *, exclude_op=None) -> bool:
    """An unresolved cleanup on any of the hosts: an open operation, or an item whose outcome is not settled."""
    ids = list(host_ids)
    if not ids:
        return False
    item_hosts = select(RuleCleanupItem.operation_id).where(RuleCleanupItem.host_id.in_(ids))
    ops = select(RuleCleanupOperation.id).where(
        RuleCleanupOperation.status.in_(OPEN_OP_STATUSES),
        or_(RuleCleanupOperation.host_id.in_(ids), RuleCleanupOperation.id.in_(item_hosts)))
    items = select(RuleCleanupItem.id).join(RuleCleanupOperation, RuleCleanupOperation.id == RuleCleanupItem.operation_id).where(
        RuleCleanupItem.host_id.in_(ids), RuleCleanupItem.state.in_(OPEN_ITEM_STATES),
        RuleCleanupOperation.status != "preview")
    if exclude_op is not None:
        ops = ops.where(RuleCleanupOperation.id != exclude_op)
        items = items.where(RuleCleanupItem.operation_id != exclude_op)
    return bool(await db.scalar(ops.limit(1)) or await db.scalar(items.limit(1)))


async def _ensure_free(db: AsyncSession, host: OpnsenseHost, fleet: list[OpnsenseHost], *, exclude_op=None) -> None:
    """Maintenance, an open restore, an unconfirmed deployment or another cleanup anywhere in the cluster."""
    from app.services.host_maintenance_service import DEPLOYMENTS, ensure_no_maintenance
    await ensure_no_maintenance(db, [host], cleanup=False)
    ids = [h.id for h in fleet]
    for model in (*DEPLOYMENTS, PolicyHierarchyRuleDeployment):
        if await db.scalar(select(model.id).where(model.host_id.in_(ids), model.status.in_(("pending", "revert_failed"))).limit(1)):
            raise api_error(409, ErrorCode.BEREINIGUNG_OFFENE_OPERATION, reason="deployment")
    if await has_open_cleanup(db, ids, exclude_op=exclude_op):
        raise api_error(409, ErrorCode.BEREINIGUNG_OFFENE_OPERATION, reason="cleanup")


# ── Reading the cluster ───────────────────────────────────────────────────

@dataclass
class Node:
    host: OpnsenseHost
    role: str | None
    ctx: object
    rules: dict[str, NormRule]
    rows: dict[str, dict]
    candidates: dict[str, dict]
    managed: dict[str, dict]
    hasync: dict | None
    states: dict = field(default_factory=dict)


def _hasync_brief(data) -> dict | None:
    """Only the two fields the protection needs (the answer also carries the sync password)."""
    node = (data or {}).get("hasync") if isinstance(data, dict) else None
    if not isinstance(node, dict):
        return None
    return {"hasync": {"pfsyncinterface": node.get("pfsyncinterface"), "pfsyncpeerip": node.get("pfsyncpeerip")}}


async def _roles(fleet: list[OpnsenseHost], clustered: bool) -> dict:
    if not clustered:
        return {h.id: None for h in fleet}
    from app.services.host_status_service import get_statuses
    statuses = await get_statuses(fleet)
    for h in fleet:
        if statuses.get(h.id, {}).get("status") != "online":
            raise api_error(502, ErrorCode.BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR, host=h.name)
    roles = {h.id: statuses[h.id].get("carp_status") for h in fleet}
    if sorted(roles.values(), key=str) != sorted(["MASTER"] + ["BACKUP"] * (len(fleet) - 1)):
        # no master, two masters (split brain) or a node without a CARP role: the write order is not known
        raise api_error(409, ErrorCode.BEREINIGUNG_CLUSTER_UNVOLLSTAENDIG, reason="roles")
    return roles


def _write_order(nodes: list["Node"]) -> list["Node"]:
    """BACKUP before MASTER (a failure then never leaves the node carrying the traffic half-changed)."""
    return sorted(nodes, key=lambda n: (n.role == "MASTER", n.host.name))


async def _load_nodes(db: AsyncSession, host: OpnsenseHost, *, unused_days: int, now: datetime) -> list[Node]:
    fleet = await _fleet(db, host)
    clustered = bool(host.cluster_name)
    if not host.enabled:
        raise api_error(409, ErrorCode.HOST_IST_DEAKTIVIERT)
    if clustered:
        if len(fleet) < 2:
            raise api_error(409, ErrorCode.BEREINIGUNG_CLUSTER_UNVOLLSTAENDIG, reason="peer_missing")
        if any(not h.enabled for h in fleet):
            raise api_error(409, ErrorCode.BEREINIGUNG_CLUSTER_UNVOLLSTAENDIG, reason="peer_disabled")
    roles = await _roles(fleet, clustered)
    nodes: list[Node] = []
    for member in fleet:
        try:
            ctx, rules, view, managed = await analysis.cleanup_view(db, member, unused_days=unused_days, refresh=True, now=now)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - no partial preview: one unreadable node stops everything
            logger.info("Bereinigung: %s nicht lesbar: %s", member.name, exc)
            raise api_error(502, ErrorCode.BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR, host=member.name) from exc
        if not rule_hit_service.looks_complete({"rows": ctx.rows}):
            raise api_error(502, ErrorCode.BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR, host=member.name)
        try:
            hasync = _hasync_brief(await client.get_hasync(member))
        except OpnsenseAPIError:
            hasync = None  # protection.infrastructure_reason then assumes the worst on a cluster node
        states = {s.rule_uuid: s for s in (await db.scalars(select(LiveRuleState).where(LiveRuleState.host_id == member.id))).all()}
        nodes.append(Node(
            host=member, role=roles.get(member.id), ctx=ctx,
            rules={r.uuid.lower(): r for r in rules if r.uuid and not r.uuid.startswith("pf@")},
            rows={str(r.get("uuid")).lower(): r for r in ctx.rows if r.get("uuid")},
            candidates={str(c["uuid"]).lower(): c for c in view["candidates"] if c.get("uuid")},
            managed=managed, hasync=hasync, states=states,
        ))
    return _write_order(nodes)


# ── Planning (preview and the re-check at deploy) ─────────────────────────

def selectable_level(candidate: dict, rule: NormRule, *, unused_days: int, external: bool) -> tuple[str | None, list[str]]:
    """Confidence that the rule may be switched off (§3.1): the best category after the caps. None = no category
    that allows switching off (``disabled_long`` only feeds the delete stage)."""
    best, notes = None, []
    for category in candidate.get("categories") or []:
        kind, level = category.get("kind"), category.get("confidence")
        if kind == "disabled_long" or level not in _RANK:
            continue
        if kind == "unused":
            if rule.action in ("block", "reject"):
                level = _cap(level, "probable")  # a block rule without hits may still protect (rare attacks)
                notes.append("UNUSED_BLOCK_RULE")
            if unused_days < MIN_SURE_UNUSED_DAYS:
                level = _cap(level, "probable")
                notes.append("UNUSED_WINDOW_SHORT")
            notes += category.get("notes") or []
        if best is None or _RANK[level] > _RANK[best]:
            best = level
    if external and best is not None:
        best = _cap(best, "probable")
        notes.append("EXTERNAL_CHANGE")
    return best, sorted(set(notes))


async def _external(db: AsyncSession, host_id, uuids: list[str], since: datetime) -> dict[str, str]:
    """{rule uuid: id of the newest external change since ``since``} - "moved" events included (a move can end a
    shadowing)."""
    if not uuids:
        return {}
    rows = (await db.execute(select(RuleChangeEvent.rule_uuid, RuleChangeEvent.id, RuleChangeEvent.observed_at).where(
        RuleChangeEvent.host_id == host_id, RuleChangeEvent.rule_uuid.in_(uuids), RuleChangeEvent.origin == "external",
        RuleChangeEvent.observed_at >= since).order_by(RuleChangeEvent.observed_at))).all()
    return {u: str(i) for u, i, _ in rows}


class _Refusal(Exception):
    def __init__(self, status: int, code: ErrorCode, **params):
        self.status, self.code, self.params = status, code, params


async def _history_items(db: AsyncSession, host_id, rule_uuid: str) -> list[tuple[RuleCleanupItem, RuleCleanupOperation]]:
    """Items of this rule on this node in executed operations, newest first."""
    return list((await db.execute(select(RuleCleanupItem, RuleCleanupOperation).join(
        RuleCleanupOperation, RuleCleanupOperation.id == RuleCleanupItem.operation_id).where(
        RuleCleanupItem.host_id == host_id,
        or_(RuleCleanupItem.rule_uuid == rule_uuid, RuleCleanupItem.restored_uuid == rule_uuid),
        RuleCleanupOperation.status != "preview").order_by(RuleCleanupOperation.created_at.desc()))).all())


async def _uuid_map(db: AsyncSession, nodes: list[Node], kind: str, uuids: list[str]) -> dict[str, dict]:
    """{uuid as given (anchor node): {host_id: uuid on that node}}. The same UUID everywhere (v1 refuses a cluster
    without it), except for a restored rule: a restore creates a new UUID per node, linked by its operation."""
    mapping: dict[str, dict] = {}
    for u in uuids:
        mapping[u] = {n.host.id: u for n in nodes}
        if kind != "enable" or len(nodes) == 1:
            continue
        restored = (await db.execute(select(RuleCleanupItem).where(
            RuleCleanupItem.restored_uuid == u, RuleCleanupItem.state == "restored",
            RuleCleanupItem.host_id.in_([n.host.id for n in nodes])))).scalars().first()
        if restored is None:
            continue
        siblings = (await db.scalars(select(RuleCleanupItem).where(
            RuleCleanupItem.operation_id == restored.operation_id, RuleCleanupItem.rule_uuid == restored.rule_uuid,
            RuleCleanupItem.state == "restored"))).all()
        by_host = {s.host_id: s.restored_uuid for s in siblings}
        mapping[u] = {n.host.id: by_host.get(n.host.id) or u for n in nodes}
    return mapping


async def _plan(db: AsyncSession, nodes: list[Node], *, kind: str, uuids: list[str], unused_days: int,
                acknowledge: set[str], limits: Limits, now: datetime) -> dict:
    """What the operation would do, recomputed from the live state, or a refusal (api_error)."""
    sources = protection.parse_sources(limits.sources)
    if not sources:
        raise api_error(409, ErrorCode.BEREINIGUNG_VERWALTUNGSQUELLEN_FEHLEN)
    clustered = len(nodes) > 1
    mapping = await _uuid_map(db, nodes, kind, uuids)
    window_start = now - timedelta(days=unused_days)
    plan_nodes = []
    problems: list[dict] = []
    missing_ack: list[str] = []
    for node in nodes:
        targets, ports = protection.management_targets(node.host.base_url, node.ctx, limits.ports)
        node_uuids = [mapping[u][node.host.id] for u in uuids]
        rows = node.rows
        covers = {c.get("by", {}).get("uuid", "").lower() for u in node_uuids
                  for c in (node.candidates.get(u.lower(), {}).get("categories") or []) if c.get("by")}
        external = await _external(db, node.host.id, [u.lower() for u in node_uuids] + sorted(covers - {""}),
                                   window_start if kind == "disable" else now - timedelta(days=3650))
        items = []
        for seq, (given, u) in enumerate(zip(uuids, node_uuids)):
            key = u.lower()
            row, rule = rows.get(key), node.rules.get(key)

            def refuse(reason: str) -> None:
                problems.append({"uuid": given, "host": node.host.name, "reason": reason})

            if row is None or rule is None:
                if clustered and kind == "delete" and await _already_deleted(db, node.host.id, u):
                    continue  # a cluster delete that stopped half-way: finish it on the nodes that still have it
                if clustered:
                    raise api_error(409, ErrorCode.BEREINIGUNG_CLUSTER_UNVOLLSTAENDIG, reason="uuid_missing", uuid=given, host=node.host.name)
                refuse("not_candidate")
                continue
            block = cleanup_mod.removal_block(rule, node.managed.get(key))
            if block:
                refuse(block)
                continue
            item = {
                "uuid": rule.uuid, "given": given, "seq": seq, "description": rule.description or rule.uuid,
                "fp": _row_fp(row), "row": _jsonable(rhs.clean_row(row)), "action": rule.action,
                "prio_group": _group(row), "sort_order": str(row.get("sort_order") or ""),
                "categories": [], "level": None, "needs_ack": False, "notes": [], "cover_uuid": None, "cover_fp": None,
                "protection": None, "external": external.get(key),
            }
            if kind == "disable":
                if not rule.enabled:
                    refuse("not_candidate")
                    continue
                candidate = node.candidates.get(key)
                if candidate is None:
                    refuse("not_candidate")
                    continue
                infra = protection.infrastructure_reason(rule, node.ctx, hasync=node.hasync, clustered=clustered)
                if infra:
                    refuse(infra)
                    continue
                check = protection.management_check(rule, node.ctx, sources, targets, ports)
                item["protection"] = check
                if check["result"] != "false":
                    refuse("management")
                    continue
                cover = next((c["by"]["uuid"] for c in candidate["categories"] if c.get("by")), None)
                if cover:
                    item["cover_uuid"] = cover
                    cover_row = rows.get(cover.lower())
                    item["cover_fp"] = _row_fp(cover_row) if cover_row else None
                level, notes = selectable_level(candidate, rule, unused_days=unused_days,
                                                external=bool(external.get(key) or (cover and external.get(cover.lower()))))
                item["categories"] = [{k: v for k, v in c.items() if k in ("kind", "confidence", "notes", "days")}
                                      for c in candidate["categories"]]
                item["level"], item["notes"] = level, notes
                if level is None:
                    refuse("not_candidate")
                    continue
                if level == "unknown":
                    refuse("confidence")
                    continue
                item["needs_ack"] = level == "probable"
                if level == "probable" and given not in acknowledge:
                    missing_ack.append(given)
            elif kind == "enable":
                if rule.enabled:
                    refuse("not_candidate")
                    continue
                history = await _history_items(db, node.host.id, rule.uuid)
                eligible = any((op.kind == "disable" and i.state == "confirmed" and i.rule_uuid == rule.uuid)
                               or (op.kind == "restore" and i.state == "restored" and i.restored_uuid == rule.uuid)
                               for i, op in history[:1])
                if not eligible:
                    refuse("not_candidate")
                    continue
                item["protection"] = protection.management_check(rule, node.ctx, sources, targets, ports)
                # Switching a block/reject rule back ON can cut off the management path; the auto-revert then only
                # works if OPNexus itself still reaches the firewall. Unless the rule is proven not to touch
                # management traffic: an explicit acknowledgement per rule (review finding 7).
                if rule.action != "pass" and item["protection"]["result"] != "false":
                    item["needs_ack"] = True
                    item["notes"] = ["ENABLE_MAY_BLOCK_MANAGEMENT"]
                    if given not in acknowledge:
                        missing_ack.append(given)
            else:  # delete
                if rule.enabled:
                    refuse("not_candidate")
                    continue
                item["basis"] = await _delete_basis(db, node, rule, row, limits=limits, unused_days=unused_days, now=now)
                if item["basis"].get("refused"):
                    problems.append({"uuid": given, "host": node.host.name, **item["basis"]["refused"]})
                    continue
            items.append(item)
        plan_nodes.append({"host_id": str(node.host.id), "host_name": node.host.name, "base_url": node.host.base_url,
                           "role": node.role, "items": items})
    if problems:
        first = problems[0]
        if first.get("code") == "wait":
            raise api_error(409, ErrorCode.BEREINIGUNG_WARTEZEIT_NICHT_ERREICHT, uuid=first["uuid"], host=first["host"],
                            days=first.get("days", 0), rules=problems)
        if first.get("code") == "stale":
            raise api_error(409, ErrorCode.BEREINIGUNG_VORSCHAU_VERALTET, uuids=", ".join(p["uuid"] for p in problems), rules=problems)
        raise api_error(409, ErrorCode.BEREINIGUNG_REGEL_NICHT_AENDERBAR, uuid=first["uuid"], host=first["host"],
                        reason=first["reason"], rules=problems)
    for given in uuids:  # every rule must still be somewhere (a finished cluster delete has nothing left to do)
        if not any(i["given"] == given for n in plan_nodes for i in n["items"]):
            raise api_error(409, ErrorCode.BEREINIGUNG_REGEL_NICHT_AENDERBAR, uuid=given, host=nodes[0].host.name,
                            reason="not_candidate", rules=[])
    if missing_ack:
        raise api_error(409, ErrorCode.BEREINIGUNG_BESTAETIGUNG_FEHLT, uuids=", ".join(sorted(set(missing_ack))))
    return _jsonable({
        "kind": kind, "uuids": uuids, "unused_days": unused_days, "sources": [str(s) for s in sources],
        "ports": list(limits.ports),
        "acknowledged": sorted(acknowledge & set(uuids)), "nodes": plan_nodes,
    })


async def _already_deleted(db: AsyncSession, host_id, rule_uuid: str) -> bool:
    """The newest executed item of this rule on this node deleted it (and nothing brought it back)."""
    history = await _history_items(db, host_id, rule_uuid)
    return bool(history) and history[0][1].kind == "delete" and history[0][0].state == "deleted" \
        and history[0][0].rule_uuid == rule_uuid and not history[0][0].restored_uuid


async def _delete_basis(db: AsyncSession, node: Node, rule: NormRule, row: dict, *, limits: Limits,
                        unused_days: int, now: datetime) -> dict:
    """Why a disabled rule may be deleted (§2b): (i) switched off by a confirmed cleanup at least
    ``delete_min_days`` ago and unchanged since, or (ii) "disabled_long" with "sure"."""
    # A delete attempt that changed nothing (refused / never tried) does not count: the confirmed switch-off before
    # it is still the basis (a cluster delete that failed on one node can be finished there).
    history = [(i, o) for i, o in await _history_items(db, node.host.id, rule.uuid)
               if not (o.kind == "delete" and i.state in ("failed", "skipped"))]
    if history:
        item, op = history[0]
        if op.kind == "disable" and item.state == "confirmed" and item.rule_uuid == rule.uuid and op.confirmed_at:
            due = op.confirmed_at + timedelta(days=limits.delete_min_days)
            if now < due:
                return {"refused": {"code": "wait", "reason": "wait", "days": max(1, (due - now).days + 1)}}
            if _content_fp(row, False) != _content_fp(item.row_before or {}, False):
                return {"refused": {"code": "stale", "reason": "changed"}}
            changed = await db.scalar(select(RuleChangeEvent.id).where(
                RuleChangeEvent.host_id == node.host.id, RuleChangeEvent.rule_uuid == rule.uuid.lower(),
                RuleChangeEvent.origin == "external", RuleChangeEvent.kind != "moved",
                RuleChangeEvent.observed_at >= op.confirmed_at).limit(1))
            if changed:
                return {"refused": {"code": "stale", "reason": "external"}}
            return {"type": "confirmed", "item_id": str(item.id), "confirmed_at": op.confirmed_at.isoformat()}
    candidate = node.candidates.get(rule.uuid.lower()) or {}
    long = next((c for c in candidate.get("categories") or [] if c.get("kind") == "disabled_long"), None)
    if long and long.get("confidence") == "sure":
        return {"type": "disabled_long", "since": str(long.get("since"))}
    return {"refused": {"code": "wait", "reason": "wait", "days": limits.delete_min_days}}


def plan_fingerprint(plan: dict) -> str:
    """Everything the preview showed that must still hold at deploy (the clean rows are covered by ``fp``)."""
    reduced = {
        "kind": plan["kind"], "uuids": plan["uuids"], "unused_days": plan["unused_days"], "sources": plan["sources"],
        "ports": plan.get("ports") or [],
        "acknowledged": plan["acknowledged"],
        "nodes": [{
            "host_id": n["host_id"], "base_url": n["base_url"], "role": n["role"],
            "items": [{k: i.get(k) for k in ("uuid", "seq", "fp", "cover_uuid", "cover_fp", "level", "needs_ack",
                                             "external", "basis")}
                      | {"categories": [(c.get("kind"), c.get("confidence")) for c in i.get("categories") or []],
                         "protection": (i.get("protection") or {}).get("result")}
                      for i in n["items"]],
        } for n in plan["nodes"]],
    }
    return hashlib.sha256(json.dumps(reduced, sort_keys=True, default=str).encode()).hexdigest()


def _stale_uuids(old: dict, new: dict) -> list[str]:
    def index(plan):
        return {(n["host_id"], i["given"]): plan_fingerprint({**plan, "nodes": [{**n, "items": [i]}]})
                for n in plan["nodes"] for i in n["items"]}
    before, after = index(old), index(new)
    changed = {key[1] for key in set(before) | set(after) if before.get(key) != after.get(key)}
    return sorted(changed) or list(old.get("uuids") or [])


def _check_selection(kind: str, uuids, unused_days, limits: Limits) -> tuple[list[str], int]:
    if kind not in KINDS:
        raise api_error(422, ErrorCode.BEREINIGUNG_AUSWAHL_UNGUELTIG, field="kind")
    cleaned: list[str] = []
    for value in uuids or []:
        text = str(value or "").strip()
        if not _UUID.match(text):
            raise api_error(422, ErrorCode.BEREINIGUNG_AUSWAHL_UNGUELTIG, field="uuids")
        if text not in cleaned:
            cleaned.append(text)
    if not cleaned:
        raise api_error(422, ErrorCode.BEREINIGUNG_AUSWAHL_UNGUELTIG, field="uuids")
    if len(cleaned) > limits.max_rules:
        raise api_error(422, ErrorCode.BEREINIGUNG_ZU_VIELE_REGELN, max=limits.max_rules, count=len(cleaned))
    days = max(cleanup_mod.MIN_DAYS, min(cleanup_mod.MAX_DAYS, int(unused_days or cleanup_mod.DEFAULT_UNUSED_DAYS)))
    return cleaned, days


# ── Audit ─────────────────────────────────────────────────────────────────

def _plan_item(op: RuleCleanupOperation, item: RuleCleanupItem) -> dict:
    for node in (op.options or {}).get("plan", {}).get("nodes", []):
        if node["host_id"] == str(item.host_id):
            for entry in node["items"]:
                if str(entry["uuid"]).lower() == item.rule_uuid.lower():
                    return entry
    return {}


def _audit(db: AsyncSession, op: RuleCleanupOperation, item: RuleCleanupItem, host: OpnsenseHost, *, action: str,
           status: str, message: str | None = None, response=None, extra: dict | None = None) -> None:
    """One audit row per item and step (§3.6), in the same transaction as the state change. ``remote_uuid`` makes
    the change show as "opnexus" in the rule's history (rule_history_service.AUDIT_KINDS)."""
    entry = _plan_item(op, item)
    details = {
        "remote_uuid": item.rule_uuid, "operation_kind": op.kind, "categories": item.categories,
        "confidence": item.confidence, "fp_before": item.fp_before, "row_before": item.row_before,
        "carp_role": item.carp_role, "preview_fingerprint": op.preview_fingerprint,
        "acknowledged": [a for a in (op.options or {}).get("acknowledged", []) if a.get("uuid") == entry.get("given")],
        "protection": _brief_protection(entry.get("protection")),
        # Firewall addresses and management sources: only an admin sees them (routers/audit.py strips the key).
        audit_service.ADMIN_ONLY_DETAILS: {"protection": entry.get("protection"),
                                           "management_sources": (op.options or {}).get("plan", {}).get("sources"),
                                           "management_ports": (op.options or {}).get("plan", {}).get("ports")},
        "response": response,
    }
    details.update(extra or {})
    db.add(audit_service.make_event(
        kind="rule_cleanup", action=action, status=status, entity_id=str(op.id), entity_name=(item.description or "")[:255],
        host_id=str(item.host_id), host_name=host.name, message=message, details=_jsonable(details)))


# ── Serialisation ─────────────────────────────────────────────────────────

def _iso(value):
    return value.isoformat() if value else None


def _read_item(item: RuleCleanupItem) -> dict:
    return {
        "id": str(item.id), "host_id": str(item.host_id), "rule_uuid": item.rule_uuid, "seq": item.seq,
        "carp_role": item.carp_role, "state": item.state, "description": item.description,
        "categories": item.categories, "confidence": item.confidence, "cover_uuid": item.cover_uuid,
        "prio_group": item.prio_group, "anchor_next_uuid": item.anchor_next_uuid, "anchor_prev_uuid": item.anchor_prev_uuid,
        "source_item_id": str(item.source_item_id) if item.source_item_id else None, "restored_uuid": item.restored_uuid,
        "changed_at": _iso(item.changed_at), "message": item.message, "has_snapshot": bool(item.snapshot),
        "restorable": item.state == "deleted" and bool(item.snapshot) and not item.restored_uuid,
    }


def _brief_protection(check: dict | None) -> dict | None:
    """The protection result without the packet that matched (it names a management source and a firewall address)."""
    if not isinstance(check, dict):
        return check
    return {k: v for k, v in check.items() if k != "match"}


def redact_plan(plan: dict | None) -> dict | None:
    """A plan without what is security-relevant for non-admins (review finding 8): the management sources and ports,
    the firewalls' base URLs and the matching packet of every protection check. Only whether sources are set stays."""
    if not isinstance(plan, dict):
        return plan
    out = {k: v for k, v in plan.items() if k not in ("sources", "ports")}
    out["sources_configured"] = bool(plan.get("sources"))
    out["nodes"] = [{**{k: v for k, v in n.items() if k != "base_url"},
                     "items": [{**i, "protection": _brief_protection(i.get("protection"))} for i in n.get("items", [])]}
                    for n in plan.get("nodes", [])]
    return out


def redact(payload: dict) -> dict:
    """An operation (or preview answer with ``plan`` and ``operation``) as a non-admin may see it."""
    out = dict(payload)
    if "plan" in out:
        out["plan"] = redact_plan(out["plan"])
    if isinstance(out.get("operation"), dict):
        out["operation"] = redact(out["operation"])
    return out


def read_operation(op: RuleCleanupOperation, items: list[RuleCleanupItem]) -> dict:
    states = {i.state for i in items}
    return {
        "id": str(op.id), "host_id": str(op.host_id), "cluster_name": op.cluster_name, "kind": op.kind,
        "status": op.status, "actor": op.actor, "created_at": _iso(op.created_at), "expires_at": _iso(op.expires_at),
        "started_at": _iso(op.started_at), "heartbeat_at": _iso(op.heartbeat_at),
        "revert_deadline": _iso(op.revert_deadline), "confirmed_at": _iso(op.confirmed_at),
        "finished_at": _iso(op.finished_at), "message": op.message,
        "options": {k: v for k, v in (op.options or {}).items() if k != "plan"},
        "plan": (op.options or {}).get("plan") if op.status == "preview" else None,
        "items": [_read_item(i) for i in sorted(items, key=lambda i: (i.carp_role != "BACKUP", str(i.host_id), i.seq))],
        "can_confirm": op.kind in SWITCH_KINDS and op.status == "awaiting_confirm" and not (states & {"failed", "unknown", "queued"}),
        "can_revert": op.kind in SWITCH_KINDS and op.status in REVERTIBLE_STATUSES,
        "can_retry": (op.kind in SWITCH_KINDS and op.status == "revert_failed")
                     or (op.kind not in SWITCH_KINDS and "unknown" in states and op.status not in ("running", "preview")),
    }


async def _items(db: AsyncSession, op: RuleCleanupOperation) -> list[RuleCleanupItem]:
    return list((await db.scalars(select(RuleCleanupItem).where(RuleCleanupItem.operation_id == op.id)
                                  .order_by(RuleCleanupItem.seq).execution_options(populate_existing=True))).all())


async def list_operations(db: AsyncSession, host: OpnsenseHost, limit: int = 50) -> list[dict]:
    fleet_ids = [h.id for h in await _fleet(db, host)]
    ops = (await db.scalars(select(RuleCleanupOperation).where(
        RuleCleanupOperation.host_id.in_(fleet_ids),
        or_(RuleCleanupOperation.status != "preview", RuleCleanupOperation.expires_at > _now()))
        .order_by(RuleCleanupOperation.created_at.desc()).limit(limit))).all()
    return [read_operation(op, await _items(db, op)) for op in ops]


async def _op_or_404(db: AsyncSession, host: OpnsenseHost, op_id, *, cluster: bool = False) -> RuleCleanupOperation:
    """The operation of this host. ``cluster``: also one anchored at another member of the SAME cluster (the cluster
    view shows the operation on every node; confirm/revert/retry/read act on the whole operation anyway)."""
    op = await db.get(RuleCleanupOperation, op_id, populate_existing=True)
    if op is None:
        raise api_error(404, ErrorCode.BEREINIGUNG_NICHT_GEFUNDEN)
    if op.host_id != host.id and not (cluster and host.cluster_name and op.host_id in {h.id for h in await _fleet(db, host)}):
        raise api_error(404, ErrorCode.BEREINIGUNG_NICHT_GEFUNDEN)
    return op


async def get_operation(db: AsyncSession, host: OpnsenseHost, op_id) -> dict:
    op = await _op_or_404(db, host, op_id, cluster=True)
    return read_operation(op, await _items(db, op))


# ── Preview ───────────────────────────────────────────────────────────────

async def preview(db: AsyncSession, host: OpnsenseHost, *, actor: str, actor_id=None, kind: str, uuids,
                  unused_days: int = cleanup_mod.DEFAULT_UNUSED_DAYS, acknowledge=(),
                  ack_text_version: str = ACK_TEXT_VERSION, now: datetime | None = None) -> dict:
    """Recompute everything from the live firewall(s) and store a preview (5 minutes, this actor, this host)."""
    now = now or _now()
    if str(ack_text_version) != ACK_TEXT_VERSION:
        # The acknowledgement goes into the audit with its text version: a page showing another (older) text must
        # not be able to record it (review finding 11). Reload the page.
        raise api_error(409, ErrorCode.BEREINIGUNG_BESTAETIGUNGSTEXT_VERALTET, expected=ACK_TEXT_VERSION)
    await _lock(db)
    limits = await _limits(db)
    selected, days = _check_selection(kind, uuids, unused_days, limits)
    fleet = await _fleet(db, host)
    await _ensure_free(db, host, fleet)
    nodes = await _load_nodes(db, host, unused_days=days, now=now)
    plan = await _plan(db, nodes, kind=kind, uuids=selected, unused_days=days,
                       acknowledge={str(a) for a in acknowledge or []}, limits=limits, now=now)
    fp = plan_fingerprint(plan)
    # One preview per actor and host: a new one replaces the old.
    await db.execute(update(RuleCleanupOperation).where(
        RuleCleanupOperation.host_id == host.id, RuleCleanupOperation.actor == actor,
        RuleCleanupOperation.status == "preview").values(status="cancelled", finished_at=now)
        .execution_options(synchronize_session=False))
    op = RuleCleanupOperation(
        host_id=host.id, cluster_name=host.cluster_name, kind=kind, status="preview", actor=actor, actor_id=actor_id,
        created_at=now, expires_at=now + PREVIEW_TTL, preview_fingerprint=fp,
        options={"unused_days": days, "uuids": selected,
                 "acknowledged": [{"uuid": u, "text_version": str(ack_text_version)[:16]} for u in plan["acknowledged"]],
                 "plan": plan},
    )
    db.add(op)
    await db.flush()
    # The middleware does not audit /preview paths: an own entry (no remote_uuid, so no history entry).
    db.add(audit_service.make_event(
        kind="rule_cleanup", action="preview", status="requested", entity_id=str(op.id), host_id=str(host.id),
        host_name=host.name, details=_jsonable({"plan": redact_plan(plan), "preview_fingerprint": fp,
                                                "acknowledged": op.options["acknowledged"],
                                                audit_service.ADMIN_ONLY_DETAILS: {"plan": plan}})))
    await db.commit()
    return {"preview_id": str(op.id), "expires_at": _iso(op.expires_at), "kind": kind, "plan": plan,
            "max_rules": limits.max_rules, "window_seconds": settings.deploy_confirm_window_seconds}


async def cancel(db: AsyncSession, host: OpnsenseHost, op_id, *, actor: str) -> dict:
    await _lock(db)
    op = await _op_or_404(db, host, op_id)
    if op.actor != actor:
        raise api_error(404, ErrorCode.BEREINIGUNG_NICHT_GEFUNDEN)
    done = await db.scalar(update(RuleCleanupOperation).where(
        RuleCleanupOperation.id == op.id, RuleCleanupOperation.status == "preview")
        .values(status="cancelled", finished_at=_now()).returning(RuleCleanupOperation.id))
    if not done:
        raise api_error(409, ErrorCode.BEREINIGUNG_AKTION_NICHT_MOEGLICH, status=op.status)
    await db.commit()
    await db.refresh(op)
    return read_operation(op, [])


# ── Deploy ────────────────────────────────────────────────────────────────

async def _sample(db: AsyncSession, host: OpnsenseHost, *, strict: bool) -> None:
    """Read pf's counters into the usage rows (commits). Right before an apply: whatever was hit since the last
    observation would otherwise be lost, because every apply zeroes the counters (P5)."""
    try:
        await rule_hit_service.sample_host(db, host)
    except OpnsenseAPIError as exc:
        # (both reads happen before sample_host touches the database: nothing to roll back)
        if strict:
            raise api_error(502, ErrorCode.BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR, host=host.name) from exc
        logger.info("Bereinigung: Zählerprobe vor dem Anwenden auf %s fehlgeschlagen: %s", host.name, exc)
    await _lock(db)


async def _preview_op(db: AsyncSession, host: OpnsenseHost, op_id, actor: str) -> RuleCleanupOperation:
    op = await _op_or_404(db, host, op_id)
    if op.actor != actor:
        raise api_error(404, ErrorCode.BEREINIGUNG_NICHT_GEFUNDEN)
    if op.status != "preview" or op.expires_at <= _now():
        raise api_error(409, ErrorCode.VORSCHAU_ABGELAUFEN_ODER_BEREITS_VERWENDET)
    return op


async def deploy(db: AsyncSession, host: OpnsenseHost, op_id, *, actor: str, confirm_name: str) -> dict:
    """Execute a preview. Admin only (enforced by the router). Nothing is written unless the live state still gives
    the same fingerprint."""
    await _lock(db)
    op = await _preview_op(db, host, op_id, actor)
    if confirm_name != host.name:
        raise api_error(409, ErrorCode.HOSTNAMEN_EXAKT_EINGEBEN)
    fleet = await _fleet(db, host)
    await _ensure_free(db, host, fleet)
    # Fresh counters first: a hit since the preview removes "unused" and so changes the fingerprint.
    for member in fleet:
        await _sample(db, member, strict=True)
    op = await _preview_op(db, host, op_id, actor)
    await _ensure_free(db, host, fleet)
    limits = await _limits(db)
    now = _now()
    stored = op.options.get("plan") or {}
    nodes = await _load_nodes(db, host, unused_days=int(op.options.get("unused_days") or 30), now=now)
    try:
        plan = await _plan(db, nodes, kind=op.kind, uuids=list(op.options.get("uuids") or []),
                           unused_days=int(op.options.get("unused_days") or 30),
                           acknowledge={a["uuid"] for a in op.options.get("acknowledged") or []}, limits=limits, now=now)
    except HTTPException as exc:  # no longer allowed at all (e.g. a hit since the preview): the preview is spent
        await _expire(db, op_id, f"{exc.detail.get('code') if isinstance(exc.detail, dict) else exc.detail}")
        raise
    if plan_fingerprint(plan) != op.preview_fingerprint:
        stale = _stale_uuids(stored, plan)
        await _expire(db, op.id, ErrorCode.BEREINIGUNG_VORSCHAU_VERALTET.value)
        raise api_error(409, ErrorCode.BEREINIGUNG_VORSCHAU_VERALTET, uuids=", ".join(stale))
    n_items = sum(len(n["items"]) for n in plan["nodes"])
    deadline = now + _window() + _budget(n_items, len(nodes)) if op.kind in SWITCH_KINDS else None
    claimed = await db.scalar(update(RuleCleanupOperation).where(
        RuleCleanupOperation.id == op.id, RuleCleanupOperation.status == "preview",
        RuleCleanupOperation.expires_at > now).values(
        status="running", started_at=now, heartbeat_at=now, revert_deadline=deadline)
        .returning(RuleCleanupOperation.id).execution_options(synchronize_session=False))
    if not claimed:
        raise api_error(409, ErrorCode.VORSCHAU_ABGELAUFEN_ODER_BEREITS_VERWENDET)
    by_host = {str(n.host.id): n for n in nodes}
    for plan_node in plan["nodes"]:
        node = by_host[plan_node["host_id"]]
        for entry in plan_node["items"]:
            db.add(RuleCleanupItem(
                operation_id=op.id, host_id=node.host.id, rule_uuid=entry["uuid"], seq=entry["seq"], carp_role=node.role,
                state="queued", description=entry["description"][:2000], categories=entry["categories"],
                confidence=entry["level"], row_before=entry["row"], fp_before=entry["fp"], cover_uuid=entry["cover_uuid"],
                cover_fp=entry["cover_fp"], prio_group=entry["prio_group"],
                source_item_id=uuid_lib.UUID(entry["basis"]["item_id"]) if (entry.get("basis") or {}).get("item_id") else None,
            ))
    try:
        await db.flush()
    except IntegrityError as exc:
        # uq_cleanup_rule_open: another operation holds one of the rules (a second backend got there first).
        await db.rollback()
        raise api_error(409, ErrorCode.BEREINIGUNG_OFFENE_OPERATION, reason="rule") from exc
    await _commit(db)  # durable intent: running + deadline + queued items, before the first remote call
    await db.refresh(op)
    if op.kind in SWITCH_KINDS:
        await _run_switch(db, op, nodes)
    else:
        await _run_delete(db, op, nodes)
    return read_operation(op, await _items(db, op))


async def _expire(db: AsyncSession, op_id, message: str) -> None:
    await db.execute(update(RuleCleanupOperation).where(
        RuleCleanupOperation.id == op_id, RuleCleanupOperation.status == "preview")
        .values(status="expired", finished_at=_now(), message=message[:1000])
        .execution_options(synchronize_session=False))
    await db.commit()


async def _claim_write(db: AsyncSession, op: RuleCleanupOperation, expect: str = "running") -> bool:
    """The heartbeat as a conditional UPDATE right before every remote write: only while the operation still has the
    status this process owns. 0 rows = another process took it over (the sweeper only takes over a stale heartbeat):
    stop without writing. The caller commits the heartbeat together with the item's intent state; between this
    UPDATE and that commit the lock is held, so nobody can take the operation in between."""
    now = _now()
    got = await db.scalar(update(RuleCleanupOperation).where(
        RuleCleanupOperation.id == op.id, RuleCleanupOperation.status == expect).values(heartbeat_at=now)
        .returning(RuleCleanupOperation.id).execution_options(synchronize_session=False))
    if got:
        set_committed_value(op, "heartbeat_at", now)
        return True
    await _commit(db)  # nothing pending: just release and take the lock again
    return False


async def _beat(db: AsyncSession, op: RuleCleanupOperation) -> None:
    op.heartbeat_at = _now()
    await _commit(db)


async def _read_enabled(host: OpnsenseHost, rule_uuid: str) -> bool | None:
    """The rule's ``enabled`` flag read back after an unclear answer; None = not readable (or gone)."""
    try:
        return _truthy((await client.get_rule(host, rule_uuid)).get("enabled", "1"))
    except OpnsenseAPIError:
        return None


async def _run_switch(db: AsyncSession, op: RuleCleanupOperation, nodes: list[Node]) -> None:
    target = op.kind == "enable"
    items = await _items(db, op)
    apply_failed = False
    stop = False
    for node in _write_order(nodes):
        node_items = [i for i in items if i.host_id == node.host.id]
        for item in node_items:
            if not await _claim_write(db, op):
                stop = True
                break
            item.state, item.changed_at = "disabling", _now()
            await _commit(db)  # heartbeat + intent, durable before the toggle
            try:
                response = await client.toggle_rule(node.host, item.rule_uuid, target)
                item.state = "disabled"
                _audit(db, op, item, node.host, action=op.kind, status="accepted", response=response)
            except OpnsenseAmbiguousError as exc:
                seen = await _read_enabled(node.host, item.rule_uuid)
                item.state = "disabled" if seen is target else ("failed" if seen is not None else "unknown")
                item.message = str(exc)[:1000]
                _audit(db, op, item, node.host, action=op.kind,
                       status="accepted" if item.state == "disabled" else ("error" if item.state == "failed" else "unknown"),
                       message=ErrorCode.BEREINIGUNG_AUSGANG_UNKLAR.value)
                stop = True  # the firewall stopped answering cleanly: no further write
            except OpnsenseAPIError as exc:
                item.state, item.message = "failed", str(exc)[:1000]
                _audit(db, op, item, node.host, action=op.kind, status="error", message=item.message)
                stop = True
            item.changed_at = _now()
            await _beat(db, op)
            if stop:
                break
        if any(i.state in ("disabled", "unknown") for i in node_items):
            # Also after an error: what is switched in the configuration must not wait for a foreign apply.
            await _sample(db, node.host, strict=False)
            try:
                await client.apply(node.host)
            except OpnsenseAPIError as exc:
                apply_failed = True
                op.message = f"{ErrorCode.BEREINIGUNG_ANWENDEN_FEHLGESCHLAGEN.value}: {node.host.name}: {exc}"[:1000]
                stop = True
            await _beat(db, op)
        if stop:
            break
    await db.refresh(op)
    if op.status != "running":
        return  # taken over (sweeper): it decides
    states = [i.state for i in items]
    now = _now()
    if not any(s in ("disabled", "unknown") for s in states):
        for item in items:
            if item.state == "queued":
                item.state, item.message = "skipped", ErrorCode.BEREINIGUNG_NICHT_AUSGEFUEHRT.value
        op.status, op.revert_deadline, op.finished_at = "failed", None, now
    elif apply_failed:
        op.status = "apply_failed"  # the armed deadline stays: the sweeper reverts
    elif any(s in ("failed", "unknown", "queued") for s in states):
        # stopped early (refusal, unclear answer, or another process took over): incomplete, never confirmable
        op.status, op.revert_deadline = "partial", now + _window()
    else:
        op.status, op.revert_deadline = "awaiting_confirm", now + _window()
    op.heartbeat_at = now
    await db.commit()


# ── Confirm / revert ──────────────────────────────────────────────────────

async def confirm(db: AsyncSession, host: OpnsenseHost, op_id) -> dict:
    """"I still have access": re-read every node (an unreachable node refuses: that IS the lock-out signal), every
    item must show the target state and otherwise its preview row. Browser session only (router)."""
    await _lock(db)
    op = await _op_or_404(db, host, op_id, cluster=True)
    if op.kind not in SWITCH_KINDS:
        raise api_error(409, ErrorCode.BEREINIGUNG_AKTION_NICHT_MOEGLICH, status=op.status)
    if op.status == "partial":
        raise api_error(409, ErrorCode.BEREINIGUNG_NICHT_BESTAETIGBAR, status=op.status)
    if op.status != "awaiting_confirm" or not op.revert_deadline or op.revert_deadline <= _now():
        raise api_error(409, ErrorCode.BEREINIGUNG_FRIST_ABGELAUFEN)
    items = await _items(db, op)
    if any(i.state in ("failed", "unknown", "queued") for i in items):
        raise api_error(409, ErrorCode.BEREINIGUNG_NICHT_BESTAETIGBAR, status="partial")
    target = op.kind == "enable"
    hosts = {i.host_id: await db.get(OpnsenseHost, i.host_id) for i in items}
    deviating: list[str] = []
    for host_id, member in hosts.items():
        try:
            search = await client.search_rules(member, all_rows=True)
        except OpnsenseAPIError as exc:
            raise api_error(502, ErrorCode.BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR, host=member.name) from exc
        if not rule_hit_service.looks_complete(search):
            raise api_error(502, ErrorCode.BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR, host=member.name)
        live = {str(r.get("uuid")).lower(): r for r in search.get("rows") or [] if isinstance(r, dict) and r.get("uuid")}
        for item in (i for i in items if i.host_id == host_id and i.state == "disabled"):
            row = live.get(item.rule_uuid.lower())
            if row is None or _truthy(row.get("enabled", "1")) is not target \
                    or _content_fp(row, target) != _content_fp(item.row_before or {}, target):
                deviating.append(item.rule_uuid)
    if deviating:
        raise api_error(409, ErrorCode.BEREINIGUNG_LIVESTAND_ABWEICHEND, uuids=", ".join(sorted(set(deviating))))
    now = _now()
    done = await db.scalar(update(RuleCleanupOperation).where(
        RuleCleanupOperation.id == op.id, RuleCleanupOperation.status == "awaiting_confirm",
        RuleCleanupOperation.revert_deadline > now).values(
        status="confirmed", confirmed_at=now, revert_deadline=None, finished_at=now)
        .returning(RuleCleanupOperation.id).execution_options(synchronize_session=False))
    if not done:  # the deadline passed while the firewall was read, or the sweeper took it
        raise api_error(409, ErrorCode.BEREINIGUNG_FRIST_ABGELAUFEN)
    for item in items:
        if item.state == "disabled":
            item.state, item.changed_at = "confirmed", now
            _audit(db, op, item, hosts[item.host_id], action="confirm", status="confirmed")
        elif item.state == "queued":
            item.state = "skipped"
    await db.commit()
    await db.refresh(op)
    return read_operation(op, items)


def _revert_order(items: list[RuleCleanupItem]) -> list:
    """MASTER before BACKUP: the node carrying the traffic gets its rules back first."""
    seen: list = []
    for item in sorted(items, key=lambda i: (i.carp_role != "MASTER", str(i.host_id))):
        if item.host_id not in seen:
            seen.append(item.host_id)
    return seen


async def _revert(db: AsyncSession, op: RuleCleanupOperation, *, allowed: tuple, heartbeat=None) -> bool:
    """Switch every changed item back (shared by the sweeper, "revert now" and "retry"). Claims the operation with a
    conditional UPDATE first; whoever loses does nothing. Errors are recorded per item, never retried here."""
    conditions = [RuleCleanupOperation.id == op.id, RuleCleanupOperation.status.in_(allowed)]
    if heartbeat is not None:  # taking over an abandoned run: only if nobody touched it since we looked
        conditions.append(RuleCleanupOperation.heartbeat_at == heartbeat if heartbeat is not False
                          else RuleCleanupOperation.heartbeat_at.is_(None))
    now = _now()
    claimed = await db.scalar(update(RuleCleanupOperation).where(*conditions).values(status="reverting", heartbeat_at=now)
                              .returning(RuleCleanupOperation.id).execution_options(synchronize_session=False))
    if not claimed:
        await db.rollback()
        await _lock(db)
        return False
    await _commit(db)
    await db.refresh(op)
    back = op.kind == "disable"  # disable -> switch back on; enable -> back off
    items = await _items(db, op)
    lost = False
    for host_id in _revert_order(items):
        member = await db.get(OpnsenseHost, host_id)
        node_items = [i for i in items if i.host_id == host_id and i.state in _CHANGED]
        touched = False
        unreachable = False
        for item in node_items:
            if unreachable or member is None:
                item.state, item.message = "revert_failed", ErrorCode.BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR.value
                continue
            if not await _claim_write(db, op, "reverting"):
                lost = True
                break
            item.state, item.changed_at = "reverting", _now()
            await _commit(db)
            try:
                response = await client.toggle_rule(member, item.rule_uuid, back)
                item.state, item.message = "reverted", None
                touched = True
                _audit(db, op, item, member, action="revert", status="accepted", response=response)
            except OpnsenseAmbiguousError as exc:
                seen = await _read_enabled(member, item.rule_uuid)
                touched = True
                if seen is back:
                    item.state, item.message = "reverted", None
                    _audit(db, op, item, member, action="revert", status="accepted", message=ErrorCode.BEREINIGUNG_AUSGANG_UNKLAR.value)
                else:
                    item.state, item.message = "revert_failed", str(exc)[:1000]
                    unreachable = seen is None
                    _audit(db, op, item, member, action="revert", status="unknown", message=item.message)
            except OpnsenseAPIError as exc:
                item.state, item.message = "revert_failed", str(exc)[:1000]
                _audit(db, op, item, member, action="revert", status="error", message=item.message)
            item.changed_at = _now()
            await _beat(db, op)
        if touched and member is not None:
            await _sample(db, member, strict=False)
            try:
                await client.apply(member)
            except OpnsenseAPIError as exc:
                for item in node_items:
                    if item.state == "reverted":
                        item.state = "revert_failed"
                        item.message = f"{ErrorCode.BEREINIGUNG_ANWENDEN_FEHLGESCHLAGEN.value}: {exc}"[:1000]
            await _beat(db, op)
        if lost:
            return True  # another process owns the revert now: it finishes it
    for item in items:
        if item.state == "queued":
            item.state, item.message = "skipped", ErrorCode.BEREINIGUNG_NICHT_AUSGEFUEHRT.value
    failed = any(i.state == "revert_failed" for i in items)
    op.status = "revert_failed" if failed else "reverted"
    op.revert_deadline, op.finished_at, op.heartbeat_at = None, _now(), _now()
    await _commit(db)
    return True


async def revert_now(db: AsyncSession, host: OpnsenseHost, op_id) -> dict:
    await _lock(db)
    op = await _op_or_404(db, host, op_id, cluster=True)
    if op.kind not in SWITCH_KINDS or not await _revert(db, op, allowed=REVERTIBLE_STATUSES):
        await db.refresh(op)  # _revert rolled back when it lost the claim: the status is expired
        raise api_error(409, ErrorCode.BEREINIGUNG_AKTION_NICHT_MOEGLICH, status=op.status)
    await db.commit()
    await db.refresh(op)
    return read_operation(op, await _items(db, op))


async def retry(db: AsyncSession, host: OpnsenseHost, op_id) -> dict:
    """revert_failed: revert again (never automatic). Delete/restore with an unresolved item: read the firewall."""
    await _lock(db)
    op = await _op_or_404(db, host, op_id, cluster=True)
    if op.kind in SWITCH_KINDS:
        if not await _revert(db, op, allowed=("revert_failed",)):
            await db.refresh(op)  # _revert rolled back when it lost the claim: the status is expired
            raise api_error(409, ErrorCode.BEREINIGUNG_AKTION_NICHT_MOEGLICH, status=op.status)
    else:
        items = [i for i in await _items(db, op) if i.state == "unknown"]
        if not items or op.status in ("running", "preview"):
            raise api_error(409, ErrorCode.BEREINIGUNG_AKTION_NICHT_MOEGLICH, status=op.status)
        for item in items:
            member = await db.get(OpnsenseHost, item.host_id)
            await _reconcile(db, op, item, member)
    await db.commit()
    await db.refresh(op)
    return read_operation(op, await _items(db, op))


async def _reconcile(db: AsyncSession, op: RuleCleanupOperation, item: RuleCleanupItem, member: OpnsenseHost) -> None:
    """An unknown delete/restore outcome, settled by reading only."""
    if op.kind == "delete":
        try:
            await client.get_rule(member, item.rule_uuid)
            item.state, item.message = "failed", None  # still there: the delete did not happen
        except OpnsenseNotFoundError:
            item.state, item.message = "deleted", None
            _audit(db, op, item, member, action="delete", status="accepted", extra={"snapshot": item.snapshot})
        except OpnsenseAPIError as exc:
            raise api_error(502, ErrorCode.BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR, host=member.name) from exc
    elif op.kind == "restore":
        verdict, found = await _find_restored(member, item)
        if verdict == "unclear":
            item.message = ErrorCode.BEREINIGUNG_AUSGANG_UNKLAR.value  # stays unknown
        elif verdict == "none":
            item.state = "failed"
        else:
            item.state, item.restored_uuid = "restored", found
            source = await db.get(RuleCleanupItem, item.source_item_id) if item.source_item_id else None
            if source is not None:
                source.restored_uuid = found
            _audit(db, op, item, member, action="restore", status="accepted",
                   extra={"remote_uuid": found, "restored_from": item.rule_uuid})
    item.changed_at = _now()


# ── Sweeper ───────────────────────────────────────────────────────────────

def _stale(op: RuleCleanupOperation, n_items: int, n_nodes: int, now: datetime) -> bool:
    if op.heartbeat_at is None:
        return True
    return op.heartbeat_at < now - (_budget(n_items, n_nodes) + HEARTBEAT_GRACE)


async def sweep_expired_cleanups(db: AsyncSession) -> list[str]:
    """Background safety net (main.py): reverts switch operations whose window passed unconfirmed and takes over
    abandoned ones (stale heartbeat), expires old previews, marks abandoned delete/restore runs. revert_failed is
    never retried automatically."""
    await _lock(db)
    now = _now()
    await db.execute(update(RuleCleanupOperation).where(
        RuleCleanupOperation.status == "preview", RuleCleanupOperation.expires_at <= now)
        .values(status="expired", finished_at=now).execution_options(synchronize_session=False))
    due = (await db.scalars(select(RuleCleanupOperation).where(
        RuleCleanupOperation.kind.in_(SWITCH_KINDS),
        or_(and_(RuleCleanupOperation.status.in_(SWEEP_STATUSES), RuleCleanupOperation.revert_deadline <= now),
            RuleCleanupOperation.status.in_(("running", "reverting"))))
        .order_by(RuleCleanupOperation.created_at).with_for_update(skip_locked=True)
        .execution_options(populate_existing=True))).all()
    handled: list[str] = []
    for op in due:
        if op.status in ("running", "reverting"):
            items = await _items(db, op)
            nodes = len({i.host_id for i in items}) or 1
            if not _stale(op, len(items), nodes, now):
                # alive (fresh heartbeat), even past its deadline: its own process finishes it and arms the window;
                # taking it over here would race its next write (review finding 2)
                continue
            done = await _revert(db, op, allowed=(op.status,), heartbeat=op.heartbeat_at or False)
        else:
            done = await _revert(db, op, allowed=SWEEP_STATUSES)
        if done:
            handled.append(str(op.id))
            logger.info("Bereinigung %s automatisch zurückgenommen (%s)", op.id, op.status)
    abandoned = (await db.scalars(select(RuleCleanupOperation).where(
        RuleCleanupOperation.kind.notin_(SWITCH_KINDS), RuleCleanupOperation.status == "running")
        .execution_options(populate_existing=True))).all()
    for op in abandoned:
        items = await _items(db, op)
        if not _stale(op, len(items), len({i.host_id for i in items}) or 1, now):
            continue
        claimed = await db.scalar(update(RuleCleanupOperation).where(
            RuleCleanupOperation.id == op.id, RuleCleanupOperation.status == "running",
            RuleCleanupOperation.heartbeat_at == op.heartbeat_at if op.heartbeat_at is not None
            else RuleCleanupOperation.heartbeat_at.is_(None)).values(
            status="failed", finished_at=now, message=ErrorCode.BEREINIGUNG_ABGEBROCHEN_VERWAIST.value)
            .returning(RuleCleanupOperation.id).execution_options(synchronize_session=False))
        if not claimed:
            continue
        for item in items:
            if item.state in ("deleting", "restoring"):
                item.state, item.message = "unknown", ErrorCode.BEREINIGUNG_ABGEBROCHEN_VERWAIST.value  # read with "retry"
            elif item.state == "queued":
                item.state, item.message = "skipped", ErrorCode.BEREINIGUNG_NICHT_AUSGEFUEHRT.value
        handled.append(str(op.id))
    await db.commit()
    return handled


# ── Delete ────────────────────────────────────────────────────────────────

def _anchors(rows: list[dict], rule_uuid: str, batch: set[str]) -> tuple[str | None, str | None, str | None]:
    """(prio_group, previous, next) model rule of the same prio_group by sort_order - legacy and automatic rules
    are not in the model and cannot be an anchor (P3), rules deleted in the same run neither."""
    me = next((r for r in rows if str(r.get("uuid")).lower() == rule_uuid.lower()), None)
    if me is None:
        return None, None, None
    group = _group(me)
    members = [r for r in rows if _group(r) == group and not _truthy(r.get("is_automatic")) and not _truthy(r.get("legacy"))
               and (r is me or str(r.get("uuid")).lower() not in batch)]
    members.sort(key=lambda r: _order(r)[0])
    index = members.index(me)
    prev = str(members[index - 1]["uuid"]) if index > 0 else None
    nxt = str(members[index + 1]["uuid"]) if index + 1 < len(members) else None
    return group, prev, nxt


async def _run_delete(db: AsyncSession, op: RuleCleanupOperation, nodes: list[Node]) -> None:
    items = await _items(db, op)
    batch = {i.rule_uuid.lower() for i in items}
    stop = False
    for node in _write_order(nodes):
        node_items = [i for i in items if i.host_id == node.host.id]
        for item in node_items:
            try:
                raw = await client.get_rule(node.host, item.rule_uuid)
            except OpnsenseAPIError as exc:
                item.state = "failed"
                item.message = ErrorCode.BEREINIGUNG_SNAPSHOT_FEHLT.value if isinstance(exc, OpnsenseNotFoundError) else str(exc)[:1000]
                _audit(db, op, item, node.host, action="delete", status="error", message=item.message)
                await _beat(db, op)
                stop = True
                break
            group, prev, nxt = _anchors(node.ctx.rows, item.rule_uuid, batch)
            live_row = node.rows.get(item.rule_uuid.lower()) or {}
            # The snapshot is committed BEFORE delRule: without it nothing is deleted.
            item.snapshot = _jsonable({"rule": client.rule_write_payload(raw), "raw": raw, "row": rhs.clean_row(live_row),
                                       "taken_at": _now(), "host": node.host.name})
            item.prio_group, item.anchor_prev_uuid, item.anchor_next_uuid = group, prev, nxt
            if not await _claim_write(db, op):
                stop = True
                break
            item.state, item.changed_at = "deleting", _now()
            await _commit(db)  # heartbeat + snapshot + intent, durable before delRule
            try:
                response = await client.del_rule(node.host, item.rule_uuid)
                if str((response or {}).get("result", "")).lower() == "deleted":
                    item.state = "deleted"
                else:  # "not found" proves nothing (P3: legacy rules answer it too) - read back
                    item.state = await _deleted_state(node.host, item.rule_uuid)
                _audit(db, op, item, node.host, action="delete",
                       status={"deleted": "accepted", "failed": "error"}.get(item.state, "unknown"),
                       response=response, extra={"snapshot": item.snapshot})
            except OpnsenseAmbiguousError as exc:
                item.state, item.message = await _deleted_state(node.host, item.rule_uuid), str(exc)[:1000]
                _audit(db, op, item, node.host, action="delete",
                       status={"deleted": "accepted", "failed": "error"}.get(item.state, "unknown"),
                       message=ErrorCode.BEREINIGUNG_AUSGANG_UNKLAR.value, extra={"snapshot": item.snapshot})
            except OpnsenseAPIError as exc:
                item.state, item.message = "failed", str(exc)[:1000]
                _audit(db, op, item, node.host, action="delete", status="error", message=item.message)
            item.changed_at = _now()
            await _beat(db, op)
            if item.state != "deleted":
                stop = True
                break
        if any(i.state in ("deleted", "unknown") for i in node_items):
            # pf is unchanged (a disabled rule is not loaded, P4); the apply keeps configuration and runtime equal
            # and leaves no "pending changes" in the GUI. It still zeroes the counters: sample first.
            await _sample(db, node.host, strict=False)
            try:
                await client.apply(node.host)
            except OpnsenseAPIError as exc:
                op.message = f"{ErrorCode.BEREINIGUNG_ANWENDEN_FEHLGESCHLAGEN.value}: {node.host.name}: {exc}"[:1000]
                stop = True
            await _beat(db, op)
        if stop:
            break
    await db.refresh(op)
    if op.status != "running":
        return
    for item in items:
        if item.state == "queued":
            item.state, item.message = "skipped", ErrorCode.BEREINIGUNG_NICHT_AUSGEFUEHRT.value
    clean = all(i.state == "deleted" for i in items) and not op.message
    op.status, op.finished_at, op.heartbeat_at = ("done" if clean else "failed"), _now(), _now()
    await db.commit()


async def _deleted_state(host: OpnsenseHost, rule_uuid: str) -> str:
    try:
        await client.get_rule(host, rule_uuid)
        return "failed"   # still there
    except OpnsenseNotFoundError:
        return "deleted"  # gone (getRule answers [] for a rule that is not in the model)
    except OpnsenseAPIError:
        return "unknown"


# ── Restore ───────────────────────────────────────────────────────────────

async def _find_restored(member: OpnsenseHost, item: RuleCleanupItem) -> tuple[str, str | None]:
    """After an unclear addRule: ("found", uuid), ("none", None) or ("unclear", None). A UUID already stored on the
    item is checked first with getRule (review finding 10): a content-identical twin must not be mistaken for the
    restored copy. Only then the one rule that looks exactly like the snapshot and is not the deleted original;
    two or more such rules, or an unreadable firewall, is "unclear" (decided by nobody but a human)."""
    if item.restored_uuid:
        try:
            await client.get_rule(member, item.restored_uuid)
            return "found", item.restored_uuid
        except OpnsenseNotFoundError:
            pass
        except OpnsenseAPIError:
            return "unclear", None
    try:
        search = await client.search_rules(member, all_rows=True)
    except OpnsenseAPIError:
        return "unclear", None
    want = (item.snapshot or {}).get("row") or {}
    target = _content_fp(want, False)
    found = [str(r["uuid"]) for r in (search or {}).get("rows") or []
             if isinstance(r, dict) and r.get("uuid") and str(r["uuid"]).lower() != item.rule_uuid.lower()
             and _content_fp(r, False) == target]
    if len(found) == 1:
        return "found", found[0]
    return ("none", None) if not found else ("unclear", None)


async def restore_item(db: AsyncSession, host: OpnsenseHost, item_id, *, actor: str, actor_id=None,
                       confirm_name: str) -> dict:
    """Bring a deleted rule back, DISABLED, at its old position (admin only, router). In a cluster every node's copy
    of the same delete run is restored (BACKUP first). Each node gets a new UUID (addRule)."""
    await _lock(db)
    fleet = await _fleet(db, host)
    source = await db.get(RuleCleanupItem, item_id)
    if source is None or source.host_id not in {h.id for h in fleet}:
        raise api_error(404, ErrorCode.BEREINIGUNG_NICHT_GEFUNDEN)
    if confirm_name != host.name:
        raise api_error(409, ErrorCode.HOSTNAMEN_EXAKT_EINGEBEN)
    if source.state != "deleted" or source.restored_uuid:
        raise api_error(409, ErrorCode.BEREINIGUNG_AKTION_NICHT_MOEGLICH, status=source.state)
    # Only the copies that this delete run really removed (a run that failed on the other node left the rule there).
    siblings = [s for s in (await db.scalars(select(RuleCleanupItem).where(
        RuleCleanupItem.operation_id == source.operation_id, RuleCleanupItem.rule_uuid == source.rule_uuid))).all()
                if s.state == "deleted" and not s.restored_uuid]
    if any(not s.snapshot for s in siblings):
        raise api_error(409, ErrorCode.BEREINIGUNG_SNAPSHOT_FEHLT)
    limits = await _limits(db)
    if not protection.parse_sources(limits.sources):
        raise api_error(409, ErrorCode.BEREINIGUNG_VERWALTUNGSQUELLEN_FEHLEN)
    await _ensure_free(db, host, fleet)
    members = {h.id: h for h in fleet}
    roles = await _roles([members[s.host_id] for s in siblings], bool(host.cluster_name) and len(siblings) > 1)
    now = _now()
    op = RuleCleanupOperation(
        host_id=host.id, cluster_name=host.cluster_name, kind="restore", status="running", actor=actor, actor_id=actor_id,
        created_at=now, expires_at=now, started_at=now, heartbeat_at=now,
        preview_fingerprint=hashlib.sha256(str(sorted(str(s.id) for s in siblings)).encode()).hexdigest(),
        options={"source_items": [str(s.id) for s in siblings]})
    db.add(op)
    await db.flush()
    restore_items = []
    for s in sorted(siblings, key=lambda s: (roles.get(s.host_id) == "MASTER", str(s.host_id))):
        restore_items.append(RuleCleanupItem(
            operation_id=op.id, host_id=s.host_id, rule_uuid=s.rule_uuid, seq=len(restore_items), carp_role=roles.get(s.host_id),
            state="queued", description=s.description, categories=s.categories, confidence=s.confidence,
            row_before=s.row_before, fp_before=s.fp_before, snapshot=s.snapshot, prio_group=s.prio_group,
            anchor_next_uuid=s.anchor_next_uuid, anchor_prev_uuid=s.anchor_prev_uuid, source_item_id=s.id))
    db.add_all(restore_items)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise api_error(409, ErrorCode.BEREINIGUNG_OFFENE_OPERATION, reason="rule") from exc
    await _commit(db)
    warnings: list[str] = []
    stop = False
    for item in restore_items:
        member = members[item.host_id]
        if not await _claim_write(db, op):
            stop = True
            break
        item.state, item.changed_at = "restoring", _now()
        await _commit(db)
        payload = {k: v for k, v in ((item.snapshot or {}).get("rule") or {}).items() if k not in client.RULE_READONLY_FIELDS}
        payload["enabled"] = "0"  # always back switched OFF; switching it on is an "enable" operation with a window
        new_uuid = None
        try:
            result = await client.add_rule(member, payload)
            new_uuid = result.get("uuid") if isinstance(result, dict) else None
            if not new_uuid:
                raise OpnsenseAmbiguousError("Firewall hat keine Regel-UUID zurückgegeben")
        except OpnsenseAmbiguousError as exc:
            verdict, new_uuid = await _find_restored(member, item)
            if verdict != "found":
                item.state, item.message = "unknown", str(exc)[:1000]
                _audit(db, op, item, member, action="restore", status="unknown", message=ErrorCode.BEREINIGUNG_AUSGANG_UNKLAR.value)
                await _beat(db, op)
                stop = True
        except OpnsenseAPIError as exc:  # a validation refusal (other firmware, missing alias/category)
            item.state, item.message = "failed", str(exc)[:1000]
            _audit(db, op, item, member, action="restore", status="error", message=item.message)
            await _beat(db, op)
            stop = True
        if stop:
            break
        item.restored_uuid = new_uuid
        source_item = await db.get(RuleCleanupItem, item.source_item_id)
        source_item.restored_uuid = new_uuid
        await _beat(db, op)  # the new UUID is durable before anything else happens
        position_ok = await _restore_position(member, new_uuid, item)
        if not position_ok:
            item.message = ErrorCode.BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR.value
            warnings.append(f"{member.name}:{item.rule_uuid}")
        await _sample(db, member, strict=False)
        try:
            await client.apply(member)
        except OpnsenseAPIError as exc:  # the rule is disabled: pf does not change; the GUI shows pending changes
            op.message = f"{ErrorCode.BEREINIGUNG_ANWENDEN_FEHLGESCHLAGEN.value}: {member.name}: {exc}"[:1000]
        item.state, item.changed_at = "restored", _now()
        _audit(db, op, item, member, action="restore", status="accepted",
               extra={"remote_uuid": new_uuid, "restored_from": item.rule_uuid, "position_restored": position_ok,
                      "anchor_next_uuid": item.anchor_next_uuid})
        await _beat(db, op)
    await db.refresh(op)
    if op.status != "running":  # taken over (abandoned-run sweep): it decided
        return read_operation(op, restore_items) | {"warnings": []}
    for item in restore_items:
        if item.state == "queued":
            item.state, item.message = "skipped", ErrorCode.BEREINIGUNG_NICHT_AUSGEFUEHRT.value
    op.status = "done" if all(i.state == "restored" for i in restore_items) else "failed"
    op.finished_at = _now()
    if warnings and not op.message:
        op.message = ErrorCode.BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR.value
    await db.commit()
    await db.refresh(op)
    result = read_operation(op, restore_items)
    result["warnings"] = [ErrorCode.BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR.value] if warnings else []
    return result


async def _restore_position(member: OpnsenseHost, new_uuid: str, item: RuleCleanupItem) -> bool:
    """moveRuleBefore the anchor when it still exists in the same prio_group; verified by reading sort_order back
    (a refusal of moveRuleBefore is an HTTP 500 = Ambiguous, P3). False = the rule stays at the end of its group."""
    anchor = item.anchor_next_uuid
    try:
        rows = {str(r.get("uuid")).lower(): r for r in ((await client.search_rules(member, all_rows=True)) or {}).get("rows") or []
                if isinstance(r, dict) and r.get("uuid")}
    except OpnsenseAPIError:
        return False
    new_row, anchor_row = rows.get(new_uuid.lower()), rows.get((anchor or "").lower())
    if new_row is None:
        return False
    if anchor is None:
        # It was the last rule of its group: the end of the group is its old position.
        return item.anchor_prev_uuid is None or (item.anchor_prev_uuid.lower() in rows
                                                 and _order(rows[item.anchor_prev_uuid.lower()])[0] < _order(new_row)[0])
    if anchor_row is None or _group(anchor_row) != _group(new_row):
        return False
    try:
        await client.move_rule_before(member, new_uuid, anchor)
    except OpnsenseAPIError as exc:
        logger.info("Bereinigung: moveRuleBefore auf %s unklar/abgelehnt: %s", member.name, exc)
    try:
        rows = {str(r.get("uuid")).lower(): r for r in ((await client.search_rules(member, all_rows=True)) or {}).get("rows") or []
                if isinstance(r, dict) and r.get("uuid")}
    except OpnsenseAPIError:
        return False
    new_row, anchor_row = rows.get(new_uuid.lower()), rows.get(anchor.lower())
    if new_row is None or anchor_row is None:
        return False
    group = [r for r in rows.values() if _group(r) == _group(anchor_row) and not _truthy(r.get("is_automatic"))
             and not _truthy(r.get("legacy"))]
    group.sort(key=lambda r: _order(r)[0])
    position = [str(r["uuid"]).lower() for r in group]
    i = position.index(anchor.lower())
    return i > 0 and position[i - 1] == new_uuid.lower()


# ── Housekeeping ──────────────────────────────────────────────────────────

async def prune_operations(db: AsyncSession, cutoff: datetime) -> int:
    """Finished operations older than the history retention (their snapshots go with them). No commit."""
    result = await db.execute(delete(RuleCleanupOperation).where(
        RuleCleanupOperation.created_at < cutoff,
        RuleCleanupOperation.status.notin_(OPEN_OP_STATUSES)))
    return result.rowcount or 0
