"""Writes to VLANs / virtual IPs / gateways / routes, and the safety around them."""
import json

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent
from tests.test_network import GATEWAYS, INTERFACES_INFO, _host, _mock

MGMT = "https://10.20.30.57"


def _url(path, base=MGMT):
    return f"{base}/api{path}"


def _backup(respx_mock, base=MGMT, status=200):
    return respx_mock.get(_url("/core/backup/download/this", base)).mock(
        return_value=httpx.Response(status, content=b"<opnsense/>"))


def _item(respx_mock, path, key, node, base=MGMT):
    respx_mock.get(_url(path, base)).mock(return_value=httpx.Response(200, json={key: node}))


async def _setup(client, respx_mock, name, base=MGMT, **overrides):
    host_id = await _host(client, respx_mock, base=base, name=name)
    _mock(respx_mock, base=base, **overrides)
    return host_id


def _body(call):
    return json.loads(call.request.content.decode())


async def _preview(client, host_id, **payload):
    return await client.post(f"/api/hosts/{host_id}/network/changes/preview", json=payload)


async def _execute(client, host_id, token, ack=False, **payload):
    return await client.post(f"/api/hosts/{host_id}/network/changes",
                             json={**payload, "preview_token": token, "acknowledge": ack})


async def _audit(host_id):
    async with SessionLocal() as db:
        rows = await db.scalars(select(AuditEvent).where(AuditEvent.host_id == host_id, AuditEvent.kind == "network")
                                .order_by(AuditEvent.created_at))
        return list(rows.all())


VLAN_ADD = {"kind": "vlan", "action": "add", "fields": {"if": "vtnet1", "tag": "20", "descr": "guests"}}


@pytest.mark.asyncio
async def test_preview_add_is_read_only_and_shows_diff(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c1")
    resp = await _preview(client, host_id, **VLAN_ADD)
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["can_execute"] is True and plan["requires_acknowledgement"] is False
    assert {c["field"] for c in plan["changes"]} == {"if", "tag", "descr"}
    assert len(plan["token"]) == 64
    assert not [c for c in respx_mock.calls if c.request.method == "POST" and "/api/hosts" not in str(c.request.url)
                and "vlan_settings" in str(c.request.url)]


@pytest.mark.asyncio
async def test_execute_add_backs_up_writes_applies_and_verifies(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c2")
    backup = _backup(respx_mock)
    add = respx_mock.post(_url("/interfaces/vlan_settings/addItem")).mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "new-vlan"}))
    reconf = respx_mock.post(_url("/interfaces/vlan_settings/reconfigure")).mock(
        return_value=httpx.Response(200, json={"status": "ok"}))
    _item(respx_mock, "/interfaces/vlan_settings/getItem/new-vlan", "vlan",
          {"if": {"vtnet1": {"selected": 1}}, "tag": "20", "descr": "guests"})
    token = (await _preview(client, host_id, **VLAN_ADD)).json()["token"]

    resp = await _execute(client, host_id, token, **VLAN_ADD)
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["uuid"] == "new-vlan" and result["verified"] is True and result["backup_id"].endswith(".xml")
    assert backup.called and reconf.called
    assert _body(add.calls.last) == {"vlan": {"if": "vtnet1", "tag": "20", "descr": "guests"}}
    assert [e.status for e in await _audit(host_id)] == ["started", "applied"]


@pytest.mark.asyncio
async def test_stale_token_is_refused_and_nothing_is_written(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c3")
    add = respx_mock.post(_url("/interfaces/vlan_settings/addItem")).mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "x"}))
    resp = await _execute(client, host_id, "0" * 64, **VLAN_ADD)
    assert resp.status_code == 409
    assert not add.called


@pytest.mark.asyncio
async def test_token_changes_when_the_fields_change(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c4")
    one = (await _preview(client, host_id, **VLAN_ADD)).json()["token"]
    other = dict(VLAN_ADD, fields={**VLAN_ADD["fields"], "tag": "21"})
    two = (await _preview(client, host_id, **other)).json()["token"]
    assert one != two
    # a token for the first request cannot be used for the second
    assert (await _execute(client, host_id, one, **other)).status_code == 409


@pytest.mark.asyncio
async def test_backup_failure_aborts_before_any_write(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c5")
    _backup(respx_mock, status=500)
    add = respx_mock.post(_url("/interfaces/vlan_settings/addItem")).mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "x"}))
    token = (await _preview(client, host_id, **VLAN_ADD)).json()["token"]
    resp = await _execute(client, host_id, token, **VLAN_ADD)
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "NETWORK_BACKUP_FAILED"
    assert not add.called
    assert [e.status for e in await _audit(host_id)] == ["aborted"]


@pytest.mark.asyncio
async def test_firewall_rejection_is_reported_and_not_applied(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c6")
    _backup(respx_mock)
    respx_mock.post(_url("/interfaces/vlan_settings/addItem")).mock(
        return_value=httpx.Response(200, json={"result": "failed", "validations": {"vlan.tag": "bad"}}))
    reconf = respx_mock.post(_url("/interfaces/vlan_settings/reconfigure")).mock(
        return_value=httpx.Response(200, json={"status": "ok"}))
    token = (await _preview(client, host_id, **VLAN_ADD)).json()["token"]
    resp = await _execute(client, host_id, token, **VLAN_ADD)
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "UPSTREAM_REJECTED"
    assert not reconf.called
    assert (await _audit(host_id))[-1].status == "rejected"


@pytest.mark.asyncio
async def test_apply_failure_is_never_reported_as_success(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c7")
    _backup(respx_mock)
    respx_mock.post(_url("/interfaces/vlan_settings/addItem")).mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "x"}))
    respx_mock.post(_url("/interfaces/vlan_settings/reconfigure")).mock(side_effect=httpx.ReadTimeout("slow"))
    token = (await _preview(client, host_id, **VLAN_ADD)).json()["token"]
    resp = await _execute(client, host_id, token, **VLAN_ADD)
    assert resp.status_code == 502 and resp.json()["detail"]["code"] == "NETWORK_APPLY_UNCERTAIN"
    assert resp.json()["detail"]["params"]["backup_id"].endswith(".xml")
    assert (await _audit(host_id))[-1].status == "apply_uncertain"


@pytest.mark.asyncio
async def test_unknown_field_is_rejected_not_forwarded(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c8")
    resp = await _preview(client, host_id, kind="vlan", action="add", fields={"if": "vtnet1", "tag": "9", "evil": "x"})
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "NETWORK_FIELD_INVALID"


@pytest.mark.asyncio
async def test_changing_the_management_vlan_needs_acknowledgement(client, respx_mock):
    # fw reached on 192.168.1.2 = the LAN VLAN device
    base = "https://192.168.1.2"
    vlans = {"rows": [{"uuid": "v-mgmt", "if": "vtnet1", "tag": "10", "pcp": "0", "descr": "", "vlanif": "vtnet1_vlan10 [LAN]"}]}
    host_id = await _setup(client, respx_mock, "fw-c9", base=base, **{
        "/interfaces/vlan_settings/searchItem": httpx.Response(200, json=vlans)})
    _item(respx_mock, "/interfaces/vlan_settings/getItem/v-mgmt", "vlan",
          {"if": {"vtnet1": {"selected": 1}}, "tag": "10", "descr": "", "vlanif": "vtnet1_vlan10"}, base=base)
    _backup(respx_mock, base)
    respx_mock.post(_url("/interfaces/vlan_settings/setItem/v-mgmt", base)).mock(
        return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(_url("/interfaces/vlan_settings/reconfigure", base)).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    change = {"kind": "vlan", "action": "set", "uuid": "v-mgmt", "fields": {"descr": "renamed"}}
    plan = (await _preview(client, host_id, **change)).json()
    assert plan["requires_acknowledgement"] is True
    assert plan["risks"][0]["code"] == "management_interface"
    refused = await _execute(client, host_id, plan["token"], **change)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "NETWORK_ACK_REQUIRED"
    allowed = await _execute(client, host_id, plan["token"], ack=True, **change)
    assert allowed.status_code == 200, allowed.text
    assert (await _audit(host_id))[0].details["acknowledged"] is True


@pytest.mark.asyncio
async def test_vip_password_never_leaves_the_server(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c10")
    _item(respx_mock, "/interfaces/vip_settings/getItem/vip1", "vip", {
        "interface": {"lan": {"selected": 1}}, "mode": {"carp": {"selected": 1}}, "network": "192.168.1.1/24",
        "vhid": "10", "password": "STORED-SECRET", "descr": "CARP LAN", "advskew": "0"})
    _backup(respx_mock)
    set_route = respx_mock.post(_url("/interfaces/vip_settings/setItem/vip1")).mock(
        return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(_url("/interfaces/vip_settings/reconfigure")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    change = {"kind": "vip", "action": "set", "uuid": "vip1", "fields": {"password": "NEW-SECRET", "advskew": "100"}}
    resp = await _preview(client, host_id, **change)
    assert "STORED-SECRET" not in resp.text and "NEW-SECRET" not in resp.text
    plan = resp.json()
    assert {"field": "password", "before": None, "after": "(gesetzt)"} in plan["changes"]
    assert [w["code"] for w in plan["warnings"]] == ["carp_change"]
    done = await _execute(client, host_id, plan["token"], **change)
    assert done.status_code == 200, done.text
    assert "NEW-SECRET" not in done.text
    assert _body(set_route.calls.last)["vip"]["password"] == "NEW-SECRET"  # but it does reach the firewall
    for event in await _audit(host_id):
        assert "NEW-SECRET" not in json.dumps(event.details) and "STORED-SECRET" not in json.dumps(event.details)


@pytest.mark.asyncio
async def test_default_gateway_and_default_route_need_acknowledgement(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c11")
    _item(respx_mock, "/routing/settings/getGateway/g1", "gateway_item",
          {"name": "WAN_GW", "interface": {"wan": {"selected": 1}}, "gateway": "10.20.30.1", "priority": "255"})
    plan = (await _preview(client, host_id, kind="gateway", action="del", uuid="g1", fields={})).json()
    assert {r["code"] for r in plan["risks"]} >= {"default_gateway", "management_gateway"}

    route = (await _preview(client, host_id, kind="route", action="add",
                            fields={"network": "0.0.0.0/0", "gateway": "WAN_GW"})).json()
    assert [r["code"] for r in route["risks"]] == ["default_route"]
    covers = (await _preview(client, host_id, kind="route", action="add",
                             fields={"network": "10.20.0.0/16", "gateway": "WAN_GW"})).json()
    assert [r["code"] for r in covers["risks"]] == ["route_covers_management"]
    harmless = (await _preview(client, host_id, kind="route", action="add",
                               fields={"network": "172.16.0.0/16", "gateway": "WAN_GW"})).json()
    assert harmless["risks"] == [] and harmless["requires_acknowledgement"] is False


@pytest.mark.asyncio
async def test_delete_is_verified_by_absence(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c12")
    _item(respx_mock, "/routes/routes/getroute/r1", "route", {"network": "10.9.0.0/16", "gateway": {"WAN_GW": {"selected": 1}}, "descr": "lab", "enabled": "1"})
    _backup(respx_mock)
    respx_mock.post(_url("/routes/routes/delroute/r1")).mock(return_value=httpx.Response(200, json={"result": "deleted"}))
    respx_mock.post(_url("/routes/routes/reconfigure")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    # after the delete the route list no longer contains it
    respx_mock.get(_url("/routes/routes/searchroute")).mock(return_value=httpx.Response(200, json={"rows": []}))
    change = {"kind": "route", "action": "del", "uuid": "r1", "fields": {}}
    plan = (await _preview(client, host_id, **change)).json()
    assert plan["risks"] == []  # 10.9.0.0/16 does not touch the management address
    done = await _execute(client, host_id, plan["token"], **change)
    assert done.status_code == 200 and done.json()["verified"] is True


@pytest.mark.asyncio
async def test_missing_object_is_404(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c13")
    respx_mock.get(_url("/interfaces/vlan_settings/getItem/ghost")).mock(return_value=httpx.Response(200, json={"vlan": {}}))
    resp = await _preview(client, host_id, kind="vlan", action="del", uuid="ghost", fields={})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_unknown_management_path_is_treated_as_risky(client, respx_mock):
    # The address OPNexus uses belongs to no interface (NAT, or a CARP address): be careful, not optimistic.
    base = "https://10.20.30.99"
    host_id = await _setup(client, respx_mock, "fw-c14", base=base)
    _item(respx_mock, "/routes/routes/getroute/r2", "route", {"network": "172.16.0.0/16", "gateway": {"WAN_GW": {"selected": 1}}, "enabled": "1"}, base=base)
    plan = (await _preview(client, host_id, kind="route", action="set", uuid="r2", fields={"descr": "x"}, )).json()
    assert [r["code"] for r in plan["risks"]] == ["management_unknown"]
    add = (await _preview(client, host_id, kind="route", action="add", fields={"network": "172.17.0.0/16", "gateway": "WAN_GW"})).json()
    assert add["risks"] == []  # a new, harmless object cannot cut anything


@pytest.mark.asyncio
async def test_template_returns_the_firewalls_own_choices(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c15")
    _item(respx_mock, "/routes/routes/getroute", "route", {
        "network": "", "descr": "", "enabled": "1", "secretless": "x",
        "gateway": {"Null4": {"value": "Null4", "selected": 0}, "WAN_GW": {"value": "WAN_GW - 10.20.30.1", "selected": 1}}})
    resp = await client.get(f"/api/hosts/{host_id}/network/template/route")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gateway"]["value"] == "WAN_GW"
    assert [o["value"] for o in body["gateway"]["options"]] == ["Null4", "WAN_GW"]
    assert "secretless" not in body  # only fields that may be sent
    assert (await client.get(f"/api/hosts/{host_id}/network/template/bogus")).status_code == 404


@pytest.mark.asyncio
async def test_inventory_reads_route_enabled_flag(client, respx_mock):
    # Real 26.7.x route rows say enabled "1"/"0", not disabled.
    rows = {"rows": [{"uuid": "r1", "network": "10.9.0.0/16", "gateway": "WAN_GW", "descr": "on", "enabled": "1"},
                     {"uuid": "r2", "network": "10.8.0.0/16", "gateway": "WAN_GW", "descr": "off", "enabled": "0"}]}
    host_id = await _setup(client, respx_mock, "fw-c16", **{"/routes/routes/searchroute": httpx.Response(200, json=rows)})
    routes = (await client.get(f"/api/hosts/{host_id}/network")).json()["routes"]
    assert [(r["network"], r["disabled"]) for r in routes] == [("10.9.0.0/16", False), ("10.8.0.0/16", True)]


@pytest.mark.asyncio
async def test_delete_preview_lists_only_meaningful_fields(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c17")
    _item(respx_mock, "/interfaces/vlan_settings/getItem/v9", "vlan",
          {"if": {"vtnet1": {"selected": 1}}, "tag": "40", "descr": "x", "vlanif": "vlan01", "pcp": ""})
    plan = (await _preview(client, host_id, kind="vlan", action="del", uuid="v9", fields={})).json()
    assert {c["field"] for c in plan["changes"]} == {"if", "tag", "descr"}


@pytest.mark.asyncio
async def test_item_endpoint_returns_current_values_without_secrets(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-c18")
    _item(respx_mock, "/interfaces/vip_settings/getItem/vip1", "vip", {
        "interface": {"lan": {"selected": 1}}, "mode": {"carp": {"selected": 1}}, "network": "192.168.1.1/24",
        "vhid": "10", "password": "STORED-SECRET", "address": "192.168.1.1/24", "descr": "CARP LAN"})
    resp = await client.get(f"/api/hosts/{host_id}/network/item/vip/vip1")
    assert resp.status_code == 200, resp.text
    assert "STORED-SECRET" not in resp.text
    assert resp.json() == {"interface": "lan", "mode": "carp", "network": "192.168.1.1/24", "vhid": "10", "descr": "CARP LAN"}
    assert (await client.get(f"/api/hosts/{host_id}/network/item/vip/..%2Fx")).status_code == 404


# ── interface assignment / IP configuration ──────────────────────────────

def _pending(respx_mock, status="ok", base=MGMT):
    respx_mock.get(_url("/interfaces/assignment/pending", base)).mock(return_value=httpx.Response(200, json={"status": status}))


IF_ADD = {"kind": "interface", "action": "add", "fields": {"if": "vtnet1_vlan20", "descr": "GUEST", "enable": "1",
                                                           "type4": "staticv4", "ipaddr": "10.20.0.1/24"}}


@pytest.mark.asyncio
async def test_interface_add_stages_applies_and_verifies(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-i1")
    _pending(respx_mock)
    _backup(respx_mock)
    add = respx_mock.post(_url("/interfaces/assignment/addItem")).mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "opt1"}))
    reconf = respx_mock.post(_url("/interfaces/assignment/reconfigure")).mock(
        return_value=httpx.Response(200, json={"status": "ok"}))
    _item(respx_mock, "/interfaces/assignment/getItem/opt1", "interface", {
        "if": {"vtnet1_vlan20": {"selected": 1}}, "descr": "GUEST", "enable": "1",
        "type4": {"staticv4": {"selected": 1}}, "ipaddr": "10.20.0.1/24"})
    plan = (await _preview(client, host_id, **IF_ADD)).json()
    assert plan["can_execute"] is True and plan["risks"] == []
    done = await _execute(client, host_id, plan["token"], **IF_ADD)
    assert done.status_code == 200, done.text
    assert done.json()["uuid"] == "opt1" and done.json()["verified"] is True
    assert _body(add.calls.last)["interface"]["ipaddr"] == "10.20.0.1/24"
    assert reconf.called


@pytest.mark.asyncio
async def test_interface_changes_are_blocked_on_firewalls_without_the_api(client, respx_mock):
    thin = {"rows": [{"uuid": "wan", "identifier": "wan", "descr": "", "if": "vtnet0"}]}  # 26.7.1: no type4
    host_id = await _setup(client, respx_mock, "fw-i2", **{"/interfaces/assignment/searchItem": httpx.Response(200, json=thin)})
    plan = (await _preview(client, host_id, **IF_ADD)).json()
    assert plan["can_execute"] is False
    assert [b["code"] for b in plan["blocks"]] == ["interface_api_unavailable"]
    resp = await _execute(client, host_id, plan["token"], **IF_ADD)
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "NETWORK_CHANGE_BLOCKED"


@pytest.mark.asyncio
async def test_staged_changes_from_someone_else_block_a_new_change(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-i3")
    _pending(respx_mock, status="pending")
    plan = (await _preview(client, host_id, **IF_ADD)).json()
    assert [b["code"] for b in plan["blocks"]] == ["interface_changes_pending"]
    add = respx_mock.post(_url("/interfaces/assignment/addItem")).mock(return_value=httpx.Response(200, json={"result": "saved"}))
    assert (await _execute(client, host_id, plan["token"], **IF_ADD)).status_code == 409
    assert not add.called


@pytest.mark.asyncio
async def test_deleting_an_interface_lists_the_rules_that_will_vanish(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-i4")
    _pending(respx_mock)
    _item(respx_mock, "/interfaces/assignment/getItem/opt1", "interface", {
        "if": {"vtnet1_vlan20": {"selected": 1}}, "descr": "GUEST", "enable": "1"})
    respx_mock.get(_url("/firewall/filter/searchRule")).mock(return_value=httpx.Response(200, json={"rows": [
        {"uuid": "a", "description": "guest out", "interface": "opt1", "is_automatic": False},
        {"uuid": "b", "description": "multi", "interface": "lan,opt1", "is_automatic": False},
        {"uuid": "c", "description": "other", "interface": "lan", "is_automatic": False},
        {"uuid": "d", "description": "system", "interface": "opt1", "is_automatic": True}]}))
    plan = (await _preview(client, host_id, kind="interface", action="del", uuid="opt1", fields={})).json()
    risk = next(r for r in plan["risks"] if r["code"] == "interface_rules_removed")
    assert risk["params"]["count"] == 2 and "guest out" in risk["params"]["names"] and "other" not in risk["params"]["names"]
    assert plan["requires_acknowledgement"] is True


@pytest.mark.asyncio
async def test_changing_the_management_interface_needs_acknowledgement(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-i5")   # reached on 10.20.30.57 = wan
    _pending(respx_mock)
    _item(respx_mock, "/interfaces/assignment/getItem/wan", "interface", {
        "if": {"vtnet0": {"selected": 1}}, "descr": "WAN", "enable": "1", "type4": {"staticv4": {"selected": 1}},
        "ipaddr": "10.20.30.57/24"})
    plan = (await _preview(client, host_id, kind="interface", action="set", uuid="wan", fields={"ipaddr": "10.20.30.77/24"})).json()
    assert [r["code"] for r in plan["risks"]] == ["management_interface"]
    assert plan["requires_acknowledgement"] is True


@pytest.mark.asyncio
async def test_locked_interface_cannot_be_deleted_and_says_why(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-i6")
    _pending(respx_mock)
    _item(respx_mock, "/interfaces/assignment/getItem/opt1", "interface",
          {"if": {"vtnet1_vlan20": {"selected": 1}}, "descr": "GUEST", "enable": "1", "lock": "1"})
    respx_mock.get(_url("/firewall/filter/searchRule")).mock(return_value=httpx.Response(200, json={"rows": []}))
    plan = (await _preview(client, host_id, kind="interface", action="del", uuid="opt1", fields={})).json()
    assert [b["code"] for b in plan["blocks"]] == ["interface_locked"] and plan["can_execute"] is False


# ── DNS / DHCP objects ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_kea_subnet_options_are_sent_nested_and_read_back_flat(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-d1")
    _backup(respx_mock)
    add = respx_mock.post(_url("/kea/dhcpv4/addSubnet")).mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "sn1"}))
    respx_mock.post(_url("/kea/service/reconfigure")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    _item(respx_mock, "/kea/dhcpv4/getSubnet/sn1", "subnet4", {
        "subnet": "192.168.50.0/24", "pools": "192.168.50.100-192.168.50.200", "description": "lab",
        "option_data": {"routers": {"192.168.50.1": {"value": "192.168.50.1", "selected": 1}, "": {"value": "", "selected": 0}},
                        "domain_name": "lab.example"}})
    change = {"kind": "kea_subnet", "action": "add", "fields": {
        "subnet": "192.168.50.0/24", "pools": "192.168.50.100-192.168.50.200", "description": "lab",
        "option_data.routers": "192.168.50.1", "option_data.domain_name": "lab.example"}}
    plan = (await _preview(client, host_id, **change)).json()
    assert plan["risks"] == []  # DHCP data cannot cut the connection, even with an unknown management path
    done = await _execute(client, host_id, plan["token"], **change)
    assert done.status_code == 200 and done.json()["verified"] is True, done.text
    assert _body(add.calls.last) == {"subnet4": {
        "subnet": "192.168.50.0/24", "pools": "192.168.50.100-192.168.50.200", "description": "lab",
        "option_data": {"routers": "192.168.50.1", "domain_name": "lab.example"}}}


@pytest.mark.asyncio
async def test_dns_override_on_unknown_management_path_is_not_treated_as_risky(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-d2", base="https://10.20.30.98")
    _item(respx_mock, "/unbound/settings/getHostOverride/h1", "host",
          {"hostname": "nas", "domain": "lan.example", "server": "192.168.1.5", "rr": {"A": {"value": "A", "selected": 1}}},
          base="https://10.20.30.98")
    plan = (await _preview(client, host_id, kind="dns_host", action="set", uuid="h1", fields={"server": "192.168.1.6"})).json()
    assert plan["risks"] == [] and plan["can_execute"] is True
    assert plan["changes"] == [{"field": "server", "before": "192.168.1.5", "after": "192.168.1.6"}]


@pytest.mark.asyncio
async def test_template_turns_tokenizer_fields_into_comma_text(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-d3")
    _item(respx_mock, "/dnsmasq/settings/getHost", "host", {
        "host": "", "domain": "", "local": "0",
        "ip": {"": {"value": "", "selected": 1}},
        "hwaddr": {"aa:bb:cc:dd:ee:ff": {"value": "aa:bb:cc:dd:ee:ff", "selected": 1}, "11:22:33:44:55:66": {"value": "11:22:33:44:55:66", "selected": 1}}})
    body = (await client.get(f"/api/hosts/{host_id}/network/template/dhcp_host")).json()
    assert body["ip"]["value"] == "" and body["hwaddr"]["value"] == "aa:bb:cc:dd:ee:ff,11:22:33:44:55:66"


@pytest.mark.asyncio
async def test_unlisted_secret_fields_never_reach_the_audit_log(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-d4")
    _backup(respx_mock)
    _item(respx_mock, "/kea/dhcpv4/getSubnet/sn9", "subnet4", {
        "subnet": "192.168.9.0/24", "description": "old", "ddns_domain_key_secret": "KEA-DDNS-SECRET"})
    respx_mock.post(_url("/kea/dhcpv4/setSubnet/sn9")).mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(_url("/kea/service/reconfigure")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    change = {"kind": "kea_subnet", "action": "set", "uuid": "sn9", "fields": {"description": "new"}}
    plan = (await _preview(client, host_id, **change)).json()
    assert (await _execute(client, host_id, plan["token"], **change)).status_code == 200
    assert "KEA-DDNS-SECRET" not in json.dumps([e.details for e in await _audit(host_id)])


@pytest.mark.asyncio
async def test_vip_edit_always_repeats_the_address(client, respx_mock):
    # OPNsense blanks a VIP's address when an update leaves out "network" (seen live on 26.7.6).
    host_id = await _setup(client, respx_mock, "fw-v1")
    _item(respx_mock, "/interfaces/vip_settings/getItem/vip1", "vip", {
        "interface": {"lan": {"selected": 1}}, "mode": {"carp": {"selected": 1}}, "network": "192.168.1.50/24",
        "vhid": "11", "advskew": "0", "descr": "x"})
    _backup(respx_mock)
    put = respx_mock.post(_url("/interfaces/vip_settings/setItem/vip1")).mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(_url("/interfaces/vip_settings/reconfigure")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    change = {"kind": "vip", "action": "set", "uuid": "vip1", "fields": {"advskew": "20"}}
    plan = (await _preview(client, host_id, **change)).json()
    done = await _execute(client, host_id, plan["token"], **change)
    assert done.status_code == 200, done.text
    assert _body(put.calls.last)["vip"] == {"advskew": "20", "network": "192.168.1.50/24"}


@pytest.mark.asyncio
async def test_verification_catches_a_field_that_changed_without_being_asked(client, respx_mock):
    # The firewall dropped an untouched field while saving: that must not be reported as a clean success.
    host_id = await _setup(client, respx_mock, "fw-v2")
    state = iter([
        {"descr": "old", "network": "172.16.0.0/16", "gateway": {"WAN_GW": {"selected": 1}}, "enabled": "1"},   # preview (before)
        {"descr": "old", "network": "172.16.0.0/16", "gateway": {"WAN_GW": {"selected": 1}}, "enabled": "1"},   # execute re-preview
        {"descr": "new", "network": "", "gateway": {"WAN_GW": {"selected": 1}}, "enabled": "1"},                # read-back: network wiped
    ])
    respx_mock.get(_url("/routes/routes/getroute/r5")).mock(side_effect=lambda req: httpx.Response(200, json={"route": next(state)}))
    _backup(respx_mock)
    respx_mock.post(_url("/routes/routes/setroute/r5")).mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(_url("/routes/routes/reconfigure")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    change = {"kind": "route", "action": "set", "uuid": "r5", "fields": {"descr": "new"}}
    plan = (await _preview(client, host_id, **change)).json()
    done = (await _execute(client, host_id, plan["token"], **change)).json()
    assert done["verified"] is False and done["mismatches"] == ["network"]


# ── generic object kinds ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_objects_list_returns_only_the_display_columns(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-o1")
    respx_mock.get(_url("/syslog/settings/searchDestinations")).mock(return_value=httpx.Response(200, json={"rows": [
        {"uuid": "d1", "enabled": "1", "transport": "udp4", "hostname": "192.0.2.10", "port": "514", "program": "", "level": "info",
         "facility": "daemon", "certificate": "SECRET-CERT-REF", "description": "central"}]}))
    body = (await client.get(f"/api/hosts/{host_id}/network/objects/syslog_destination")).json()
    assert body["available"] is True and body["rows"][0]["hostname"] == "192.0.2.10"
    assert "certificate" not in body["rows"][0] and "facility" not in body["rows"][0]
    assert (await client.get(f"/api/hosts/{host_id}/network/objects/vlan")).status_code == 404  # typed kinds have their own lists


@pytest.mark.asyncio
async def test_objects_list_reports_a_missing_api_instead_of_failing(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-o2")
    respx_mock.get(_url("/interfaces/gre_settings/searchItem")).mock(return_value=httpx.Response(404, json={"errorMessage": "Endpoint not found"}))
    body = (await client.get(f"/api/hosts/{host_id}/network/objects/gre")).json()
    assert body == {"available": False, "columns": body["columns"], "rows": []}


@pytest.mark.asyncio
async def test_tunables_always_need_acknowledgement_and_defaults_cannot_be_edited(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-o3")
    add = (await _preview(client, host_id, kind="tunable", action="add", fields={"tunable": "vfs.read_max", "value": "128"})).json()
    assert [r["code"] for r in add["risks"]] == ["tunable_change"] and add["requires_acknowledgement"] is True
    _item(respx_mock, "/core/tunables/getItem/0add3c58db981d0d24d5d4ab322e3695", "sysctl",
          {"tunable": "net.inet.tcp.drop_synfin", "value": "1", "descr": "built in"})
    default = (await _preview(client, host_id, kind="tunable", action="set", uuid="0add3c58db981d0d24d5d4ab322e3695", fields={"value": "0"})).json()
    assert [b["code"] for b in default["blocks"]] == ["tunable_default_row"] and default["can_execute"] is False


@pytest.mark.asyncio
async def test_lagg_or_bridge_containing_the_management_device_needs_acknowledgement(client, respx_mock):
    host_id = await _setup(client, respx_mock, "fw-o4")   # reached on vtnet0
    mine = (await _preview(client, host_id, kind="lagg", action="add", fields={"members": "vtnet0,vtnet2", "proto": "failover"})).json()
    assert [r["code"] for r in mine["risks"]] == ["management_member"]
    other = (await _preview(client, host_id, kind="lagg", action="add", fields={"members": "vtnet2,vtnet3", "proto": "failover"})).json()
    assert other["risks"] == []
