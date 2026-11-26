"""R11: a rule that would block the way OPNexus (or a management source) reaches the firewall is reported before the
push (GET /api/rules/{id}/management-check) and stops the deploy until it is acknowledged. The automatic revert runs
in OPNexus and needs that way, so such a rule cannot be taken back automatically."""
import httpx
import pytest

from app.services import opnsense_client
from app.services.rule_analysis import context as ctx_mod
from tests.test_rule_analysis_api import FW, _mock_fw
from tests.test_rule_usage_overlap import _deploy_row, _rule
from tests.test_rules import _create_host

GOV = "/api/settings/governance"
ADMIN_PC = "10.20.30.50"          # inside the fixture's LAN


@pytest.fixture(autouse=True)
def _fresh_cache():
    ctx_mod.clear_cache()
    yield
    ctx_mod.clear_cache()


def _own(monkeypatch, address):
    async def seen_from(host):
        return address
    monkeypatch.setattr(opnsense_client, "own_source_address", seen_from)


async def _sources(client, values):
    assert (await client.put(GOV, json={"cleanup_management_sources": values})).status_code == 200


async def _check(client, rule_id):
    resp = await client.get(f"/api/rules/{rule_id}/management-check")
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_a_block_rule_on_top_that_hits_opnexus_is_reported(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, ADMIN_PC)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "block all", protocol="any", source_net="any", destination_port="", placement="top")
    body = await _check(client, rule["id"])
    entry = body["hosts"][0]
    assert body["risk"] == "blocks" and entry["risk"] == "blocks" and entry["opnexus_address_known"] is True
    assert any(f["who"] == "opnexus" and f["verdict"] in ("block", "reject") for f in entry["findings"])
    # nothing was written to the firewall
    assert all(c.request.method == "GET" or c.request.url.path.endswith("findReferences") for c in respx_mock.calls)


@pytest.mark.asyncio
async def test_a_block_rule_behind_the_allow_rule_does_not_warn(client, respx_mock, monkeypatch):
    """A default-deny at the end of the list: the earlier pass rule still decides the management packet."""
    _mock_fw(respx_mock)
    _own(monkeypatch, ADMIN_PC)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "deny rest", protocol="any", source_net="any", destination_port="")
    body = await _check(client, rule["id"])
    assert body["risk"] == "none" and body["hosts"][0]["findings"] == []


@pytest.mark.asyncio
async def test_a_block_rule_for_an_unrelated_port_does_not_warn(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, ADMIN_PC)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "block 8123", destination_port="8123", placement="top")
    assert (await _check(client, rule["id"]))["risk"] == "none"


@pytest.mark.asyncio
async def test_a_configured_management_source_is_protected_too(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, None)                                     # OPNexus's own address cannot be told
    await _sources(client, [ADMIN_PC])
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "block admin pc", protocol="tcp", source_net=ADMIN_PC, destination_port="443", placement="top")
    entry = (await _check(client, rule["id"]))["hosts"][0]
    assert entry["risk"] == "blocks" and entry["opnexus_address_known"] is False
    assert any(f["who"] == "admin" for f in entry["findings"])


@pytest.mark.asyncio
async def test_without_any_known_source_the_answer_is_unknown_not_none(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, None)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "block all", protocol="any", source_net="any", destination_port="", placement="top")
    body = await _check(client, rule["id"])
    assert body["risk"] == "unknown" and body["hosts"][0]["findings"][0]["reason"] == "no_source"


@pytest.mark.asyncio
async def test_a_new_pass_rule_never_warns(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, ADMIN_PC)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "pass all", action="pass", protocol="any", source_net="any", destination_port="")
    assert (await _check(client, rule["id"]))["risk"] == "none"


@pytest.mark.asyncio
async def test_an_unreachable_host_is_a_hint_not_an_error(client, respx_mock):
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.route(url__startswith="https://down.test").mock(side_effect=httpx.ConnectError("down"))
    host = await _create_host(client, "fw-down", "https://down.test")
    rule = await _rule(client, [host["id"]], "x")
    body = await _check(client, rule["id"])
    assert body["risk"] == "none" and body["hosts"][0]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_the_deploy_is_refused_until_the_risk_is_acknowledged(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, ADMIN_PC)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "block all", protocol="any", source_net="any", destination_port="", placement="top")
    token = (await client.get(f"/api/rules/{rule['id']}/preview")).json()["token"]
    refused = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": token})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "MANAGEMENT_PATH_AT_RISK"
    assert refused.json()["detail"]["params"]["hosts"] == "fw-a"
    assert not any(c.request.method == "POST" and "addRule" in c.request.url.path for c in respx_mock.calls)


@pytest.mark.asyncio
async def test_a_bulk_item_without_acknowledgement_is_refused_and_the_others_go_on(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, ADMIN_PC)
    host = await _create_host(client, "fw-a", FW)
    risky = await _rule(client, [host["id"]], "block all", protocol="any", source_net="any", destination_port="", placement="top")
    token = (await client.get(f"/api/rules/{risky['id']}/preview")).json()["token"]
    resp = await client.post("/api/rules/bulk/deploy", json={"items": [{"rule_id": risky["id"], "preview_token": token}]})
    assert resp.status_code == 200, resp.text
    result = resp.json()["results"][0]
    assert result["status"] == "failed" and result["error"]["code"] == "MANAGEMENT_PATH_AT_RISK"


# ── R4: comment on deploy ────────────────────────────────────────────────────────────────────────────────────

async def _require_comment(client, on=True):
    assert (await client.put(GOV, json={"require_deploy_comment": on})).status_code == 200


@pytest.mark.asyncio
async def test_the_comment_requirement_is_off_by_default_and_a_deploy_goes_through(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    assert (await client.get(GOV)).json()["require_deploy_comment"] is False


@pytest.mark.asyncio
async def test_a_deploy_without_comment_is_refused_when_required_and_the_comment_lands_in_the_audit(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, ADMIN_PC)
    await _require_comment(client)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "allow 8123", action="pass", destination_port="8123")
    token = (await client.get(f"/api/rules/{rule['id']}/preview")).json()["token"]
    refused = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": token})
    assert refused.status_code == 422 and refused.json()["detail"]["code"] == "DEPLOY_COMMENT_REQUIRED"
    blank = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": token, "comment": "   "})
    assert blank.status_code == 422
    respx_mock.post(f"{FW}/api/firewall/filter/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "new-uuid"}))
    respx_mock.post(url__regex=r".*/api/firewall/filter/apply.*").mock(return_value=httpx.Response(200, json={"status": "OK"}))
    ok = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": token, "comment": "ticket 4711: office printer"})
    assert ok.status_code == 200, ok.text
    events = (await client.get("/api/audit", params={"kind": "rule", "limit": 50})).json()["items"]
    assert any(e["details"].get("comment") == "ticket 4711: office printer" for e in events)


@pytest.mark.asyncio
async def test_a_bulk_deploy_needs_the_comment_too(client, respx_mock, monkeypatch):
    _mock_fw(respx_mock)
    _own(monkeypatch, ADMIN_PC)
    await _require_comment(client)
    host = await _create_host(client, "fw-a", FW)
    rule = await _rule(client, [host["id"]], "allow 8123", action="pass", destination_port="8123")
    token = (await client.get(f"/api/rules/{rule['id']}/preview")).json()["token"]
    resp = await client.post("/api/rules/bulk/deploy", json={"items": [{"rule_id": rule["id"], "preview_token": token}]})
    assert resp.json()["results"][0]["error"]["code"] == "DEPLOY_COMMENT_REQUIRED"
