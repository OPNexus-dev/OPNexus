"""HTTP attempts plus transactionally recorded deployment state transitions.

Requests are committed before executing an operation, so an interrupted
request remains visible. Deployment events commit with the deployment state.
No credentials, cookies or arbitrary request bodies are recorded.
"""
import copy
import uuid
from contextvars import ContextVar
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.audit import AuditEvent

context = ContextVar("audit_context", default=None)


# Column widths of audit_events. entity_id/host_id/action are taken from the
# request path BEFORE the router validated it, so a junk segment (300 chars,
# or a NUL byte which Postgres text cannot store) used to turn into a 500 or a
# dropped connection instead of a plain 404/422 (found by fuzzing, 2026-10-09).
_LIMITS = {"correlation_id": 36, "actor": 150, "kind": 32, "action": 40, "status": 32,
           "entity_id": 36, "entity_name": 255, "host_id": 36, "host_name": 255}


def _clean(key, value):
    if not isinstance(value, str):
        return value
    value = value.replace("\x00", "")
    limit = _LIMITS.get(key)
    return value[:limit] if limit else value


def make_event(**values):
    ctx = context.get() or {}
    values = {k: _clean(k, v) for k, v in values.items()}
    return AuditEvent(actor=_clean("actor", ctx.get("actor", "system:rollback")),
                      correlation_id=_clean("correlation_id", ctx.get("id", str(uuid.uuid4()))), **values)


async def persist(**values):
    async with SessionLocal() as db:
        db.add(make_event(**values))
        await db.commit()


@event.listens_for(Session, "before_flush")
def deployment_history(session, flush_context, instances):
    from app.models import (FirewallRule, FirewallAlias, RuleDeployment, AliasDeployment,
                            PortForwardRule, PortForwardDeployment, OneToOneRule, OneToOneDeployment,
                            OutboundNatRule, OutboundNatDeployment, OpnsenseHost,
                            PolicyHierarchyRule, PolicyHierarchyRuleDeployment)
    mapping = {
        RuleDeployment: ("rule", FirewallRule, "rule_id", "opnsense_rule_uuid"),
        AliasDeployment: ("alias", FirewallAlias, "alias_id", "opnsense_alias_uuid"),
        PortForwardDeployment: ("port_forward", PortForwardRule, "port_forward_id", "opnsense_port_forward_uuid"),
        OneToOneDeployment: ("one_to_one", OneToOneRule, "one_to_one_id", "opnsense_one_to_one_uuid"),
        OutboundNatDeployment: ("outbound_nat", OutboundNatRule, "outbound_nat_id", "opnsense_outbound_nat_uuid"),
        PolicyHierarchyRuleDeployment: ("policy_hierarchy", PolicyHierarchyRule, "policy_hierarchy_id", "opnsense_policy_hierarchy_uuid"),
    }
    for dep in list(session.new) + list(session.dirty) + list(session.deleted):
        if type(dep) not in mapping or getattr(dep, "_skip_audit", False):
            continue
        is_new, deleted = dep in session.new, dep in session.deleted
        if not is_new and not deleted and not session.is_modified(dep, include_collections=False):
            continue
        kind, model, id_attr, uuid_attr = mapping[type(dep)]
        entity_id = getattr(dep, id_attr)
        entity = session.get(model, entity_id)
        host = session.get(OpnsenseHost, dep.host_id)
        state = inspect(dep)
        old_status = state.attrs.status.history.deleted
        old_payload = state.attrs.last_payload.history.deleted
        ctx = context.get() or {}
        action = ctx.get("action", "rollback")
        if action == "confirm" and dep.status != "success":
            action = "state_change"
        details = {
            "remote_uuid": getattr(dep, uuid_attr),
            "before_status": old_status[0] if old_status else (None if is_new else dep.status),
            "before_payload": copy.deepcopy(old_payload[0] if old_payload else (None if is_new else dep.last_payload)),
            "after_payload": None if deleted else copy.deepcopy(dep.last_payload),
            "rollback_payload": copy.deepcopy(dep.previous_payload),
            "revert_deadline": dep.revert_deadline.isoformat() if dep.revert_deadline else None,
        }
        # A failed push may never have populated last_payload. Preserve the
        # intended payload separately from the last known applied state.
        if entity is not None and action == "deploy":
            from importlib import import_module
            builder = import_module(f"app.services.{kind}_deploy_service")._build_payload
            try:
                details["requested_payload"] = builder(entity, host) if host is not None else builder(entity)
            except Exception:  # a missing variable must not break the audit write; record the unresolved intent instead
                details["requested_payload"] = builder(entity)
        session.add(make_event(kind=kind, action=action, status="removed" if deleted else dep.status,
            entity_id=str(entity_id), entity_name=(getattr(entity, "name", None) or getattr(entity, "description", None)),
            host_id=str(dep.host_id), host_name=host.name if host else None,
            message=dep.message, details=details))


def tracked_rollback(kind):
    """Persist worker attempts before contacting a firewall, including crashes.
    Flush the resulting state while its correlation context is still active.
    """
    from functools import wraps
    def decorate(function):
        @wraps(function)
        async def wrapped(db, deployment):
            from app.models import OpnsenseHost
            token = None
            if context.get() is None:
                token = context.set({"actor": "system:rollback", "id": str(uuid.uuid4()), "action": "rollback"})
            host = await db.get(OpnsenseHost, deployment.host_id)
            values = {"kind": kind, "action": (context.get() or {}).get("action", "rollback"),
                      "entity_id": str(getattr(deployment, f"{kind}_id")),
                      "host_id": str(deployment.host_id), "host_name": host.name if host else None}
            try:
                await persist(**values, status="requested", details={
                    "remote_uuid": getattr(deployment, f"opnsense_{kind}_uuid"),
                    "rollback_payload": copy.deepcopy(deployment.previous_payload),
                    "before_payload": copy.deepcopy(deployment.last_payload)})
                result = await function(db, deployment)
                await db.flush()
                return result
            except Exception:
                await persist(**values, status="interrupted", message="Rollback-Ausgang unklar; gespeicherten und tatsächlichen Stand prüfen.")
                raise
            finally:
                if token is not None:
                    context.reset(token)
        return wrapped
    return decorate
