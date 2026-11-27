"""Audit grouped by operation (GET /api/audit?group=operation): one row per correlation_id, result rule, filters, paging."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.services.audit_group_service import Step, is_http_message, summarize
from app.services.audit_service import context


def _step(status, minutes=0, **kw):
    base = dict(created_at=datetime(2026, 11, 26, 8, 0, tzinfo=timezone.utc) + timedelta(minutes=minutes), actor="julian", kind="rule",
                action="deploy", status=status, entity_id="e1", entity_name="Rule A", host_id=None, host_name=None, message=None)
    base.update(kw)
    return Step(**base)


def _result(*statuses):
    return summarize("c", [_step(s, i) for i, s in enumerate(statuses)])["result"]


# --- Result rule (pure) ---------------------------------------------------------------------------------------------------
def test_result_is_the_last_business_state_when_nothing_went_wrong():
    assert _result("requested", "pending", "pending", "accepted") == "pending"
    assert _result("requested", "success", "accepted") == "success"
    assert _result("requested", "pending", "success", "accepted") == "success"   # later business state wins
    assert _result("requested", "removed", "accepted") == "removed"


def test_result_falls_back_to_the_http_state():
    assert _result("requested", "accepted") == "accepted"
    assert _result("requested") == "requested"


@pytest.mark.parametrize("worse,better", [("revert_failed", "error"), ("error", "denied"), ("denied", "rejected"), ("rejected", "interrupted"), ("interrupted", "success")])
def test_the_more_serious_state_wins_regardless_of_order(worse, better):
    assert _result("requested", worse, better, "accepted") == worse
    assert _result("requested", better, worse, "accepted") == worse


def test_an_error_is_not_hidden_by_a_later_success():
    assert _result("requested", "error", "success", "accepted") == "error"


def test_summary_fields():
    out = summarize("c1", [
        _step("requested", 0, host_id="h1", host_name="fw1", entity_name=None),
        _step("pending", 1, host_id="h1", host_name="fw1", comment="TICKET-7"),
        _step("pending", 2, host_id="h2", host_name="fw2"),
        _step("accepted", 3, message="HTTP 202"),
    ])
    assert out["steps"] == 4 and out["actor"] == "julian" and out["action"] == "deploy" and out["kind"] == "rule"
    assert out["entity_name"] == "Rule A"                       # the first step naming one, not the bare request
    assert [h["name"] for h in out["hosts"]] == ["fw1", "fw2"]   # distinct, in order
    assert out["comment"] == "TICKET-7"
    assert out["started_at"] < out["ended_at"]


def test_a_bulk_operation_counts_its_objects():
    out = summarize("c", [
        _step("requested", 0, entity_id=None, entity_name=None),
        _step("pending", 1, entity_id="r1", entity_name="Rule A"),
        _step("pending", 2, entity_id="r2", entity_name="Rule B"),
        _step("pending", 3, entity_id="r2", entity_name="Rule B"),
        _step("accepted", 4, entity_id=None, entity_name=None),
    ])
    assert out["entities"] == 2 and out["entity_name"] == "Rule A"
    assert summarize("c", [_step("requested"), _step("accepted", 1)])["entities"] == 1   # both steps name e1


def test_bare_http_lines_are_not_a_message():
    assert is_http_message("HTTP 202") and is_http_message("HTTP 404")
    assert not is_http_message("HTTP rejected") and not is_http_message(None) and not is_http_message("Rule deployed")
    assert summarize("c", [_step("requested"), _step("accepted", 1, message="HTTP 202")])["message"] is None
    assert summarize("c", [_step("requested"), _step("error", 1, message="timeout")])["message"] == "timeout"


def test_comment_is_cut_at_300_characters():
    assert len(summarize("c", [_step("requested", comment="x" * 500)])["comment"]) == 300


# --- API ------------------------------------------------------------------------------------------------------------------
async def _operation(steps, actor="admin", base=None, **common):
    """steps: list of (status, extra) -> events with one correlation id, one minute apart."""
    cid = str(uuid.uuid4())
    base = base or datetime.now(timezone.utc) - timedelta(minutes=30)
    async with SessionLocal() as db:
        for i, (status, extra) in enumerate(steps):
            values = dict(kind="rule", action="deploy", entity_id="e", entity_name="Rule")
            values.update(common)
            values.update(extra)
            db.add(AuditEvent(correlation_id=cid, actor=actor, status=status, created_at=base + timedelta(minutes=i), details=values.pop("details", {}), **values))
        await db.commit()
    return cid


@pytest.mark.asyncio
async def test_one_row_per_operation_with_the_result(client):
    a = await _operation([("requested", {}), ("pending", {"host_id": "h1", "host_name": "fw1"}), ("success", {"host_id": "h1", "host_name": "fw1"}), ("accepted", {"message": "HTTP 202"})])
    b = await _operation([("requested", {}), ("rejected", {})], base=datetime.now(timezone.utc) - timedelta(minutes=5))
    page = (await client.get("/api/audit", params={"group": "operation"})).json()
    assert page["total"] == 2
    items = {i["correlation_id"]: i for i in page["items"]}
    assert items[a]["result"] == "success" and items[a]["steps"] == 4 and items[a]["hosts"] == [{"id": "h1", "name": "fw1"}]
    assert items[b]["result"] == "rejected" and items[b]["steps"] == 2
    assert [i["correlation_id"] for i in page["items"]] == [b, a]          # newest operation first
    assert "details" not in page["items"][0]                              # a summary: no raw payloads


@pytest.mark.asyncio
async def test_comment_comes_from_the_details_and_is_shown_in_the_row(client):
    cid = await _operation([("requested", {}), ("pending", {"details": {"comment": "TICKET-42"}}), ("accepted", {})])
    item = (await client.get("/api/audit", params={"group": "operation"})).json()["items"][0]
    assert item["correlation_id"] == cid and item["comment"] == "TICKET-42"


@pytest.mark.asyncio
async def test_filters_select_operations_but_do_not_change_their_summary(client):
    a = await _operation([("requested", {}), ("error", {"host_id": "h1", "host_name": "fw1"}), ("accepted", {})], kind="rule")
    b = await _operation([("requested", {}), ("success", {"host_id": "h2", "host_name": "fw2"}), ("accepted", {})], kind="alias")
    only_alias = (await client.get("/api/audit", params={"group": "operation", "kind": "alias"})).json()
    assert only_alias["total"] == 1 and only_alias["items"][0]["correlation_id"] == b
    errors = (await client.get("/api/audit", params={"group": "operation", "status": "error"})).json()
    assert [i["correlation_id"] for i in errors["items"]] == [a]
    assert errors["items"][0]["steps"] == 3                                 # all steps, not only the matching one
    by_host = (await client.get("/api/audit", params={"group": "operation", "host_id": "h2"})).json()
    assert [i["correlation_id"] for i in by_host["items"]] == [b]
    search = (await client.get("/api/audit", params={"group": "operation", "q": "fw1"})).json()
    assert [i["correlation_id"] for i in search["items"]] == [a]


@pytest.mark.asyncio
async def test_paging_counts_operations_not_steps(client):
    ids = []
    for n in range(5):
        ids.append(await _operation([("requested", {}), ("success", {}), ("accepted", {})], base=datetime.now(timezone.utc) - timedelta(minutes=60 - n * 5)))
    first = (await client.get("/api/audit", params={"group": "operation", "limit": 2})).json()
    assert first["total"] == 5 and len(first["items"]) == 2
    second = (await client.get("/api/audit", params={"group": "operation", "limit": 2, "offset": 2})).json()
    third = (await client.get("/api/audit", params={"group": "operation", "limit": 2, "offset": 4})).json()
    seen = [i["correlation_id"] for i in first["items"] + second["items"] + third["items"]]
    assert seen == list(reversed(ids)) and len(set(seen)) == 5


@pytest.mark.asyncio
async def test_the_steps_of_one_operation_by_correlation_id(client):
    cid = await _operation([("requested", {}), ("pending", {}), ("accepted", {})])
    await _operation([("requested", {}), ("accepted", {})])
    steps = (await client.get("/api/audit", params={"correlation_id": cid})).json()
    assert steps["total"] == 3 and {e["correlation_id"] for e in steps["items"]} == {cid}


@pytest.mark.asyncio
async def test_empty_result_and_validation(client, anon_client):
    page = (await client.get("/api/audit", params={"group": "operation", "kind": "nothing"})).json()
    assert page == {"total": 0, "offset": 0, "limit": 50, "items": []}
    assert (await client.get("/api/audit", params={"group": "nonsense"})).status_code == 422
    assert (await client.get("/api/audit", params={"group": "operation", "limit": 201})).status_code == 422
    assert (await anon_client.get("/api/audit", params={"group": "operation"})).status_code == 401


@pytest.mark.asyncio
async def test_the_ungrouped_list_is_unchanged(client):
    await _operation([("requested", {}), ("accepted", {})])
    page = (await client.get("/api/audit")).json()
    assert page["total"] == 2 and "details" in page["items"][0]
