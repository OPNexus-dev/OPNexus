"""1.39: the usage column of the rules table (GET /api/rules/usage) and the overlap hint next to the preview
(GET /api/rules/{id}/overlap). Both are read-only; neither changes the preview token."""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.database import SessionLocal
from app.models.rule_deployment import RuleDeployment
from app.models.rule_hit import RuleHit
from app.services.rule_analysis import context as ctx_mod
from tests.test_rule_analysis_api import FW, _mock_fw
from tests.test_rules import _create_host

NOW = datetime.now(timezone.utc)
DEFAULT_ALLOW = "fccf1b9d-2461-43d6-938f-2c3c39b3ddee"        # "Default allow LAN to any rule" in the fixture, 400000.0000299


@pytest.fixture(autouse=True)
def _fresh_cache():
    ctx_mod.clear_cache()
    yield
    ctx_mod.clear_cache()


async def _rule(client, targets, description="r", **over):
    payload = {"description": description, "interface": "lan", "direction": "in", "protocol": "tcp", "action": "block",
               "source_net": "lan", "destination_net": "any", "destination_port": "443", "target_host_ids": targets}
    payload.update(over)
    resp = await client.post("/api/rules", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _deploy_row(rule_id, host_id, uuid_):
    async with SessionLocal() as db:
        db.add(RuleDeployment(rule_id=uuid.UUID(rule_id), host_id=uuid.UUID(host_id), status="success",
                              opnsense_rule_uuid=uuid_, last_payload={}, deployed_at=NOW))
        await db.commit()


async def _hit(host_id, uuid_, *, first_days, hit_days=None, enabled=True):
    async with SessionLocal() as db:
        db.add(RuleHit(host_id=uuid.UUID(host_id), rule_uuid=uuid_, description="x", enabled=enabled,
                       first_seen_at=NOW - timedelta(days=first_days), last_sampled_at=NOW - timedelta(minutes=5),
                       last_hit_at=None if hit_days is None else NOW - timedelta(days=hit_days)))
        await db.commit()


def _by_id(body):
    return {item["rule_id"]: item for item in body}


@pytest.mark.asyncio
async def test_usage_states_per_central_rule(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    names = ["hit", "idle", "young", "nodata", "undeployed"]
    rules = {n: await _rule(client, [host["id"]], n) for n in names}
    for n in ("hit", "idle", "young", "nodata"):
        await _deploy_row(rules[n]["id"], host["id"], f"uuid-{n}")
    await _hit(host["id"], "uuid-hit", first_days=90, hit_days=2)
    await _hit(host["id"], "uuid-idle", first_days=90)              # watched 90 days, never hit
    await _hit(host["id"], "uuid-young", first_days=3)              # still collecting data
    usage = _by_id((await client.get("/api/rules/usage")).json())
    assert usage[rules["hit"]["id"]]["state"] == "used" and usage[rules["hit"]["id"]]["last_hit_at"]
    assert usage[rules["idle"]["id"]]["state"] == "unused" and usage[rules["idle"]["id"]]["last_hit_at"] is None
    assert usage[rules["young"]["id"]]["state"] == "collecting"
    assert usage[rules["nodata"]["id"]]["state"] == "unknown" and usage[rules["nodata"]["id"]]["hosts_tracked"] == 0
    assert usage[rules["undeployed"]["id"]]["state"] == "unknown" and usage[rules["undeployed"]["id"]]["hosts_total"] == 0


@pytest.mark.asyncio
async def test_usage_of_a_cluster_rule_needs_every_node(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    a = await _create_host(client, "fw-a", "https://fw-a.test")
    b = await _create_host(client, "fw-b", "https://fw-b.test")
    both = await _rule(client, [a["id"], b["id"]], "both")
    one_side = await _rule(client, [a["id"], b["id"]], "one-side")
    for rule in (both, one_side):
        await _deploy_row(rule["id"], a["id"], f"a-{rule['description']}")
        await _deploy_row(rule["id"], b["id"], f"b-{rule['description']}")
    # the master saw nothing for 90 days, the backup node saw a hit 1 day ago: the rule is in use
    await _hit(a["id"], "a-both", first_days=90)
    await _hit(b["id"], "b-both", first_days=90, hit_days=1)
    # only one node has data at all: "unused" is not provable
    await _hit(a["id"], "a-one-side", first_days=90)
    usage = _by_id((await client.get("/api/rules/usage")).json())
    assert usage[both["id"]]["state"] == "used" and usage[both["id"]]["hosts_tracked"] == 2
    assert usage[one_side["id"]]["state"] == "unknown" and usage[one_side["id"]]["hosts_tracked"] == 1


@pytest.mark.asyncio
async def test_a_rule_shadowed_by_an_earlier_rule_is_reported_before_the_push(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "block https")
    before = (await client.get(f"/api/rules/{rule['id']}/preview")).json()["token"]
    resp = await client.get(f"/api/rules/{rule['id']}/overlap")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["hosts"][0]
    assert entry["status"] == "ok" and entry["host_name"] == "fw-a"
    assert entry["overlap"]["kind"] == "shadowed" and entry["overlap"]["by"]["uuid"] == DEFAULT_ALLOW
    assert entry["overlap"]["by"]["description"] == "Default allow LAN to any rule"
    assert entry["overlap"]["confidence"] == "probable"            # a draft is never "sure"
    assert resp.json()["has_findings"] is True
    assert (await client.get(f"/api/rules/{rule['id']}/preview")).json()["token"] == before
    assert all(c.request.method == "GET" or c.request.url.path.endswith("findReferences") for c in respx_mock.calls)


@pytest.mark.asyncio
async def test_a_rule_placed_on_top_reports_the_rules_it_makes_pointless(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "block everything", protocol="any", source_net="any", destination_port="", placement="top")
    entry = (await client.get(f"/api/rules/{rule['id']}/overlap")).json()["hosts"][0]
    assert entry["overlap"] is None
    assert entry["makes_pointless_total"] >= 1
    assert "Default allow LAN to any rule" in {r["description"] for r in entry["makes_pointless"]}


@pytest.mark.asyncio
async def test_an_already_deployed_rule_is_judged_at_its_live_position(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "same as default", protocol="any", destination_port="", action="pass")
    await _deploy_row(rule["id"], host["id"], DEFAULT_ALLOW)        # it replaces that rule in place, so it is not its own shadow
    entry = (await client.get(f"/api/rules/{rule['id']}/overlap")).json()["hosts"][0]
    assert entry["status"] == "ok" and entry["overlap"] is None


@pytest.mark.asyncio
async def test_an_unreachable_host_is_a_hint_not_an_error(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.route(url__startswith="https://down.test").mock(side_effect=httpx.ConnectError("down"))
    host = await _create_host(client, "fw-down", "https://down.test")
    rule = await _rule(client, [host["id"]], "x")
    resp = await client.get(f"/api/rules/{rule['id']}/overlap")
    assert resp.status_code == 200
    assert resp.json()["hosts"][0]["status"] == "unavailable" and resp.json()["has_findings"] is False
