"""Taking over rules that were created by hand on a firewall."""
import json

import httpx
import pytest

BASE = "https://fw-adopt.test"


def _row(uuid, **over):
    row = {
        "uuid": uuid, "description": "Manual SSH", "enabled": "1", "action": "pass", "quick": "1",
        "interface": "lan", "%interface": "LAN", "interfacenot": "0", "direction": "in", "ipprotocol": "inet",
        "protocol": "TCP", "source_net": "10.0.0.0/8", "source_not": "0", "source_port": "",
        "destination_net": "any", "destination_not": "0", "destination_port": "22", "log": "1",
        "received-on": "", "gateway": "WAN_GW", "is_automatic": False,
    }
    row.update(over)
    return row


async def _host(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    r = await client.post("/api/hosts", json={"name": "fw-adopt", "base_url": BASE, "api_key": "k", "api_secret": "s"})
    return r.json()["id"]


def _live(respx_mock, rows):
    return respx_mock.get(f"{BASE}/api/firewall/filter/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": rows, "total": len(rows)})
    )


@pytest.mark.asyncio
async def test_unmanaged_lists_adoptable_and_refused_with_reasons(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _live(respx_mock, [
        _row("u-ok"),
        _row("u-neg", description="Not source", source_not="1"),
        _row("u-multi", description="Two ifaces", interface="lan,opt1"),
        _row("u-float", description="Late", quick="0"),
        _row("u-auto", description="anti-lockout rule", is_automatic=True),
    ])
    resp = await client.get(f"/api/rules/unmanaged?host_id={host_id}")
    assert resp.status_code == 200, resp.text
    by_uuid = {r["uuid"]: r for r in resp.json()}
    assert "u-auto" not in by_uuid  # system rules are never offered
    assert by_uuid["u-ok"]["adoptable"] is True and by_uuid["u-ok"]["log"] is True
    assert by_uuid["u-neg"]["reasons"] == ["negated_source"]
    assert by_uuid["u-multi"]["reasons"] == ["interface_not_single"]
    assert by_uuid["u-float"]["reasons"] == ["not_quick"]


@pytest.mark.asyncio
async def test_adopt_records_rule_without_writing_to_the_firewall(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _live(respx_mock, [_row("u-ok")])
    resp = await client.post("/api/rules/adopt", json={"host_id": host_id, "rule_uuid": "u-ok"})
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    assert (rule["description"], rule["interface"], rule["source_net"], rule["destination_port"]) == (
        "Manual SSH", "lan", "10.0.0.0/8", "22")
    assert rule["log"] is True and rule["enabled"] is True
    assert rule["target_host_ids"] == [host_id]
    assert [d["status"] for d in rule["deployments"]] == ["success"]
    assert rule["workflow_state"] == "synced"
    assert all(call.request.method == "GET" for call in respx_mock.calls if "/filter/" in str(call.request.url))
    # no longer offered afterwards
    again = await client.get(f"/api/rules/unmanaged?host_id={host_id}")
    assert again.json() == []


@pytest.mark.asyncio
async def test_adopt_refuses_unrepresentable_rule(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _live(respx_mock, [_row("u-neg", source_not="1")])
    resp = await client.post("/api/rules/adopt", json={"host_id": host_id, "rule_uuid": "u-neg"})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "RULE_NOT_ADOPTABLE"
    assert "negated_source" in resp.json()["detail"]["params"]["reasons"]
    assert (await client.get("/api/rules")).json() == [] or all(r["description"] != "Manual SSH" for r in (await client.get("/api/rules")).json())


@pytest.mark.asyncio
async def test_adopt_unknown_uuid_is_404_and_double_adopt_is_refused(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _live(respx_mock, [_row("u-ok")])
    assert (await client.post("/api/rules/adopt", json={"host_id": host_id, "rule_uuid": "nope"})).status_code == 404
    assert (await client.post("/api/rules/adopt", json={"host_id": host_id, "rule_uuid": "u-ok"})).status_code == 201
    second = await client.post("/api/rules/adopt", json={"host_id": host_id, "rule_uuid": "u-ok"})
    assert second.status_code == 409
    assert "already_managed" in second.json()["detail"]["params"]["reasons"]


@pytest.mark.asyncio
async def test_edit_after_adopt_updates_same_remote_rule_and_keeps_log(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _live(respx_mock, [_row("u-ok")])
    rule = (await client.post("/api/rules/adopt", json={"host_id": host_id, "rule_uuid": "u-ok"})).json()

    edit = {
        "description": "Manual SSH (edited)", "interface": "lan", "direction": "in", "ipprotocol": "inet",
        "protocol": "TCP", "action": "pass", "source_net": "10.0.0.0/8", "source_port": None,
        "destination_net": "any", "destination_port": "22", "enabled": True, "log": True,
        "target_host_ids": [host_id], "target_group_ids": [], "expected_updated_at": rule["updated_at"],
    }
    assert (await client.put(f"/api/rules/{rule['id']}", json=edit)).status_code == 200

    set_route = respx_mock.post(f"{BASE}/api/firewall/filter/setRule/u-ok").mock(
        return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(f"{BASE}/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    from tests.helpers import deploy_approved
    resp = await deploy_approved(client, f"/api/rules/{rule['id']}/deploy")
    assert resp.status_code == 200, resp.text
    assert set_route.called
    body = set_route.calls.last.request.content.decode()
    sent = json.loads(body)["rule"]
    assert sent["description"] == "Manual SSH (edited)"
    assert sent["log"] == "1"  # not silently reset by the push
    assert "gateway" not in sent  # fields OPNexus does not model are never sent, so they stay as they are
