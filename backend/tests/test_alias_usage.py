"""R5: "used by" for central aliases (GET /api/aliases/{id}/usage) and the delete protection built on it."""
import httpx
import pytest

from tests.test_aliases import _alias_payload
from tests.test_rules import _create_host


async def _alias(client, host, name, content="10.0.1.10", type_="host"):
    resp = await client.post("/api/aliases", json=_alias_payload([host["id"]], name=name, content=content, type=type_))
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _rule(client, host, description, **over):
    payload = {"description": description, "interface": "lan", "direction": "in", "protocol": "tcp", "action": "pass",
               "source_net": "any", "destination_net": "any", "destination_port": "443", "target_host_ids": [host["id"]]}
    payload.update(over)
    resp = await client.post("/api/rules", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_usage_lists_rules_nat_and_nested_aliases(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    web = await _alias(client, host, "Web_Backends")
    ports = await _alias(client, host, "Web_Ports", content="80\n443", type_="port")
    await _alias(client, host, "All_Servers", content="Web_Backends\n10.0.9.9", type_="network")
    await _rule(client, host, "allow web", destination_net="Web_Backends", destination_port="Web_Ports")
    await _rule(client, host, "unrelated", destination_net="10.0.1.10")
    body = (await client.get(f"/api/aliases/{web['id']}/usage")).json()
    assert body["total"] == 2 and body["truncated"] is False
    kinds = {(i["kind"], i["name"]): i["fields"] for i in body["items"]}
    assert kinds[("rule", "allow web")] == ["destination_net"]
    assert kinds[("alias", "All_Servers")] == ["content"]
    assert (await client.get(f"/api/aliases/{ports['id']}/usage")).json()["items"][0]["fields"] == ["destination_port"]


@pytest.mark.asyncio
async def test_an_inverted_reference_counts(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    alias = await _alias(client, host, "Blocked_Nets")
    await _rule(client, host, "not from blocked", source_net="!Blocked_Nets")
    assert (await client.get(f"/api/aliases/{alias['id']}/usage")).json()["total"] == 1


@pytest.mark.asyncio
async def test_an_alias_in_use_cannot_be_deleted_until_the_reference_is_gone(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    alias = await _alias(client, host, "Web_Backends")
    rule = await _rule(client, host, "allow web", destination_net="Web_Backends")
    refused = await client.delete(f"/api/aliases/{alias['id']}")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ALIAS_STILL_USED"
    assert refused.json()["detail"]["params"] == {"count": 1, "names": "allow web"}
    assert (await client.delete(f"/api/rules/{rule['id']}")).status_code == 204
    assert (await client.delete(f"/api/aliases/{alias['id']}")).status_code == 204


@pytest.mark.asyncio
async def test_an_unused_alias_reports_nothing(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    alias = await _alias(client, host, "Lonely")
    assert (await client.get(f"/api/aliases/{alias['id']}/usage")).json() == {"total": 0, "items": [], "truncated": False}
