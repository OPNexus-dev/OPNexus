"""Rule placement: a rule with placement "top" is moved in front of the first rule of its priority group after addRule.

The deploy is checked against a small fake of searchRule/moveRuleBefore (sort_order = prio_group.sequence). The key
"placement" belongs to OPNexus and must never reach addRule/setRule.
"""
import json

import httpx
import pytest


async def _host(client, respx_mock=None, name="fw1"):
    if respx_mock is not None:
        respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post("/api/hosts", json={"name": name, "base_url": f"https://{name}.test", "api_key": "k",
                                                 "api_secret": "s", "verify_tls": False})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _rule(host_id, **kw):
    body = {"description": "Block telnet", "interface": "lan", "direction": "in", "protocol": "tcp", "action": "block",
            "destination_port": "23", "target_host_ids": [host_id], "placement": "top"}
    body.update(kw)
    return body


def _fake_firewall(respx_mock, host="fw1", *, refuse_move=False, existing_first="pass-any"):
    """searchRule/moveRuleBefore on a firewall that already holds pass-any (seq 100) and another rule (seq 200)."""
    rows = {existing_first: 100, "other": 200}
    calls = {"add": [], "move": [], "set": []}

    def row(u):
        return {"uuid": u, "prio_group": "400000", "sort_order": f"400000.{rows[u]:07d}", "sequence": str(rows[u])}

    respx_mock.get(f"https://{host}.test/api/firewall/filter/searchRule").mock(
        side_effect=lambda req: httpx.Response(200, json={"rows": sorted((row(u) for u in rows), key=lambda r: r["sort_order"])}))

    def add(req):
        calls["add"].append(json.loads(req.content)["rule"])
        rows["new-uuid"] = 300
        return httpx.Response(200, json={"result": "saved", "uuid": "new-uuid"})

    def set_(req):
        calls["set"].append(json.loads(req.content)["rule"])
        return httpx.Response(200, json={"result": "saved"})

    def move(req):
        sel, target = req.url.path.rsplit("/", 2)[-2:]
        calls["move"].append((sel, target))
        if refuse_move:
            return httpx.Response(500, json={"errorMessage": "Unexpected error"})
        rows[sel] = rows[target] - 50
        return httpx.Response(200, json={"status": "ok"})

    respx_mock.post(f"https://{host}.test/api/firewall/filter/addRule").mock(side_effect=add)
    respx_mock.post(url__regex=rf"https://{host}.test/api/firewall/filter/setRule/.*").mock(side_effect=set_)
    respx_mock.post(url__regex=rf"https://{host}.test/api/firewall/filter/moveRuleBefore/.*").mock(side_effect=move)
    respx_mock.post(f"https://{host}.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx_mock.post(url__regex=rf"https://{host}.test/api/firewall/filter/delRule/.*").mock(
        return_value=httpx.Response(200, json={"result": "deleted"}))
    return calls


@pytest.mark.asyncio
async def test_placement_defaults_to_end_and_is_stored(client):
    host = await _host(client)
    body = (await client.post("/api/rules", json=_rule(host["id"], placement="end"))).json()
    assert body["placement"] == "end"
    body = (await client.post("/api/rules", json=_rule(host["id"], placement="top"))).json()
    assert body["placement"] == "top"
    bad = await client.post("/api/rules", json=_rule(host["id"], placement="middle"))
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_top_rule_is_moved_before_the_first_rule_and_key_is_not_sent(client, respx_mock):
    host = await _host(client, respx_mock)
    calls = _fake_firewall(respx_mock)
    rule = (await client.post("/api/rules", json=_rule(host["id"]))).json()

    resp = await client.post(f"/api/rules/{rule['id']}/deploy")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deployments"][0]["status"] == "pending"
    assert calls["move"] == [("new-uuid", "pass-any")]
    assert "placement" not in calls["add"][0]


@pytest.mark.asyncio
async def test_end_rule_is_not_moved(client, respx_mock):
    host = await _host(client, respx_mock)
    calls = _fake_firewall(respx_mock)
    rule = (await client.post("/api/rules", json=_rule(host["id"], placement="end"))).json()
    assert (await client.post(f"/api/rules/{rule['id']}/deploy")).status_code == 200
    assert calls["move"] == []


@pytest.mark.asyncio
async def test_refused_move_marks_the_deployment_as_error_with_armed_revert(client, respx_mock):
    host = await _host(client, respx_mock)
    _fake_firewall(respx_mock, refuse_move=True)
    rule = (await client.post("/api/rules", json=_rule(host["id"]))).json()

    resp = await client.post(f"/api/rules/{rule['id']}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["revert_deadline"] is not None  # the sweeper undoes the misplaced rule


@pytest.mark.asyncio
async def test_changing_placement_is_a_change_in_the_preview(client, respx_mock):
    host = await _host(client, respx_mock)
    calls = _fake_firewall(respx_mock)
    rule = (await client.post("/api/rules", json=_rule(host["id"], placement="end"))).json()
    assert (await client.post(f"/api/rules/{rule['id']}/deploy")).status_code == 200
    assert (await client.post(f"/api/rules/{rule['id']}/confirm")).status_code == 200

    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    assert preview["hosts"][0]["action"] == "unchanged"

    edited = {**_rule(host["id"], placement="top"), "expected_updated_at": rule["updated_at"]}
    assert (await client.put(f"/api/rules/{rule['id']}", json=edited)).status_code == 200
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    assert preview["hosts"][0]["action"] == "update"
    assert {"field": "placement", "before": None, "after": "top"} in preview["hosts"][0]["changes"]

    assert (await client.post(f"/api/rules/{rule['id']}/deploy")).status_code == 200
    assert calls["move"] == [("new-uuid", "pass-any")] and "placement" not in calls["set"][0]
