"""Network configuration differences between the nodes of an HA cluster."""
import httpx
import pytest

from app.database import SessionLocal
from app.schemas.network import HostNetwork
from app.services import network_drift_service, notification_checks
from tests.test_network import DHCP_HOSTS, DNS_HOSTS, GATEWAYS, INTERFACES_INFO, KEA_RESERVATIONS, KEA_SUBNETS, ROUTES, VIPS, VLANS, ASSIGNMENTS

ENDPOINTS = {
    "/interfaces/overview/interfacesInfo": INTERFACES_INFO, "/interfaces/assignment/searchItem": ASSIGNMENTS,
    "/interfaces/vlan_settings/searchItem": VLANS, "/interfaces/vip_settings/searchItem": VIPS,
    "/routing/settings/searchGateway": GATEWAYS, "/routes/routes/searchroute": ROUTES,
    "/unbound/settings/searchHostOverride": DNS_HOSTS, "/dnsmasq/settings/searchHost": DHCP_HOSTS,
    "/kea/dhcpv4/searchSubnet": KEA_SUBNETS, "/kea/dhcpv4/searchReservation": KEA_RESERVATIONS,
}


async def _host(client, name, base, cluster="ha"):
    r = await client.post("/api/hosts", json={"name": name, "base_url": base, "api_key": "k", "api_secret": "s",
                                              "verify_tls": False, "cluster_name": cluster})
    assert r.status_code == 201, r.text


def _serve(respx_mock, base, **replace):
    for path, body in ENDPOINTS.items():
        respx_mock.get(f"{base}/api{path}").mock(return_value=httpx.Response(200, json=replace.get(path, body)))


def _net(**replace) -> HostNetwork:
    data = {
        "host_id": "x", "host_name": "x", "interfaces": [], "vlans": [], "vips": [], "gateways": [], "routes": [],
        "management_device": None, "can_configure_interfaces": True, "unavailable": [],
    }
    data.update(replace)
    return HostNetwork(**data)


def test_identical_configuration_has_no_differences_even_with_per_node_values():
    vip_a = {"uuid": "1", "interface": "lan", "interface_label": "LAN", "mode": "carp", "address": "192.168.1.1/24",
             "vhid": "10", "advbase": "1", "advskew": "0", "description": "x"}
    vip_b = {**vip_a, "uuid": "2", "advskew": "100"}   # advskew legitimately differs between master and backup
    a, b = _net(vips=[vip_a]), _net(vips=[vip_b])
    assert network_drift_service.compare({"a": a, "b": b}) == []


def test_missing_and_different_objects_are_reported_with_who_has_what():
    vlan = {"uuid": "1", "parent": "vtnet1", "tag": "10", "pcp": "0", "description": "LAN", "device": "vtnet1_vlan10"}
    gw_a = {"uuid": "g", "name": "WAN_GW", "description": "", "interface": "wan", "interface_label": "WAN", "ip_protocol": "inet",
            "address": "10.0.0.1", "is_default": True, "disabled": False, "priority": "255", "status": "Online"}
    a = _net(vlans=[vlan, {**vlan, "uuid": "2", "tag": "20"}], gateways=[gw_a])
    b = _net(vlans=[vlan], gateways=[{**gw_a, "address": "10.0.0.9"}])
    rows = {(r["category"], r["key"]): r for r in network_drift_service.compare({"a": a, "b": b})}
    assert rows[("vlans", "20@vtnet1")]["status"] == "missing" and rows[("vlans", "20@vtnet1")]["missing_from"] == ["b"]
    assert rows[("gateways", "WAN_GW")]["status"] == "different"
    assert rows[("gateways", "WAN_GW")]["signatures"]["a"] != rows[("gateways", "WAN_GW")]["signatures"]["b"]
    assert ("vlans", "10@vtnet1") not in rows


def test_a_category_one_node_cannot_deliver_is_not_reported_as_a_difference():
    vlan = {"uuid": "1", "parent": "vtnet1", "tag": "10", "pcp": "0", "description": "", "device": "d"}
    a, b = _net(vlans=[vlan]), _net(vlans=[], unavailable=["vlans"])
    assert network_drift_service.compare({"a": a, "b": b}) == []


@pytest.mark.asyncio
async def test_endpoint_and_alert_report_a_route_missing_on_one_node(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    await _host(client, "fw-da", "https://10.20.30.91")
    await _host(client, "fw-db", "https://10.20.30.92")
    _serve(respx_mock, "https://10.20.30.91")
    _serve(respx_mock, "https://10.20.30.92", **{"/routes/routes/searchroute": {"rows": []}})
    body = (await client.get("/api/network/drift")).json()
    cluster = next(c for c in body["clusters"] if c["name"] == "ha")
    assert cluster["unreachable"] == []
    diff = [d for d in cluster["differences"] if d["category"] == "routes"]
    assert [(d["key"], d["status"], d["missing_from"]) for d in diff] == [("10.9.0.0/16", "missing", ["fw-db"])]

    async with SessionLocal() as db:
        problems = await notification_checks.check_network_drift(db)
    mine = [v for k, v in problems.items() if k.startswith("ha:routes:")]
    assert mine and mine[0]["missing_from"] == ["fw-db"] and "Route" in mine[0]["object"]


@pytest.mark.asyncio
async def test_an_unreachable_node_keeps_open_alerts_instead_of_resolving_them(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    await _host(client, "fw-dc", "https://10.20.30.93", cluster="ha2")
    await _host(client, "fw-dd", "https://10.20.30.94", cluster="ha2")
    _serve(respx_mock, "https://10.20.30.93")
    respx_mock.get(url__regex=r"https://10\.20\.30\.94/.*").mock(side_effect=httpx.ConnectError("down"))
    async with SessionLocal() as db:
        problems = await notification_checks.check_network_drift(db)
    assert "ha2:" in problems.preserve_prefixes
