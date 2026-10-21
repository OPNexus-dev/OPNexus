"""Rule analysis API + MCP tool (matrix case 25): GET only, read tokens allowed, nothing written."""
import json

import httpx
import pytest

from app.services.rule_analysis import context as ctx_mod
from tests.test_api_tokens import _bearer, _make
from tests.test_rule_analysis import FIXTURE
from tests.test_rules import _create_host

FW, DOWN = "https://198.51.100.200", "https://198.51.100.201"


@pytest.fixture(autouse=True)
def _fresh_cache():
    ctx_mod.clear_cache()
    yield
    ctx_mod.clear_cache()


def _mock_fw(respx_mock, base=FW):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(f"{base}/api/firewall/filter/searchRule").mock(return_value=httpx.Response(200, json=FIXTURE["rules"]))
    respx_mock.get(f"{base}/api/diagnostics/firewall/pfStatistics/rules").mock(return_value=httpx.Response(200, json=FIXTURE["pf"]))
    respx_mock.get(f"{base}/api/interfaces/overview/interfacesInfo").mock(return_value=httpx.Response(200, json=FIXTURE["interfaces"]))
    respx_mock.get(f"{base}/api/firewall/group/searchItem").mock(return_value=httpx.Response(200, json=FIXTURE["groups"]))
    respx_mock.get(f"{base}/api/firewall/alias/searchItem").mock(return_value=httpx.Response(200, json=FIXTURE["aliases"]))
    respx_mock.get(f"{base}/api/firewall/d_nat/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))
    return respx_mock.post(f"{base}/api/firewall/alias_util/findReferences").mock(
        return_value=httpx.Response(200, json={"status": "ok", "matches": []}))


Q = {"src": "10.20.30.50", "dst": "198.51.100.10", "proto": "tcp", "dport": "443", "iface": "lan"}


@pytest.mark.asyncio
async def test_host_match_and_validation(client, respx_mock):
    refs = _mock_fw(respx_mock)
    host = await _create_host(client, "fw-a", FW)
    resp = await client.get(f"/api/hosts/{host['id']}/rule-analysis/match", params=Q)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["action"] == "pass" and body["confidence"] == "sure" and body["error"] is None
    assert body["decisive_rule"]["description"] == "Default allow LAN to any rule"
    assert body["legs"][0]["interface"] == "lan" and body["trace"][0]["steps"]
    assert refs.call_count == 2 and json.loads(refs.calls[0].request.content) == {"ip": "10.20.30.50"}
    bad = await client.get(f"/api/hosts/{host['id']}/rule-analysis/match", params={**Q, "src": "pc.example"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "ANALYSE_ADRESSE_UNGUELTIG"
    mixed = await client.get(f"/api/hosts/{host['id']}/rule-analysis/match", params={**Q, "dst": "2001:db8::1"})
    assert mixed.json()["detail"]["code"] == "ANALYSE_FAMILIEN_MISCHUNG"
    iface = await client.get(f"/api/hosts/{host['id']}/rule-analysis/match", params={**Q, "iface": "opt7"})
    assert iface.status_code == 422 and iface.json()["detail"]["code"] == "ANALYSE_INTERFACE_UNBEKANNT"


@pytest.mark.asyncio
async def test_fleet_match_reports_unreachable_hosts_per_host(client, respx_mock):
    _mock_fw(respx_mock)
    respx_mock.route(url__startswith=DOWN).mock(side_effect=httpx.ConnectError("down"))
    await _create_host(client, "fw-a", FW)
    await _create_host(client, "fw-down", DOWN)
    resp = await client.get("/api/rule-analysis/match", params={**Q, "iface": "auto"})
    assert resp.status_code == 200, resp.text
    by_name = {r["host_name"]: r for r in resp.json()}
    assert by_name["fw-a"]["action"] == "pass" and "IFACE_GUESSED" in by_name["fw-a"]["notes"]
    assert by_name["fw-a"]["confidence"] == "probable"
    assert by_name["fw-down"]["error"]["code"] == "ANALYSE_FIREWALL_NICHT_LESBAR"


@pytest.mark.asyncio
async def test_25_mcp_rule_match_with_a_read_token_changes_nothing(client, respx_mock):
    _mock_fw(respx_mock)
    host = await _create_host(client, "fw-a", FW)
    token = await _make(client, "read")
    async with _bearer(token["token"]) as api:
        direct = await api.get(f"/api/hosts/{host['id']}/rule-analysis/match", params=Q)
        assert direct.status_code == 200
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "rule_match", "arguments": {**Q, "dport": 443, "host_ids": host["id"]}}}
        result = (await api.post("/api/mcp", json=rpc)).json()["result"]
        tools = (await api.post("/api/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).json()["result"]["tools"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload[0]["action"] == "pass" and "trace" not in payload[0]
    assert "rule_match" in {t["name"] for t in tools}
    # The only POST that reached the firewall is the read-only pf table lookup.
    posts = {c.request.url.path for c in respx_mock.calls if c.request.method != "GET"}
    assert posts == {"/api/firewall/alias_util/findReferences"}
