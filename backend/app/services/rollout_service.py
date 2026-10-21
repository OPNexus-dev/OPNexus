"""Rollout plans: resolving waves to hosts, validation, preview token / activation fingerprint.

The run engine (rollout_engine.py) builds on ``resolve``: what a preview shows is exactly what a run
freezes when it starts.
"""
import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.host import OpnsenseHost
from app.models.maintenance_window import MaintenanceWindow
from app.models.policy import DeviceGroup
from app.models.rollout import RolloutPlan
from app.services import rollout_health, window_service

# Hard limits: an unattended engine that fires firmware updates must stay small and explainable.
MAX_WAVES = 10
MAX_HOSTS = 100
MAX_SOAK_MINUTES = 24 * 60


def _issue(code: str, **params) -> dict:
    return {"code": code, "params": params}


async def resolve(db: AsyncSession, plan: RolloutPlan) -> dict:
    """Waves with their hosts (a host in several waves belongs to the earliest), errors that block a
    start/activation, and warnings."""
    errors: list[dict] = []
    warnings: list[dict] = []
    seen: dict = {}
    duplicates: set[str] = set()
    waves = []
    group_ids = {g.id for w in plan.waves for g in w.groups}
    groups = {g.id: g for g in (await db.scalars(
        select(DeviceGroup).where(DeviceGroup.id.in_(group_ids)).options(selectinload(DeviceGroup.hosts)))).all()} if group_ids else {}
    for index, wave in enumerate(plan.waves):
        hosts: list[OpnsenseHost] = []
        for group in sorted(wave.groups, key=lambda g: g.name):
            for host in sorted(groups[group.id].hosts, key=lambda h: h.name):
                if host.id in seen:
                    if seen[host.id] != index:
                        duplicates.add(host.name)
                    continue
                seen[host.id] = index
                hosts.append(host)
        if not hosts:
            warnings.append(_issue("empty_wave", wave=wave.label))
        clusters = sorted({h.cluster_name for h in hosts if h.cluster_name})
        if clusters and not wave.carp_handover:
            warnings.append(_issue("master_needs_handover", wave=wave.label, clusters=", ".join(clusters)))
        waves.append({
            "index": index, "label": wave.label, "soak_minutes": wave.soak_minutes, "carp_handover": wave.carp_handover,
            "require_approval": wave.require_approval, "groups": sorted(g.name for g in wave.groups),
            "hosts": [{"id": str(h.id), "name": h.name, "cluster_name": h.cluster_name, "enabled": h.enabled} for h in hosts],
        })
    if duplicates:
        warnings.append(_issue("duplicate_hosts", names=", ".join(sorted(duplicates))))
    all_hosts = [h for w in waves for h in w["hosts"]]
    disabled = sorted(h["name"] for h in all_hosts if not h["enabled"])
    if disabled:
        warnings.append(_issue("disabled_hosts", names=", ".join(disabled)))
    if not plan.waves:
        errors.append(_issue("no_waves"))
    elif not all_hosts:
        errors.append(_issue("no_hosts"))
    if len(plan.waves) > MAX_WAVES:
        errors.append(_issue("too_many_waves", limit=MAX_WAVES))
    if len(all_hosts) > MAX_HOSTS:
        errors.append(_issue("too_many_hosts", limit=MAX_HOSTS))

    # Clusters: where are their nodes?
    cluster_names = sorted({h["cluster_name"] for h in all_hosts if h["cluster_name"]})
    for cluster in cluster_names:
        members = (await db.scalars(select(OpnsenseHost.id).where(OpnsenseHost.cluster_name == cluster))).all()
        in_plan = {h["id"]: w["index"] for w in waves for h in w["hosts"] if h["cluster_name"] == cluster}
        partial = len(in_plan) < len(members)
        split = len(set(in_plan.values())) > 1
        if plan.action == "major_update" and (partial or split):
            # Never two major releases side by side in one cluster longer than one wave.
            errors.append(_issue("cluster_split", cluster=cluster))
        elif partial:
            warnings.append(_issue("cluster_partial", cluster=cluster))
        elif split:
            warnings.append(_issue("mixed_versions", cluster=cluster, wave=waves[max(in_plan.values())]["label"]))

    window = await db.get(MaintenanceWindow, plan.window_id) if plan.window_id else None
    if plan.auto_start and plan.action != "update":
        errors.append(_issue("auto_start_update_only"))
    if plan.auto_start and window is None:
        errors.append(_issue("auto_start_needs_window"))
    if window is None:
        warnings.append(_issue("no_window"))
    elif not window.enabled:
        warnings.append(_issue("window_disabled"))
    now = datetime.now(timezone.utc)
    return {
        "waves": waves, "errors": errors, "warnings": warnings,
        "window": {"id": str(window.id), "name": window.name, "definition": window_service.definition(window),
                   "next": [o.as_dict() for o in window_service.occurrences(window, now, 3)] if window.enabled else []}
        if window else None,
    }


def definition(plan: RolloutPlan) -> dict:
    return {
        "id": str(plan.id), "name": plan.name, "action": plan.action, "window_id": str(plan.window_id) if plan.window_id else None,
        "auto_start": plan.auto_start, "gate_checks": effective_checks(plan.gate_checks), "compliance_mode": plan.compliance_mode,
        "waves": [{"position": w.position, "label": w.label, "soak_minutes": w.soak_minutes, "carp_handover": w.carp_handover,
                   "require_approval": w.require_approval, "group_ids": sorted(str(g.id) for g in w.groups)} for w in plan.waves],
    }


def effective_checks(checks: dict | None) -> dict:
    merged = {**rollout_health.DEFAULT_CHECKS, **{k: bool(v) for k, v in (checks or {}).items() if k in rollout_health.CHECKS}}
    for name in rollout_health.ALWAYS_ON:
        merged[name] = True
    return merged


def fingerprint(plan: RolloutPlan, resolved: dict) -> str:
    """Covers the plan, every resolved host (id, name, cluster, enabled) and the window definition: a change
    to any of them invalidates a preview and stops an activated automatic start."""
    state = {
        "plan": definition(plan),
        "hosts": [[w["index"], h["id"], h["name"], h["cluster_name"], h["enabled"]] for w in resolved["waves"] for h in w["hosts"]],
        "window": (resolved.get("window") or {}).get("definition"),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


async def load_plan(db: AsyncSession, plan_id) -> RolloutPlan | None:
    return await db.scalar(
        select(RolloutPlan).where(RolloutPlan.id == plan_id).execution_options(populate_existing=True))
