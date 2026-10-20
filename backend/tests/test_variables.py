"""Site variables: {{NAME}} placeholders filled per host, failing closed when a value is missing."""
import json

import httpx
import pytest

from app.models.host import OpnsenseHost
from app.models.host_variable import HostVariable
from app.services import variables_service as vs
from tests.test_aliases import _alias_payload
from tests.test_port_forward import _pf_payload
from tests.test_rules import _create_host, _rule_payload


def _host(name="fw", **values):
    host = OpnsenseHost(name=name, base_url="https://x", api_key="k")
    host.variables = [HostVariable(name=n, value=v) for n, v in values.items()]
    return host


# ── unit ─────────────────────────────────────────────────────────────────

def test_placeholders_are_found_and_resolved():
    payload = {"source_net": "{{LAN_NET}}", "destination_net": "any", "description": "ssh to {{ SITE }} ({{LAN_NET}})", "enabled": "1", "port": 22}
    assert vs.names_in_payload(payload) == {"LAN_NET", "SITE"}
    resolved = vs.resolve(payload, _host(LAN_NET="192.168.7.0/24", SITE="Nord"))
    assert resolved == {"source_net": "192.168.7.0/24", "destination_net": "any", "description": "ssh to Nord (192.168.7.0/24)", "enabled": "1", "port": 22}
    assert payload["source_net"] == "{{LAN_NET}}"  # the original is untouched


def test_a_missing_value_raises_naming_host_and_variable():
    with pytest.raises(vs.MissingVariables) as exc:
        vs.resolve({"a": "{{A}}", "b": "{{B}}"}, _host("fw-nord", A="1"))
    assert exc.value.missing == {"fw-nord": ["B"]}


@pytest.mark.parametrize("text", ["lowercase {{lan}}", "{LAN_NET}", "{{ }}", "{{1ABC}}", "$LAN_NET", "{{LAN-NET}}"])
def test_things_that_are_not_placeholders_are_left_alone(text):
    assert vs.names_in(text) == set()
    assert vs.resolve({"x": text}, _host())["x"] == text


def test_values_are_inserted_literally_not_interpreted():
    out = vs.resolve({"x": "{{A}}"}, _host(A="\\1 $& {{B}}"))
    assert out["x"] == "\\1 $& {{B}}"  # no regex-group or second-level expansion


def test_value_and_name_validation():
    assert vs.NAME_RE.fullmatch("LAN_NET2") and not vs.NAME_RE.fullmatch("lan") and not vs.NAME_RE.fullmatch("2LAN") and not vs.NAME_RE.fullmatch("A" * 41)
    for ok in ("192.168.7.0/24", "fw-nord.example.com", "10.0.0.1,10.0.0.2", "Filiale Nord", "a@b+c"):
        assert vs.VALUE_RE.fullmatch(ok)
    for bad in ("", "{{X}}", "a\nb", "ok\n", 'a"b', "a;b", "a" * 256, "<x>"):
        assert not vs.VALUE_RE.fullmatch(bad)


# ── API ──────────────────────────────────────────────────────────────────

async def _mock_hosts(respx_mock, *bases):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    # multi-host deploys compare what the interface name means on every target
    respx_mock.get(url__regex=r".*/firewall/filter/getRule").mock(
        return_value=httpx.Response(200, json={"rule": {"interface": {"lan": {"value": "LAN", "selected": 0}}}}))
    respx_mock.get(url__regex=r".*/firewall/alias/searchItem.*").mock(return_value=httpx.Response(200, json={"rows": []}))


def _bodies(route):
    return [json.loads(call.request.content) for call in route.calls]


@pytest.mark.asyncio
async def test_variables_api_roundtrip_validation_and_usage_discovery(client, respx_mock):
    await _mock_hosts(respx_mock)
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    assert (await client.get(f"/api/hosts/{h1['id']}/variables")).json() == {"variables": []}
    await client.post("/api/rules", json=_rule_payload([h1["id"]], description="ssh {{SITE}}", source_net="{{LAN_NET}}"))
    await client.post("/api/aliases", json=_alias_payload([h2["id"]], content="{{DNS_IP}}"))
    # fw1 uses LAN_NET and SITE, nothing yet defined -> both missing; fw2's alias is not fw1's business.
    got = (await client.get(f"/api/hosts/{h1['id']}/variables")).json()["variables"]
    assert [(v["name"], v["value"], v["missing"]) for v in got] == [("LAN_NET", None, True), ("SITE", None, True)]
    assert got[0]["used_by"] == [{"kind": "rule", "id": got[0]["used_by"][0]["id"], "label": "ssh {{SITE}}"}]
    put = await client.put(f"/api/hosts/{h1['id']}/variables", json={"variables": {"LAN_NET": "192.168.7.0/24", "EXTRA": "x"}})
    assert put.status_code == 200, put.text
    by_name = {v["name"]: v for v in put.json()["variables"]}
    assert by_name["LAN_NET"]["missing"] is False and by_name["SITE"]["missing"] is True and by_name["EXTRA"]["used_by"] == []
    # PUT replaces the whole set.
    put = await client.put(f"/api/hosts/{h1['id']}/variables", json={"variables": {"SITE": "Nord"}})
    assert {v["name"] for v in put.json()["variables"]} == {"LAN_NET", "SITE"}  # LAN_NET only listed because the rule still uses it
    assert next(v for v in put.json()["variables"] if v["name"] == "LAN_NET")["value"] is None
    for bad in ({"lan": "1"}, {"OK": "bad;value"}, {"OK": ""}, {"OK": "{{X}}"}):
        resp = await client.put(f"/api/hosts/{h1['id']}/variables", json={"variables": bad})
        assert resp.status_code == 422 and resp.json()["detail"]["code"] == "VARIABLE_INVALID", bad
    import uuid
    assert (await client.get(f"/api/hosts/{uuid.uuid4()}/variables")).status_code == 404


@pytest.mark.asyncio
async def test_each_host_gets_its_own_values_in_preview_and_deploy(client, respx_mock):
    await _mock_hosts(respx_mock)
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    await client.put(f"/api/hosts/{h1['id']}/variables", json={"variables": {"LAN_NET": "192.168.7.0/24", "SITE": "Nord"}})
    await client.put(f"/api/hosts/{h2['id']}/variables", json={"variables": {"LAN_NET": "10.20.0.0/16", "SITE": "Sued"}})
    rule = (await client.post("/api/rules", json=_rule_payload([h1["id"], h2["id"]], description="ssh {{SITE}}", source_net="{{LAN_NET}}"))).json()
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    rows = {r["host_name"]: r for r in preview["hosts"]}
    after = lambda r: {c["field"]: c["after"] for c in r["changes"]}  # noqa: E731
    assert after(rows["fw1"])["source_net"] == "192.168.7.0/24" and after(rows["fw1"])["description"] == "ssh Nord"
    assert after(rows["fw2"])["source_net"] == "10.20.0.0/16" and after(rows["fw2"])["description"] == "ssh Sued"
    assert preview["can_deploy"] is True
    routes = {b: respx_mock.post(f"https://{b}.test/api/firewall/filter/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": f"u-{b}"})) for b in ("fw1", "fw2")}
    for b in ("fw1", "fw2"):
        respx_mock.post(f"https://{b}.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    resp = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})
    assert resp.status_code == 200, resp.text
    sent = {b: _bodies(r)[0]["rule"] for b, r in routes.items()}
    assert (sent["fw1"]["source_net"], sent["fw1"]["description"]) == ("192.168.7.0/24", "ssh Nord")
    assert (sent["fw2"]["source_net"], sent["fw2"]["description"]) == ("10.20.0.0/16", "ssh Sued")
    assert "{{" not in json.dumps(sent)
    # The centrally stored rule keeps its placeholders; the deployment stores what was really sent.
    assert (await client.get("/api/rules")).json()[0]["source_net"] == "{{LAN_NET}}"
    # Changing a host's value shows up as a change for that host only.
    await client.put(f"/api/hosts/{h2['id']}/variables", json={"variables": {"LAN_NET": "10.30.0.0/16", "SITE": "Sued"}})
    for r in routes.values():
        pass
    await client.post(f"/api/rules/{rule['id']}/confirm")
    again = {r["host_name"]: r["action"] for r in (await client.get(f"/api/rules/{rule['id']}/preview")).json()["hosts"]}
    assert again == {"fw1": "unchanged", "fw2": "update"}


@pytest.mark.asyncio
async def test_a_missing_value_blocks_the_whole_deploy_before_anything_is_written(client, respx_mock):
    await _mock_hosts(respx_mock)
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    await client.put(f"/api/hosts/{h1['id']}/variables", json={"variables": {"LAN_NET": "192.168.7.0/24"}})
    rule = (await client.post("/api/rules", json=_rule_payload([h1["id"], h2["id"]], source_net="{{LAN_NET}}"))).json()
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    rows = {r["host_name"]: r for r in preview["hosts"]}
    assert rows["fw2"]["action"] == "missing_variable" and "LAN_NET" in rows["fw2"]["message"] and rows["fw1"]["action"] == "create"
    assert preview["can_deploy"] is False and any("Variable" in w for w in preview["warnings"])
    # respx would fail on any unmocked firewall call: nothing may be sent, not even to the host that has its value.
    resp = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "VARIABLES_MISSING"
    assert "fw2: LAN_NET" in resp.json()["detail"]["params"]["details"] and "fw1" not in resp.json()["detail"]["params"]["details"]
    assert (await client.get("/api/rules")).json()[0]["deployments"] == []


@pytest.mark.asyncio
async def test_aliases_and_port_forwards_use_variables_too(client, respx_mock):
    await _mock_hosts(respx_mock)
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    await client.put(f"/api/hosts/{h1['id']}/variables", json={"variables": {"DNS_IP": "10.9.9.9", "WEB": "10.7.0.20"}})
    alias = (await client.post("/api/aliases", json=_alias_payload([h1["id"]], content="{{DNS_IP}}"))).json()
    pf = (await client.post("/api/port-forward", json=_pf_payload([h1["id"]], target="{{WEB}}"))).json()
    a_prev = (await client.get(f"/api/aliases/{alias['id']}/preview")).json()
    assert {c["field"]: c["after"] for c in a_prev["hosts"][0]["changes"]}["content"] == "10.9.9.9"
    p_prev = (await client.get(f"/api/port-forward/{pf['id']}/preview")).json()
    assert {c["field"]: c["after"] for c in p_prev["hosts"][0]["changes"]}["target"] == "10.7.0.20"
    await client.put(f"/api/hosts/{h1['id']}/variables", json={"variables": {"DNS_IP": "10.9.9.9"}})
    p_prev = (await client.get(f"/api/port-forward/{pf['id']}/preview")).json()
    assert p_prev["hosts"][0]["action"] == "missing_variable"


@pytest.mark.asyncio
async def test_workflow_state_does_not_crash_on_missing_values(client, respx_mock):
    await _mock_hosts(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    await client.post("/api/rules", json=_rule_payload([host["id"]], source_net="{{NOPE}}"))
    listing = (await client.get("/api/rules")).json()
    assert listing[0]["workflow_state"] == "draft" and listing[0]["workflow_counts"]["changes"] == 1


@pytest.mark.asyncio
async def test_the_client_refuses_any_payload_with_an_unresolved_placeholder(client, respx_mock):
    from app.models.host import OpnsenseHost as H
    from app.services import opnsense_client, opnsense_secret_store
    import uuid
    await _mock_hosts(respx_mock)
    created = await _create_host(client, "fw1", "https://fw1.test")
    from app.database import SessionLocal
    async with SessionLocal() as db:
        host = await db.get(H, uuid.UUID(created["id"]))
        with pytest.raises(opnsense_client.OpnsenseAPIError, match="Variable"):
            await opnsense_client.add_rule(host, {"source_net": "{{LAN_NET}}", "action": "pass"})
        with pytest.raises(opnsense_client.OpnsenseAPIError, match="Variable"):
            await opnsense_client.add_rule(host, {"nested": "x {{ A_B }} y"})


@pytest.mark.asyncio
async def test_variables_follow_the_host_and_operators_may_set_them(client, respx_mock):
    from tests.test_api_tokens import _login
    await _mock_hosts(respx_mock)
    host = await _create_host(client, "fw1", "https://fw1.test")
    await client.put(f"/api/hosts/{host['id']}/variables", json={"variables": {"A": "1"}})
    assert (await client.post("/api/users", json={"username": "varop", "password": "variables-pass-1", "role": "operator"})).status_code == 201
    op = await _login("varop", "variables-pass-1")
    try:
        assert (await op.put(f"/api/hosts/{host['id']}/variables", json={"variables": {"A": "2"}})).status_code == 200
    finally:
        await op.aclose()
    assert (await client.post("/api/users", json={"username": "varro", "password": "variables-pass-1", "role": "read_only"})).status_code == 201
    ro = await _login("varro", "variables-pass-1")
    try:
        assert (await ro.get(f"/api/hosts/{host['id']}/variables")).status_code == 200
        assert (await ro.put(f"/api/hosts/{host['id']}/variables", json={"variables": {}})).status_code == 403
    finally:
        await ro.aclose()
    await client.delete(f"/api/hosts/{host['id']}")  # variables go with the host
    from app.database import SessionLocal
    from sqlalchemy import select
    async with SessionLocal() as db:
        assert (await db.scalars(select(HostVariable))).all() == []
