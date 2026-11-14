"""Interface roles: {{IF_NAME}} as the interface of a central object, one real interface per firewall, hosts without the role skipped."""
import json

import httpx
import pytest

from app.models.host import OpnsenseHost
from app.models.host_variable import HostVariable
from app.services import variables_service as vs
from tests.test_policy_hierarchy import _create_group, _create_host_in_group, _hrule_payload
from tests.test_port_forward import _pf_payload
from tests.test_rules import _create_host, _rule_payload


def _host(name="fw", **values):
    host = OpnsenseHost(name=name, base_url="https://x", api_key="k")
    host.variables = [HostVariable(name=n, value=v) for n, v in values.items()]
    return host


def _builder(entity, host):
    return vs.resolve({"interface": entity}, host)


# ── unit ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["lan", "opt3", "vlan0.20", "{{IF_GUEST}}", "{{ IF_GUEST }}"])
def test_valid_interface_fields(value):
    assert "{{" not in vs.validate_interface_field(value).replace("{{IF_GUEST}}", "")


@pytest.mark.parametrize("value", ["lan{{IF_GUEST}}", "{{IF_A}}{{IF_B}}", "{{LAN_NET}}", "{{IF_GUEST", "IF_GUEST}}"])
def test_invalid_interface_fields(value):
    with pytest.raises(ValueError):
        vs.validate_interface_field(value)


def test_role_helpers():
    assert vs.is_role("IF_GUEST") and not vs.is_role("LAN_NET") and not vs.is_role("XIF_GUEST")
    assert vs.ROLE_NAME_RE.fullmatch("IF_GUEST") and not vs.ROLE_NAME_RE.fullmatch("IF_") and not vs.ROLE_NAME_RE.fullmatch("IF_a")
    with_role, without = _host("a", IF_GUEST="opt3"), _host("b")
    assert vs.applicable_hosts(_builder, "{{IF_GUEST}}", [with_role, without]) == [with_role]
    # a host missing an ordinary variable is NOT skipped: that stays an error the deploy reports
    assert vs.applicable_hosts(_builder, "{{LAN_NET}}", [without]) == [without]
    assert vs.is_skipped(_builder, "{{LAN_NET}} {{IF_GUEST}}", without)
    row = vs.preview_row_missing(without, ["IF_GUEST"], None)
    assert row["action"] == "not_applicable" and "GUEST" in row["message"]
    assert vs.preview_row_missing(without, ["LAN_NET"], None)["action"] == "missing_variable"
    vs.ensure_resolvable(_builder, "{{IF_GUEST}}", [with_role, without])  # the host without the role is not an error
    with pytest.raises(vs.MissingVariables):
        vs.ensure_resolvable(_builder, "{{LAN_NET}}", [without])


# ── API ──────────────────────────────────────────────────────────────────


def _options(*names):
    return httpx.Response(200, json={"rule": {"interface": {n: {"value": n.upper(), "selected": 0} for n in names}}})


async def _setup(client, respx_mock, fw1=("lan", "opt3"), fw2=("lan",)):
    for base, names in (("fw1", fw1), ("fw2", fw2)):
        respx_mock.get(f"https://{base}.test/api/firewall/filter/getRule").mock(return_value=_options(*names))
        respx_mock.get(f"https://{base}.test/api/firewall/alias/searchItem").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    h1 = await _create_host(client, "fw1", "https://fw1.test")
    h2 = await _create_host(client, "fw2", "https://fw2.test")
    return h1, h2


async def _set(client, host, **values):
    return await client.put(f"/api/hosts/{host['id']}/variables", json={"variables": values})


def _mock_deploy(respx_mock, *bases):
    routes = {}
    for b in bases:
        routes[b] = respx_mock.post(f"https://{b}.test/api/firewall/filter/addRule").mock(
            return_value=httpx.Response(200, json={"result": "saved", "uuid": f"u-{b}"}))
        respx_mock.post(f"https://{b}.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    return routes


@pytest.mark.asyncio
async def test_role_values_are_validated_live(client, respx_mock):
    h1, _ = await _setup(client, respx_mock)
    assert (await _set(client, h1, IF_GUEST="opt3")).status_code == 200
    for bad in ({"IF_": "opt3"}, {"IF_guest": "opt3"}, {"IF_GUEST": "opt 3"}, {"IF_GUEST": "opt9"}):
        resp = await _set(client, h1, **bad)
        assert resp.status_code == 422 and resp.json()["detail"]["code"] == "VARIABLE_INVALID", bad
    got = (await client.get(f"/api/hosts/{h1['id']}/variables")).json()["variables"]
    assert [(v["name"], v["value"], v["role"]) for v in got] == [("IF_GUEST", "opt3", True)]


@pytest.mark.asyncio
async def test_a_host_without_the_role_is_skipped_not_an_error(client, respx_mock):
    h1, h2 = await _setup(client, respx_mock)
    await _set(client, h1, IF_GUEST="opt3")
    rule = (await client.post("/api/rules", json=_rule_payload([h1["id"], h2["id"]], interface="{{IF_GUEST}}"))).json()
    assert rule["interface"] == "{{IF_GUEST}}"
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    rows = {r["host_name"]: r for r in preview["hosts"]}
    assert rows["fw1"]["action"] == "create" and rows["fw2"]["action"] == "not_applicable"
    assert {c["field"]: c["after"] for c in rows["fw1"]["changes"]}["interface"] == "opt3"
    assert preview["can_deploy"] is True and any("Rolle" in w for w in preview["warnings"])
    routes = _mock_deploy(respx_mock, "fw1")  # an unmocked call to fw2 would fail the test
    resp = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})
    assert resp.status_code == 200, resp.text
    assert [b["rule"]["interface"] for b in map(lambda c: json.loads(c.request.content), routes["fw1"].calls)] == ["opt3"]
    deployed = (await client.get("/api/rules")).json()[0]["deployments"]
    assert [d["host_name"] for d in deployed] == ["fw1"]


@pytest.mark.asyncio
async def test_each_firewall_gets_its_own_interface_for_the_same_role(client, respx_mock):
    # the point of the feature: opt3 on one firewall, vlan0.20 on the other, no INTERFACE_MEANING_DIFFERS
    h1, h2 = await _setup(client, respx_mock, fw1=("lan", "opt3"), fw2=("lan", "vlan0.20"))
    await _set(client, h1, IF_GUEST="opt3")
    await _set(client, h2, IF_GUEST="vlan0.20")
    rule = (await client.post("/api/rules", json=_rule_payload([h1["id"], h2["id"]], interface="{{IF_GUEST}}"))).json()
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    routes = _mock_deploy(respx_mock, "fw1", "fw2")
    resp = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})
    assert resp.status_code == 200, resp.text
    sent = {b: json.loads(r.calls[0].request.content)["rule"]["interface"] for b, r in routes.items()}
    assert sent == {"fw1": "opt3", "fw2": "vlan0.20"}
    assert (await client.get("/api/rules")).json()[0]["interface"] == "{{IF_GUEST}}"  # stored centrally as the role


@pytest.mark.asyncio
async def test_a_literal_interface_still_has_to_mean_the_same_everywhere(client, respx_mock):
    h1, h2 = await _setup(client, respx_mock, fw1=("lan", "opt3"), fw2=("lan", "opt3"))
    respx_mock.get("https://fw2.test/api/firewall/filter/getRule").mock(
        return_value=httpx.Response(200, json={"rule": {"interface": {"opt3": {"value": "Other", "selected": 0}}}}))
    rule = (await client.post("/api/rules", json=_rule_payload([h1["id"], h2["id"]], interface="opt3"))).json()
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    resp = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "INTERFACE_MEANING_DIFFERS"


@pytest.mark.asyncio
async def test_no_target_with_the_role_means_nothing_is_deployed(client, respx_mock):
    h1, h2 = await _setup(client, respx_mock)
    rule = (await client.post("/api/rules", json=_rule_payload([h1["id"], h2["id"]], interface="{{IF_GUEST}}"))).json()
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    assert preview["can_deploy"] is False and {r["action"] for r in preview["hosts"]} == {"not_applicable"}
    resp = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "ROLE_NOT_APPLICABLE_ANYWHERE"


@pytest.mark.asyncio
async def test_a_role_whose_interface_vanished_is_refused_before_anything_is_written(client, respx_mock):
    h1, _ = await _setup(client, respx_mock)
    await _set(client, h1, IF_GUEST="opt3")
    rule = (await client.post("/api/rules", json=_rule_payload([h1["id"]], interface="{{IF_GUEST}}"))).json()
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    respx_mock.get("https://fw1.test/api/firewall/filter/getRule").mock(return_value=_options("lan"))  # opt3 was removed meanwhile
    resp = await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "ROLE_INTERFACE_UNKNOWN"
    assert "fw1=opt3" in resp.json()["detail"]["params"]["detail"]
    assert (await client.get("/api/rules")).json()[0]["deployments"] == []


@pytest.mark.asyncio
async def test_port_forward_uses_roles_too(client, respx_mock):
    h1, h2 = await _setup(client, respx_mock)
    await _set(client, h1, IF_GUEST="opt3")
    rule = (await client.post("/api/port-forward", json=_pf_payload([h1["id"], h2["id"]], interface="{{IF_GUEST}}")))
    assert rule.status_code in (200, 201), rule.text
    preview = (await client.get(f"/api/port-forward/{rule.json()['id']}/preview")).json()
    assert {r["host_name"]: r["action"] for r in preview["hosts"]} == {"fw1": "create", "fw2": "not_applicable"}


@pytest.mark.asyncio
async def test_the_interface_field_accepts_only_an_identifier_or_exactly_one_role(client, respx_mock):
    h1, _ = await _setup(client, respx_mock)
    for bad in ("lan{{IF_GUEST}}", "{{LAN_NET}}", "{{IF_A}}{{IF_B}}"):
        resp = await client.post("/api/rules", json=_rule_payload([h1["id"]], interface=bad))
        assert resp.status_code == 422, bad
    resp = await client.post("/api/policy-hierarchy/rules", json=_hrule_payload("00000000-0000-0000-0000-000000000000", interface="lan{{IF_GUEST}}"))
    assert resp.status_code == 422 and "Rolle" in resp.text  # a validation error, not "group not found"


@pytest.mark.asyncio
async def test_interface_roles_listing(client, respx_mock):
    h1, h2 = await _setup(client, respx_mock, fw1=("lan", "opt3"), fw2=("lan", "vlan0.20"))
    await _set(client, h1, IF_GUEST="opt3", SITE="Nord")
    await _set(client, h2, IF_GUEST="vlan0.20")
    got = (await client.get("/api/interface-roles")).json()
    assert got == {"roles": [{"name": "IF_GUEST", "hosts": {"fw1": "opt3", "fw2": "vlan0.20"}}]}


@pytest.mark.asyncio
async def test_the_hierarchy_order_is_restored_for_the_resolved_interface(client, respx_mock, monkeypatch):
    calls = []

    async def fake_reassert(db, host, interfaces):
        calls.append((host.name, set(interfaces)))

    monkeypatch.setattr("app.services.policy_hierarchy_deploy_service.reassert_after_managed_change", fake_reassert)
    h1, h2 = await _setup(client, respx_mock, fw1=("lan", "opt3"), fw2=("lan", "vlan0.20"))
    await _set(client, h1, IF_GUEST="opt3")
    await _set(client, h2, IF_GUEST="vlan0.20")
    rule = (await client.post("/api/rules", json=_rule_payload([h1["id"], h2["id"]], interface="{{IF_GUEST}}"))).json()
    preview = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    _mock_deploy(respx_mock, "fw1", "fw2")
    assert (await client.post(f"/api/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})).status_code == 200
    assert sorted(calls) == [("fw1", {"opt3"}), ("fw2", {"vlan0.20"})]


# ── hierarchy rules ──────────────────────────────────────────────────────


def test_hierarchy_order_uses_the_interface_each_host_resolves_to():
    from types import SimpleNamespace as NS
    from app.services.policy_hierarchy_service import ordered_hierarchy_rules
    group = NS(id=1, parent_id=None)
    rule = lambda i, interface, seq: NS(id=i, hierarchy_group_id=1, rulebase="pre", interface=interface, enabled=True, sequence=seq)  # noqa: E731
    rules = [rule(1, "{{IF_GUEST}}", 0), rule(2, "opt3", 1), rule(3, "lan", 2), rule(4, "{{IF_GUEST}}", 3)]
    with_role = NS(name="a", policy_hierarchy_group_id=1, variables=[HostVariable(name="IF_GUEST", value="opt3")])
    without = NS(name="b", policy_hierarchy_group_id=1, variables=[])
    other = NS(name="c", policy_hierarchy_group_id=1, variables=[HostVariable(name="IF_GUEST", value="vlan0.20")])
    ids = lambda host, interface: [r.id for r in ordered_hierarchy_rules(host, interface, "pre", [group], rules)]  # noqa: E731
    assert ids(with_role, "opt3") == [1, 2, 4]  # the role and the literal opt3 share one list and keep their sequence
    assert ids(with_role, "lan") == [3]
    assert ids(without, "opt3") == [2]  # a host without the role never carries the role's rules
    assert ids(other, "vlan0.20") == [1, 4] and ids(other, "opt3") == [2]


@pytest.mark.asyncio
async def test_hierarchy_rule_with_a_role_deploys_each_firewall_its_own_interface(client, respx_mock, monkeypatch):
    ordered = []

    async def fake_reassert(db, host, interface):
        ordered.append((host.name, interface))

    monkeypatch.setattr("app.services.policy_hierarchy_deploy_service.reassert_ordering", fake_reassert)
    for base, names in (("fw1", ("lan", "opt3")), ("fw2", ("lan", "vlan0.20")), ("fw3", ("lan",))):
        respx_mock.get(f"https://{base}.test/api/firewall/filter/getRule").mock(return_value=_options(*names))
        respx_mock.get(f"https://{base}.test/api/firewall/filter/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))
        respx_mock.get(f"https://{base}.test/api/firewall/alias/searchItem").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/get_vip_status").mock(return_value=httpx.Response(200, json={"rows": []}))
    group = await _create_group(client, "Root")
    hosts = {n: await _create_host_in_group(client, n, f"https://{n}.test", group["id"]) for n in ("fw1", "fw2", "fw3")}
    await _set(client, hosts["fw1"], IF_GUEST="opt3")
    await _set(client, hosts["fw2"], IF_GUEST="vlan0.20")
    resp = await client.post("/api/policy-hierarchy/rules", json=_hrule_payload(group["id"], interface="{{IF_GUEST}}"))
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    preview = (await client.get(f"/api/policy-hierarchy/rules/{rule['id']}/preview")).json()
    rows = {r["host_name"]: r for r in preview["hosts"]}
    assert (rows["fw1"]["action"], rows["fw2"]["action"], rows["fw3"]["action"]) == ("create", "create", "not_applicable")
    assert {c["field"]: c["after"] for c in rows["fw2"]["changes"]}["interface"] == "vlan0.20" and preview["can_deploy"] is True
    routes = _mock_deploy(respx_mock, "fw1", "fw2")  # fw3 is not mocked: any call to it fails the test
    resp = await client.post(f"/api/policy-hierarchy/rules/{rule['id']}/deploy", json={"preview_token": preview["token"]})
    assert resp.status_code == 200, resp.text
    sent = {b: json.loads(r.calls[0].request.content)["rule"]["interface"] for b, r in routes.items()}
    assert sent == {"fw1": "opt3", "fw2": "vlan0.20"}
    assert sorted(ordered) == [("fw1", "opt3"), ("fw2", "vlan0.20")]  # the position is fixed on the interface each firewall really has
    assert (await client.get(f"/api/policy-hierarchy/rules/{rule['id']}/preview")).json()["hosts"][2]["action"] == "not_applicable"
    # the host page lists the hierarchy rule as a user of the role
    used = (await client.get(f"/api/hosts/{hosts['fw1']['id']}/variables")).json()["variables"][0]["used_by"]
    assert [u["kind"] for u in used] == ["policy_hierarchy_rule"]
