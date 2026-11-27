"""Audit history grouped by operation (read only).

One operation is one ``correlation_id``: the HTTP request ("requested", then "accepted" or "rejected") and every state change it caused
("pending", "success", "removed", ...). The audit list shows one row per operation instead of one card per step
(docs/DESIGN-UI-2026-11.md section 9.4). Nothing is written: ``audit_events`` stays append-only and the export is untouched.

Result of an operation: the most serious state wins, otherwise the last business state, otherwise the last HTTP state.
  revert_failed > error > denied > rejected > interrupted > (last business state) > accepted > requested
"""
from dataclasses import dataclass
from datetime import datetime

# Priority of a step for the result. Business states (success, pending, reverted, removed, ...) all share BUSINESS: the later one wins.
CRITICAL = {"revert_failed": 100, "error": 90, "denied": 80, "rejected": 70, "interrupted": 60}
HTTP_ONLY = {"accepted": 20, "requested": 10}
BUSINESS = 50
COMMENT_LIMIT = 300


@dataclass
class Step:
    created_at: datetime
    actor: str
    kind: str
    action: str
    status: str
    entity_id: str | None
    entity_name: str | None
    host_id: str | None
    host_name: str | None
    message: str | None
    comment: str | None = None


def step_priority(status: str) -> int:
    return CRITICAL.get(status) or HTTP_ONLY.get(status) or BUSINESS


def is_http_message(message: str | None) -> bool:
    """The bare line the request middleware writes ("HTTP 202") tells nothing; the list hides it."""
    return bool(message) and message.startswith("HTTP ") and message[5:].isdigit()


def summarize(correlation_id: str, steps: list[Step]) -> dict:
    """``steps`` ascending by time (the order they happened in)."""
    first = steps[0]
    result = max(enumerate(steps), key=lambda pair: (step_priority(pair[1].status), pair[0]))[1]
    hosts: list[dict] = []
    seen: set[str] = set()
    for step in steps:
        key = step.host_id or step.host_name
        if key and key not in seen:
            seen.add(key)
            hosts.append({"id": step.host_id, "name": step.host_name})
    # One request can touch many objects (a bulk deploy of up to 50 rules shares one correlation id): count them, so the row
    # does not pretend the operation was about the first object only.
    entities = {s.entity_id or s.entity_name for s in steps if s.entity_id or s.entity_name}
    comment = next((s.comment for s in steps if s.comment), None)
    # Entity: the first step that names one (the HTTP request often only knows the id).
    named = next((s for s in steps if s.entity_name), first)
    return {
        "correlation_id": correlation_id,
        "started_at": first.created_at,
        "ended_at": steps[-1].created_at,
        "actor": first.actor,
        "kind": first.kind,
        "action": first.action,
        "entity_id": named.entity_id or first.entity_id,
        "entity_name": named.entity_name,
        "entities": len(entities),
        "hosts": hosts,
        "result": result.status,
        "message": None if is_http_message(result.message) else result.message,
        "steps": len(steps),
        "comment": comment[:COMMENT_LIMIT] if comment else None,
    }
