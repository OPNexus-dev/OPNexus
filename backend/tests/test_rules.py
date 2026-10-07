import httpx
import pytest

from httpx import AsyncClient


async def _create_host(client: AsyncClient, name: str, base_url: str) -> dict:
    resp = await client.post(
        "/api/hosts",
        json={
            "name": name,
            "base_url": base_url,
            "api_key": "key",
            "api_secret": "secret",
            "verify_tls": False,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _rule_payload(target_host_ids: list[str], **overrides) -> dict:
    payload = {
        "description": "Allow SSH",
        "interface": "lan",
        "direction": "in",
        "protocol": "tcp",
        "action": "pass",
        "source_net": "any",
        "destination_net": "lan",
        "destination_port": "22",
        "target_host_ids": target_host_ids,
    }
    payload.update(overrides)
    return payload


@pytest.mark.asyncio
async def test_create_rule(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["description"] == "Allow SSH"
    assert body["target_host_ids"] == [host["id"]]
    assert body["deployments"] == []


@pytest.mark.asyncio
async def test_deploy_rule_success(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    rule_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "abc-123"})
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["deployments"]) == 1
    dep = body["deployments"][0]
    # Deploy lands in "pending" first, not "success" — see test_deploy_safety.py
    # for the confirm/auto-revert lockout-protection flow this feeds into.
    assert dep["status"] == "pending"
    assert dep["host_id"] == host["id"]
    assert dep["message"] is None


@pytest.mark.asyncio
async def test_deploy_rule_mixed_host_failure(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host_ok = await _create_host(client, "fw-ok", "https://fw-ok.test")
    host_down = await _create_host(client, "fw-down", "https://fw-down.test")

    resp = await client.post(
        "/api/rules",
        json=_rule_payload(
            [host_ok["id"], host_down["id"]],
            description="Block telnet",
            interface="wan",
            protocol="tcp",
            action="block",
            destination_net="any",
            destination_port="23",
        ),
    )
    rule_id = resp.json()["id"]

    respx_mock.post("https://fw-ok.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "ok-uuid"})
    )
    respx_mock.post("https://fw-ok.test/api/firewall/filter/apply").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    respx_mock.post("https://fw-down.test/api/firewall/filter/addRule").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    # Interface- and alias-consistency checks (only run for multi-host
    # deploys) hit both hosts' getRule/searchItem before any writes — same
    # interface label on both here, and neither host has an alias
    # matching source_net/destination_net ("any"), so both are no-ops.
    respx_mock.get(url__regex=r".*/getRule$").mock(
        return_value=httpx.Response(200, json={"rule": {"interface": {"wan": {"value": "WAN", "selected": 0}}}})
    )
    respx_mock.get(url__regex=r".*/searchItem$").mock(
        return_value=httpx.Response(200, json={"total": 0, "rowCount": 0, "current": 1, "rows": []})
    )

    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    deployments = {d["host_id"]: d for d in resp.json()["deployments"]}
    assert deployments[host_ok["id"]]["status"] == "pending"
    assert deployments[host_down["id"]]["status"] == "error"
    assert deployments[host_down["id"]]["message"]


@pytest.mark.asyncio
async def test_deploy_rejects_interface_meaning_different_networks_across_hosts(client, respx_mock):
    # Same identifier ("opt6"), genuinely different underlying networks on
    # each host — exactly the fw-primary/fw-secondary case found live.
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host_a = await _create_host(client, "fw-a", "https://fw-a.test")
    host_b = await _create_host(client, "fw-b", "https://fw-b.test")

    resp = await client.post(
        "/api/rules",
        json=_rule_payload([host_a["id"], host_b["id"]], interface="opt6"),
    )
    rule_id = resp.json()["id"]

    respx_mock.get("https://fw-a.test/api/firewall/filter/getRule").mock(
        return_value=httpx.Response(200, json={"rule": {"interface": {"opt6": {"value": "Guest", "selected": 0}}}})
    )
    respx_mock.get("https://fw-b.test/api/firewall/filter/getRule").mock(
        return_value=httpx.Response(200, json={"rule": {"interface": {"opt6": {"value": "Dummy01", "selected": 0}}}})
    )
    add = respx_mock.post(url__regex=r".*/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "x"}))

    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == {
        "code": "INTERFACE_MEANING_DIFFERS",
        "params": {"interface": "opt6", "detail": "fw-a=Guest, fw-b=Dummy01"},
    }
    # Must be rejected before any firewall was actually touched.
    assert add.call_count == 0
    assert (await client.get("/api/rules")).json()[0]["deployments"] == []


@pytest.mark.asyncio
async def test_deploy_rejects_alias_with_different_content_across_hosts(client, respx_mock):
    # Same alias name ("Web_Backends") used as source, but it resolves to
    # different IPs on each host — the alias equivalent of the opt6 case.
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host_a = await _create_host(client, "fw-c", "https://fw-c.test")
    host_b = await _create_host(client, "fw-d", "https://fw-d.test")

    resp = await client.post(
        "/api/rules",
        json=_rule_payload([host_a["id"], host_b["id"]], source_net="Web_Backends"),
    )
    rule_id = resp.json()["id"]

    respx_mock.get(url__regex=r".*/getRule$").mock(
        return_value=httpx.Response(200, json={"rule": {"interface": {"lan": {"value": "LAN", "selected": 0}}}})
    )
    respx_mock.get("https://fw-c.test/api/firewall/alias/searchItem").mock(
        return_value=httpx.Response(200, json={"rows": [{"name": "Web_Backends", "type": "host", "content": "10.0.1.10"}]})
    )
    respx_mock.get("https://fw-d.test/api/firewall/alias/searchItem").mock(
        return_value=httpx.Response(200, json={"rows": [{"name": "Web_Backends", "type": "host", "content": "10.0.2.10"}]})
    )
    add = respx_mock.post(url__regex=r".*/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "x"}))

    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "ALIAS_CONTENT_DIFFERS"
    assert resp.json()["detail"]["params"]["alias"] == "Web_Backends"
    assert add.call_count == 0
    assert (await client.get("/api/rules")).json()[0]["deployments"] == []


@pytest.mark.asyncio
async def test_delete_rule(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")

    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    rule_id = resp.json()["id"]

    resp = await client.delete(f"/api/rules/{rule_id}")
    assert resp.status_code == 204

    resp = await client.get("/api/rules")
    assert resp.status_code == 200
    assert all(r["id"] != rule_id for r in resp.json())


@pytest.mark.asyncio
async def test_deploy_rule_without_targets_rejected(client):
    resp = await client.post("/api/rules", json=_rule_payload([]))
    rule_id = resp.json()["id"]

    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 400
