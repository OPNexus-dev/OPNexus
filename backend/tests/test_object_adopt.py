"""Taking over aliases, port forwards, 1:1 NAT and outbound NAT that were created by hand on a firewall.

The live rows below have the shape recorded from OPNsense 26.7 (test-fw-3, 2026-10-20), see the module
docstring of app/services/object_adopt_service.py."""
import json

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent
from tests.helpers import deploy_approved
from tests.test_api_tokens import _login

BASE = "https://fw-adopt-obj.test"
API = f"{BASE}/api/firewall"


async def _host(client, respx_mock, name="fw-obj"):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    r = await client.post("/api/hosts", json={"name": name, "base_url": BASE, "api_key": "k", "api_secret": "s"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _search(respx_mock, path, rows):
    return respx_mock.get(f"{API}/{path}").mock(return_value=httpx.Response(200, json={"rows": rows, "total": len(rows)}))


def _mode(respx_mock, mode="hybrid"):
    respx_mock.get(f"{API}/source_nat/get").mock(return_value=httpx.Response(
        200, json={"filter": {"general": {"snat_mode": {m: {"selected": 1 if m == mode else 0} for m in
                                                          ("automatic", "hybrid", "advanced", "disabled")}}}}))


def _writes(respx_mock):
    return [c for c in respx_mock.calls if c.request.method != "GET"]


# ── live rows (shapes captured from OPNsense 26.7) ──────────────────────

def alias_row(name, type_="host", content="10.9.9.1\n10.9.9.2", **over):
    row = {"uuid": f"uuid-{name}", "enabled": "1", "name": name, "type": type_, "%type": type_.title(),
           "content": content, "description": f"{name} desc", "proto": "", "interface": "", "counters": "0",
           "updatefreq": "", "categories": ""}
    row.update(over)
    return row


SYSTEM_ALIASES = [
    alias_row("bogons", "external", "", description="bogon networks (internal)"),
    alias_row("sshlockout", "external", "", description="abuse lockout table (internal)"),
    alias_row("__lan_network", "internal", "", description="lan net"),
]


def pf_row(**over):
    row = {"uuid": "pf-1", "ipprotocol": "inet", "protocol": "tcp", "%protocol": "TCP", "disabled": "0", "nordr": "0",
           "interface": "wan", "%interface": "WAN", "source.network": "any", "source.port": "", "source.not": "0",
           "destination.network": "wanip", "destination.port": "8443", "destination.not": "0", "target": "192.168.1.50",
           "local-port": "443", "natreflection": "", "descr": "Web", "pass": "", "log": "0", "poolopts": "", "tag": "",
           "tagged": "", "nosync": "0", "categories": "", "is_automatic": False}
    row.update(over)
    return row


def o2o_row(**over):
    row = {"uuid": "o2o-1", "enabled": "1", "interface": "wan", "%interface": "WAN", "type": "binat",
           "source_net": "192.168.1.10", "source_not": "0", "destination_net": "any", "destination_not": "0",
           "external": "203.0.113.201", "natreflection": "", "log": "0", "description": "Mail 1:1", "categories": ""}
    row.update(over)
    return row


def snat_row(**over):
    row = {"uuid": "snat-1", "enabled": "1", "interface": "wan", "%interface": "WAN", "ipprotocol": "inet",
           "protocol": "any", "source_net": "192.168.1.0/24", "source_not": "0", "source_port": "",
           "destination_net": "any", "destination_not": "0", "destination_port": "", "target": "", "target_port": "",
           "nonat": "0", "nosync": "0", "staticnatport": "0", "log": "0", "tag": "", "tagged": "",
           "description": "LAN out", "categories": ""}
    row.update(over)
    return row


AUTOMATIC_NAT = [
    {"uuid": "lockout_0", "interface": "lan", "descr": "Anti-Lockout Rule", "is_automatic": True},
    {"uuid": "automatic_wan", "interface": "wan", "description": "Auto created rule", "is_automatic": True},
]

# kind -> (url prefix, search path, row factory, id field of the preview)
NAT_KINDS = {
    "port-forward": ("d_nat/searchRule", pf_row, "Web"),
    "one-to-one": ("one_to_one/searchRule", o2o_row, "Mail 1:1"),
    "outbound-nat": ("source_nat/searchRule", snat_row, "LAN out"),
}


# ── aliases ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alias_candidates_mark_system_unsupported_and_nested(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _search(respx_mock, "alias/searchItem", SYSTEM_ALIASES + [
        alias_row("ok_hosts"),
        alias_row("ok_nets", "network", "10.0.0.0/8\n192.168.0.0/16"),
        alias_row("ok_ports", "port", "8080\n9000:9010"),
        alias_row("ok_group", "networkgroup", "ok_nets\n__lan_network"),
        alias_row("a_url", "url", "https://example.org/list.txt", updatefreq="1"),
        alias_row("geo", "geoip", "DE", proto="IPv4"),
        alias_row("uses_url", "host", "a_url"),
        alias_row("uses_hosts", "host", "ok_hosts\n10.9.9.3"),
        alias_row("asn_v4", "asn", "64496", proto="IPv4"),
        alias_row("bad name", "host", "10.1.1.1"),
        alias_row("off", "host", "10.9.9.9", enabled="0"),
    ])
    resp = await client.get(f"/api/aliases/unmanaged?host_id={host_id}")
    assert resp.status_code == 200, resp.text
    by = {r["title"]: r for r in resp.json()}

    for name in ("bogons", "sshlockout", "__lan_network"):
        assert by[name]["reasons"] == ["system"] and not by[name]["adoptable"]
    for name in ("ok_hosts", "ok_nets", "ok_ports", "off"):
        assert by[name]["adoptable"] is True and by[name]["reasons"] == []
    assert by["off"]["enabled"] is False
    # __lan_network is OPNsense's own and fine; ok_nets is a live alias that is not managed yet
    assert by["ok_group"]["reasons"] == ["references_unmanaged_alias"] and by["ok_group"]["bulk_adoptable"] is True
    assert by["ok_group"]["reason_details"]["references_unmanaged_alias"] == "ok_nets"
    assert by["a_url"]["reasons"] == ["unsupported_type"] and by["geo"]["reasons"] == ["unsupported_type"]
    assert by["a_url"]["reason_details"]["unsupported_type"]
    assert by["uses_url"]["reasons"] == ["references_unmanaged_alias"]
    assert by["uses_url"]["reason_details"]["references_unmanaged_alias"] == "a_url"
    assert by["uses_url"]["bulk_adoptable"] is False  # its target can never be adopted
    assert by["asn_v4"]["reasons"] == ["proto_set"]
    assert by["bad name"]["reasons"] == ["name_invalid"]


@pytest.mark.asyncio
async def test_nested_alias_waits_for_its_target_but_bulk_orders_them(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _search(respx_mock, "alias/searchItem", [
        alias_row("outer", "host", "inner\n10.0.0.9"),
        alias_row("inner", "host", "base"),
        alias_row("base", "host", "10.0.0.1"),
    ])
    by = {r["title"]: r for r in (await client.get(f"/api/aliases/unmanaged?host_id={host_id}")).json()}
    assert by["base"]["adoptable"] is True
    for name, target in (("inner", "base"), ("outer", "inner")):
        assert by[name]["adoptable"] is False and by[name]["bulk_adoptable"] is True
        assert by[name]["reason_details"]["references_unmanaged_alias"] == target
    # alone it is refused ...
    refused = await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "uuid-outer"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "OBJECT_NOT_ADOPTABLE"
    assert "references_unmanaged_alias" in refused.json()["detail"]["params"]["reasons"]
    import json  # the UI fills "{detail}" of the reason text from this
    assert json.loads(refused.json()["detail"]["params"]["reason_details"]) == {"references_unmanaged_alias": "inner"}
    # ... after its target is managed it works
    assert (await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "uuid-base"})).status_code == 201
    assert (await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "uuid-inner"})).status_code == 201
    assert (await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "uuid-outer"})).status_code == 201


@pytest.mark.asyncio
async def test_adopt_alias_records_it_without_writing_and_preview_is_unchanged(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _search(respx_mock, "alias/searchItem", SYSTEM_ALIASES + [
        alias_row("web_servers", "host", "10.0.0.1\n 10.0.0.2 \n\nexample.org", description="Web tier"),
    ])
    resp = await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "uuid-web_servers"})
    assert resp.status_code == 201, resp.text
    alias = resp.json()
    assert (alias["name"], alias["type"], alias["description"], alias["enabled"]) == ("web_servers", "host", "Web tier", True)
    assert alias["content"] == "10.0.0.1\n10.0.0.2\nexample.org"
    assert alias["target_host_ids"] == [host_id]
    assert [d["status"] for d in alias["deployments"]] == ["success"]
    assert alias["workflow_state"] == "synced"
    assert not any("/firewall/" in str(c.request.url) for c in _writes(respx_mock))

    preview = (await client.get(f"/api/aliases/{alias['id']}/preview")).json()
    assert [(h["action"], h["changes"]) for h in preview["hosts"]] == [("unchanged", [])]
    assert preview["can_deploy"] is False
    # no longer offered; adopting again is refused
    assert [r["title"] for r in (await client.get(f"/api/aliases/unmanaged?host_id={host_id}")).json()
            if r["reasons"] != ["system"]] == []
    again = await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "uuid-web_servers"})
    assert again.status_code == 409 and "already_managed" in again.json()["detail"]["params"]["reasons"]

    # audit: the deployment record (kind alias, remote_uuid) is written with the adoption
    async with SessionLocal() as db:
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "alias"))).all()
    assert any(e.entity_name == "web_servers" and e.details["remote_uuid"] == "uuid-web_servers"
               and e.message == "Von der Firewall übernommen" for e in events)


@pytest.mark.asyncio
async def test_edit_after_alias_adopt_updates_the_same_remote_object(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _search(respx_mock, "alias/searchItem", [alias_row("web_servers", "host", "10.0.0.1")])
    alias = (await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "uuid-web_servers"})).json()
    edit = {"name": "web_servers", "type": "host", "content": "10.0.0.1\n10.0.0.2", "description": "web_servers desc",
            "enabled": True, "target_host_ids": [host_id], "target_group_ids": [], "expected_updated_at": alias["updated_at"]}
    assert (await client.put(f"/api/aliases/{alias['id']}", json=edit)).status_code == 200
    add = respx_mock.post(f"{API}/alias/addItem").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "dup"}))
    setter = respx_mock.post(f"{API}/alias/setItem/uuid-web_servers").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(f"{API}/alias/reconfigure").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    resp = await deploy_approved(client, f"/api/aliases/{alias['id']}/deploy")
    assert resp.status_code == 200, resp.text
    assert setter.called and not add.called  # no duplicate
    assert json.loads(setter.calls.last.request.content)["alias"]["content"] == "10.0.0.1\n10.0.0.2"
    confirm = await client.post(f"/api/aliases/{alias['id']}/confirm")
    assert [d["status"] for d in confirm.json()["deployments"]] == ["success"]


@pytest.mark.asyncio
async def test_alias_name_that_exists_centrally_is_refused(client, respx_mock):
    host_id = await _host(client, respx_mock)
    assert (await client.post("/api/aliases", json={"name": "dup", "type": "host", "content": "1.1.1.1"})).status_code == 201
    _search(respx_mock, "alias/searchItem", [alias_row("dup", "host", "2.2.2.2")])
    row = (await client.get(f"/api/aliases/unmanaged?host_id={host_id}")).json()[0]
    assert row["reasons"] == ["name_exists"]
    assert (await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "uuid-dup"})).status_code == 409


@pytest.mark.asyncio
async def test_adopt_all_aliases_orders_dependencies_and_reports_skipped(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _search(respx_mock, "alias/searchItem", SYSTEM_ALIASES + [
        alias_row("outer", "host", "inner"),
        alias_row("inner", "host", "10.0.0.1"),
        alias_row("a_url", "url", "https://example.org/x"),
        alias_row("uses_url", "host", "a_url"),
        alias_row("plain", "port", "443"),
    ])
    resp = await client.post("/api/aliases/adopt-all", json={"host_id": host_id})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert sorted(a["title"] for a in body["adopted"]) == ["inner", "outer", "plain"]
    skipped = {s["title"]: s for s in body["skipped"]}
    assert set(skipped) == {"a_url", "uses_url"}  # system aliases are not reported
    assert skipped["a_url"]["reasons"] == ["unsupported_type"]
    assert skipped["uses_url"]["reasons"] == ["references_unmanaged_alias"]
    aliases = {a["name"]: a for a in (await client.get("/api/aliases")).json()}
    assert set(aliases) == {"inner", "outer", "plain"}
    assert all([d["status"] for d in a["deployments"]] == ["success"] and a["workflow_state"] == "synced" for a in aliases.values())
    # a second run finds nothing left to take over
    again = (await client.post("/api/aliases/adopt-all", json={"host_id": host_id})).json()
    assert again["adopted"] == [] and {s["title"] for s in again["skipped"]} == {"a_url", "uses_url"}


@pytest.mark.asyncio
async def test_adopt_unknown_alias_is_404(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _search(respx_mock, "alias/searchItem", [alias_row("x")])
    resp = await client.post("/api/aliases/adopt", json={"host_id": host_id, "uuid": "nope"})
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "ADOPT_OBJECT_NOT_FOUND"


# ── NAT ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_automatic_nat_rows_are_never_offered(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _mode(respx_mock)
    for prefix, (path, factory, _) in NAT_KINDS.items():
        _search(respx_mock, path, [factory(), *AUTOMATIC_NAT])
        rows = (await client.get(f"/api/{prefix}/unmanaged?host_id={host_id}")).json()
        assert [r["uuid"] for r in rows] == [factory()["uuid"]], prefix
        assert rows[0]["adoptable"] is True


@pytest.mark.asyncio
async def test_port_forward_refusals(client, respx_mock):
    host_id = await _host(client, respx_mock)
    cases = {
        "multi": (dict(interface="wan,lan"), "interface_not_single"),
        "v46": (dict(ipprotocol="inet46"), "ipprotocol_unsupported"),
        "anyproto": (dict(protocol=""), "protocol_any"),
        "nordr": (dict(nordr="1", target="", **{"local-port": ""}), "no_redirect"),
        "notarget": (dict(target=""), "no_target"),
        "linked": (dict(**{"pass": "pass"}), "filter_rule_linked"),
        "srcnot": (dict(**{"source.not": "1"}), "negated_source"),
        "dstnot": (dict(**{"destination.not": "1"}), "negated_destination"),
        "refl": (dict(natreflection="enable"), "natreflection_unsupported"),
        "nodesc": (dict(descr=""), "no_description"),
        "long": (dict(target="x" * 200), "value_too_long"),
    }
    _search(respx_mock, "d_nat/searchRule", [pf_row(**{"uuid": k, "descr": f"rule {k}", **over})
                                              for k, (over, _) in cases.items()] + [pf_row(uuid="fine")])
    rows = {r["uuid"]: r for r in (await client.get(f"/api/port-forward/unmanaged?host_id={host_id}")).json()}
    assert rows["fine"]["adoptable"] is True
    for key, (_, reason) in cases.items():
        assert reason in rows[key]["reasons"] and not rows[key]["adoptable"], key


@pytest.mark.asyncio
async def test_port_forward_adopt_maps_fields_and_keeps_unmanaged_ones(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _search(respx_mock, "d_nat/searchRule", [
        pf_row(disabled="1", natreflection="purenat", log="1", poolopts="random", tag="tg1", **{"source.network": "10.0.0.0/8"}),
        pf_row(uuid="pf-2", descr="Alias based", target="web_servers", **{"destination.port": "web_ports", "local-port": ""}),
        *AUTOMATIC_NAT,
    ])
    candidates = {r["uuid"]: r for r in (await client.get(f"/api/port-forward/unmanaged?host_id={host_id}")).json()}
    assert sorted(candidates["pf-1"]["kept"]) == ["log", "poolopts", "tag"]
    resp = await client.post("/api/port-forward/adopt", json={"host_id": host_id, "uuid": "pf-1"})
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    assert (rule["description"], rule["interface"], rule["ipprotocol"], rule["protocol"]) == ("Web", "wan", "inet", "tcp")
    assert (rule["source_net"], rule["destination_net"], rule["destination_port"]) == ("10.0.0.0/8", "wanip", "8443")
    assert (rule["target"], rule["local_port"], rule["natreflection"], rule["enabled"]) == ("192.168.1.50", "443", "purenat", False)
    assert rule["workflow_state"] == "synced" and [d["status"] for d in rule["deployments"]] == ["success"]
    preview = (await client.get(f"/api/port-forward/{rule['id']}/preview")).json()
    assert [h["action"] for h in preview["hosts"]] == ["unchanged"] and preview["can_deploy"] is False
    assert not _writes(respx_mock) or not any("/firewall/" in str(c.request.url) for c in _writes(respx_mock))

    # edit -> deploy updates the same remote rule and never sends the fields it does not manage
    edit = {k: rule[k] for k in ("description", "interface", "ipprotocol", "protocol", "source_net", "source_port",
                                 "destination_net", "destination_port", "target", "local_port", "natreflection", "enabled")}
    edit.update(target="192.168.1.51", target_host_ids=[host_id], target_group_ids=[], expected_updated_at=rule["updated_at"])
    assert (await client.put(f"/api/port-forward/{rule['id']}", json=edit)).status_code == 200
    add = respx_mock.post(f"{API}/d_nat/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "dup"}))
    setter = respx_mock.post(f"{API}/d_nat/setRule/pf-1").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(f"{API}/d_nat/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert (await deploy_approved(client, f"/api/port-forward/{rule['id']}/deploy")).status_code == 200
    assert setter.called and not add.called
    sent = json.loads(setter.calls.last.request.content)["rule"]
    assert sent["target"] == "192.168.1.51" and sent["disabled"] == "1"
    assert not {"log", "poolopts", "tag", "tagged", "nosync", "categories", "nordr"} & set(sent)


@pytest.mark.asyncio
async def test_one_to_one_adopt_and_refusals(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _search(respx_mock, "one_to_one/searchRule", [
        o2o_row(),
        o2o_row(uuid="o2", description="NAT off", type="nat", enabled="0", natreflection="enable", log="1",
                source_not="1", destination_net="10.0.0.0/8", destination_not="1"),
        o2o_row(uuid="o3", interface="wan,lan", description="multi"),
        o2o_row(uuid="o4", description=""),
        o2o_row(uuid="o5", description="weird", type="other"),
        o2o_row(uuid="o6", description="refl", natreflection="purenat"),
        *AUTOMATIC_NAT,
    ])
    rows = {r["uuid"]: r for r in (await client.get(f"/api/one-to-one/unmanaged?host_id={host_id}")).json()}
    assert rows["o2o-1"]["adoptable"] and rows["o2"]["adoptable"]
    assert rows["o3"]["reasons"] == ["interface_not_single"] and rows["o4"]["reasons"] == ["no_description"]
    assert rows["o5"]["reasons"] == ["type_unsupported"] and rows["o6"]["reasons"] == ["natreflection_unsupported"]

    rule = (await client.post("/api/one-to-one/adopt", json={"host_id": host_id, "uuid": "o2"})).json()
    assert (rule["type"], rule["enabled"], rule["natreflection"], rule["log"], rule["source_not"], rule["destination_not"]) == (
        "nat", False, "enable", True, True, True)
    assert rule["destination_net"] == "10.0.0.0/8" and rule["external"] == "203.0.113.201"
    preview = (await client.get(f"/api/one-to-one/{rule['id']}/preview")).json()
    assert [h["action"] for h in preview["hosts"]] == ["unchanged"]


@pytest.mark.asyncio
async def test_outbound_nat_adopt_and_refusals(client, respx_mock):
    host_id = await _host(client, respx_mock)
    _mode(respx_mock, "hybrid")
    _search(respx_mock, "source_nat/searchRule", [
        snat_row(),
        snat_row(uuid="s2", description="to pool", protocol="TCP", destination_port="443", target="203.0.113.205",
                 target_port="1024", staticnatport="1", log="1", enabled="0", destination_not="1", destination_net="10.0.0.0/8"),
        snat_row(uuid="s3", description="no nat", nonat="1"),
        snat_row(uuid="s4", description="v46", ipprotocol="inet46"),
        snat_row(uuid="s5", description=""),
        *AUTOMATIC_NAT,
    ])
    rows = {r["uuid"]: r for r in (await client.get(f"/api/outbound-nat/unmanaged?host_id={host_id}")).json()}
    assert rows["snat-1"]["adoptable"] and rows["s2"]["adoptable"]
    assert sorted(rows["s2"]["kept"]) == ["log", "staticnatport"]
    assert rows["s3"]["reasons"] == ["no_nat_rule"] and rows["s4"]["reasons"] == ["ipprotocol_unsupported"]
    assert rows["s5"]["reasons"] == ["no_description"]

    rule = (await client.post("/api/outbound-nat/adopt", json={"host_id": host_id, "uuid": "s2"})).json()
    assert (rule["protocol"], rule["destination_port"], rule["target"], rule["target_port"], rule["enabled"],
            rule["destination_not"]) == ("TCP", "443", "203.0.113.205", "1024", False, True)
    assert rule["source_port"] is None
    preview = (await client.get(f"/api/outbound-nat/{rule['id']}/preview")).json()
    assert [h["action"] for h in preview["hosts"]] == ["unchanged"]

    # edit -> deploy: setRule on the same uuid, nonat/log/staticnatport are not part of the payload
    edit = {k: rule[k] for k in ("description", "interface", "source_net", "destination_net", "source_not", "destination_not",
                                 "enabled", "ipprotocol", "protocol", "source_port", "destination_port", "target", "target_port")}
    edit.update(enabled=True, target_host_ids=[host_id], target_group_ids=[], expected_updated_at=rule["updated_at"])
    assert (await client.put(f"/api/outbound-nat/{rule['id']}", json=edit)).status_code == 200
    add = respx_mock.post(f"{API}/source_nat/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "dup"}))
    setter = respx_mock.post(f"{API}/source_nat/setRule/s2").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(f"{API}/source_nat/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert (await deploy_approved(client, f"/api/outbound-nat/{rule['id']}/deploy")).status_code == 200
    assert setter.called and not add.called
    sent = json.loads(setter.calls.last.request.content)["rule"]
    assert sent["enabled"] == "1" and not {"nonat", "log", "staticnatport", "nosync", "tag"} & set(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["automatic", "disabled"])
async def test_outbound_nat_adoption_needs_a_manual_capable_mode(client, respx_mock, mode):
    host_id = await _host(client, respx_mock)
    _mode(respx_mock, mode)
    search = _search(respx_mock, "source_nat/searchRule", [snat_row()])
    for call in (client.get(f"/api/outbound-nat/unmanaged?host_id={host_id}"),
                 client.post("/api/outbound-nat/adopt", json={"host_id": host_id, "uuid": "snat-1"}),
                 client.post("/api/outbound-nat/adopt-all", json={"host_id": host_id})):
        resp = await call
        assert resp.status_code == 409 and resp.json()["detail"]["code"] == "OUTBOUND_MODE_BLOCKS_ADOPT", resp.text
        assert resp.json()["detail"]["params"]["mode"] == mode
    assert not search.called
    assert (await client.get("/api/outbound-nat")).json() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", list(NAT_KINDS))
async def test_adopt_all_nat_takes_what_is_adoptable_and_is_idempotent(client, respx_mock, prefix):
    host_id = await _host(client, respx_mock)
    _mode(respx_mock)
    path, factory, title = NAT_KINDS[prefix]
    bad = {"descr": ""} if prefix == "port-forward" else {"description": ""}
    _search(respx_mock, path, [factory(), factory(uuid="second", **({"descr": "Second"} if prefix == "port-forward" else {"description": "Second"})),
                              factory(uuid="bad", **bad), *AUTOMATIC_NAT])
    result = (await client.post(f"/api/{prefix}/adopt-all", json={"host_id": host_id})).json()
    assert sorted(a["title"] for a in result["adopted"]) == sorted([title, "Second"])
    assert [(s["uuid"], s["reasons"]) for s in result["skipped"]] == [("bad", ["no_description"])]
    listed = (await client.get(f"/api/{prefix}")).json()
    assert len(listed) == 2 and all(r["workflow_state"] == "synced" for r in listed)
    again = (await client.post(f"/api/{prefix}/adopt-all", json={"host_id": host_id})).json()
    assert again["adopted"] == [] and len((await client.get(f"/api/{prefix}")).json()) == 2
    assert not any("/firewall/" in str(c.request.url) for c in _writes(respx_mock))


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["aliases", *NAT_KINDS])
async def test_roles_and_unknown_host(client, respx_mock, prefix):
    host_id = await _host(client, respx_mock)
    _mode(respx_mock)
    path = "alias/searchItem" if prefix == "aliases" else NAT_KINDS[prefix][0]
    row = alias_row("a1") if prefix == "aliases" else NAT_KINDS[prefix][1]()
    _search(respx_mock, path, [row])
    unknown = "00000000-0000-0000-0000-000000000000"
    assert (await client.get(f"/api/{prefix}/unmanaged?host_id={unknown}")).status_code == 404
    assert (await client.post(f"/api/{prefix}/adopt", json={"host_id": unknown, "uuid": "x"})).status_code == 404

    # read-only users cannot adopt (it creates central objects), operators can (it writes nothing to a firewall)
    for role, expected in (("read_only", 403), ("operator", 201)):
        user = await client.post("/api/users", json={"username": f"u-{role}-{prefix}", "password": "adopt-test-password-1", "role": role})
        assert user.status_code == 201, user.text
        session = await _login(f"u-{role}-{prefix}", "adopt-test-password-1")
        try:
            assert (await session.get(f"/api/{prefix}/unmanaged?host_id={host_id}")).status_code == 200
            resp = await session.post(f"/api/{prefix}/adopt", json={"host_id": host_id, "uuid": row["uuid"]})
            assert resp.status_code == expected, (role, resp.text)
        finally:
            await session.aclose()
