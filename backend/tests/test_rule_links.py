"""1.39.1: deployment links that an OPNsense config sync made stale (the secondary holds the primary's UUIDs)."""
import uuid

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.host import OpnsenseHost
from app.models.rule_deployment import RuleDeployment
from app.services import rule_link_service
from tests.test_rules import _create_host

A, B = "https://fw-a.test", "https://fw-b.test"
PAYLOAD = {"description": "Allow SSH", "action": "pass", "interface": "lan", "direction": "in", "protocol": "tcp",
           "source_net": "any", "source_port": "", "destination_net": "lan", "destination_port": "22"}


def _row(uuid_, **over):
    row = {"uuid": uuid_, "description": "Allow SSH", "action": "pass", "interface": "lan", "direction": "in", "protocol": "TCP",
           "source_net": "any", "source_port": "", "destination_net": "lan", "destination_port": "22", "is_automatic": None,
           "enabled": "1", "sort_order": "400000.0000001"}
    row.update(over)
    return row


def _live(respx_mock, base, rows):
    respx_mock.get(f"{base}/api/firewall/filter/searchRule").mock(return_value=httpx.Response(200, json={"rows": rows}))


async def _setup(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    a, b = await _create_host(client, "fw-a", A), await _create_host(client, "fw-b", B)
    resp = await client.post("/api/rules", json={**{k: v for k, v in PAYLOAD.items() if k != "source_port"}, "target_host_ids": [a["id"], b["id"]]})
    rule = resp.json()
    async with SessionLocal() as db:
        for host, uuid_ in ((a, "uuid-a"), (b, "uuid-b-recorded")):
            db.add(RuleDeployment(rule_id=uuid.UUID(rule["id"]), host_id=uuid.UUID(host["id"]), status="success",
                                  opnsense_rule_uuid=uuid_, last_payload=dict(PAYLOAD)))
        await db.commit()
    return a, b, rule


@pytest.mark.asyncio
async def test_config_sync_case_is_found_strong_and_repaired_on_request(client, respx_mock):
    a, b, rule = await _setup(client, respx_mock)
    _live(respx_mock, A, [_row("uuid-a")])
    _live(respx_mock, B, [_row("uuid-a")])          # the secondary holds the PRIMARY's uuid (what the sync produces)
    found = (await client.get(f"/api/rule-order/hosts/{b['id']}/links")).json()
    assert [(i["stored_uuid"], i["live_uuid"], i["strength"]) for i in found["items"]] == [("uuid-b-recorded", "uuid-a", "partner_uuid")]
    assert found["items"][0]["rule_description"] == "Allow SSH" and found["ambiguous"] == 0
    assert (await client.get(f"/api/rule-order/hosts/{a['id']}/links")).json()["items"] == []   # the primary is fine

    assert (await client.post(f"/api/rule-order/hosts/{b['id']}/links/repair", json={"token": "0" * 64})).status_code == 409
    done = await client.post(f"/api/rule-order/hosts/{b['id']}/links/repair", json={"token": found["token"]})
    assert done.status_code == 200 and done.json() == {"repaired": 1}
    async with SessionLocal() as db:
        dep = (await db.scalars(select(RuleDeployment).where(RuleDeployment.host_id == uuid.UUID(b["id"])))).one()
        assert dep.opnsense_rule_uuid == "uuid-a"
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.action == "relink"))).all()
        assert len(events) == 1 and events[0].host_name == "fw-b" and events[0].details["strength"] == "partner_uuid"
    assert (await client.get(f"/api/rule-order/hosts/{b['id']}/links")).json()["items"] == []
    # the live view now calls the rule "central" on both nodes
    order = (await client.get(f"/api/rule-order/hosts/{b['id']}")).json()["rows"]
    assert [r["origin"] for r in order] == ["central"]


@pytest.mark.asyncio
async def test_only_strong_links_are_repaired_automatically(client, respx_mock):
    a, b, rule = await _setup(client, respx_mock)
    _live(respx_mock, B, [_row("uuid-other")])      # same content, but not the partner's uuid: only a content match
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, uuid.UUID(b["id"]))
        search = {"rows": [_row("uuid-other")]}
        found = await rule_link_service.proposals(db, host, search)
        assert [i["strength"] for i in found["items"]] == ["content"]
        assert await rule_link_service.reconcile(db, host, search) == 0           # not on its own
    done = await client.post(f"/api/rule-order/hosts/{b['id']}/links/repair",
                             json={"token": (await client.get(f"/api/rule-order/hosts/{b['id']}/links")).json()["token"]})
    assert done.json() == {"repaired": 1}                                           # but on request
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, uuid.UUID(a["id"]))
        assert await rule_link_service.reconcile(db, host, {"rows": [_row("uuid-a")]}) == 0   # nothing stale: no change


@pytest.mark.asyncio
async def test_the_observe_step_repairs_strong_links_by_itself(client, respx_mock):
    a, b, rule = await _setup(client, respx_mock)
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, uuid.UUID(b["id"]))
        assert await rule_link_service.reconcile(db, host, {"rows": [_row("uuid-a")]}) == 1
        dep = (await db.scalars(select(RuleDeployment).where(RuleDeployment.host_id == host.id))).one()
        assert dep.opnsense_rule_uuid == "uuid-a"
        event = (await db.scalars(select(AuditEvent).where(AuditEvent.action == "relink"))).one()
        assert event.actor == "system:relink"


@pytest.mark.asyncio
async def test_ambiguous_and_unsettled_links_are_left_alone(client, respx_mock):
    a, b, rule = await _setup(client, respx_mock)
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, uuid.UUID(b["id"]))
        two = {"rows": [_row("uuid-x"), _row("uuid-y")]}                           # two identical candidates
        found = await rule_link_service.proposals(db, host, two)
        assert found["items"] == [] and found["ambiguous"] == 1
        dep = (await db.scalars(select(RuleDeployment).where(RuleDeployment.host_id == host.id))).one()
        dep.status = "pending"                                                      # an open deployment is never touched
        await db.commit()
        assert (await rule_link_service.proposals(db, host, {"rows": [_row("uuid-a")]}))["items"] == []
        # a rule that does not match the recorded content is no candidate either
        dep.status = "success"
        await db.commit()
        assert (await rule_link_service.proposals(db, host, {"rows": [_row("uuid-a", destination_port="23")]}))["items"] == []


@pytest.mark.asyncio
async def test_an_unreachable_host_is_a_clear_error(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.route(url__startswith=A).mock(side_effect=httpx.ConnectError("down"))
    a = await _create_host(client, "fw-a", A)
    resp = await client.get(f"/api/rule-order/hosts/{a['id']}/links")
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "ORDER_HOST_UNREACHABLE"
