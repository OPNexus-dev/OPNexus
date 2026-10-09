"""Read-only network inventory. Response shapes copied from real OPNsense
26.7.x test firewalls (CARP password replaced)."""
import httpx
import pytest

BASE = "https://fw-net.test"

INTERFACES_INFO = {"total": 3, "rowCount": 3, "current": 1, "rows": [
    {"device": "vtnet0", "identifier": "wan", "description": "WAN", "status": "up", "is_physical": True,
     "mtu": "1500", "macaddr": "bc:24:11:25:7e:b8", "media": "10Gbase-T <full-duplex>",
     "ipv4": [{"ipaddr": "10.20.30.57/24"}], "ipv6": []},
    {"device": "vtnet1_vlan10", "identifier": "lan", "description": "LAN", "status": "up", "is_physical": False,
     "mtu": "1500", "ipv4": [{"ipaddr": "192.168.1.2/24"},
                             {"ipaddr": "192.168.1.1/24", "vhid": "10", "status": "MASTER"}],
     "vlan": {"tag": "10", "proto": "802.1q", "pcp": "0", "parent": "vtnet1"}},
    {"device": "vtnet1", "identifier": "", "description": "Unassigned Interface", "status": "up",
     "is_physical": True, "ipv4": []},
]}
ASSIGNMENTS = {"rows": [
    {"uuid": "wan", "identifier": "wan", "pending_action": "", "type4": "staticv4", "%type4": "Static IPv4", "%type6": "DHCPv6"},
    {"uuid": "lan", "identifier": "lan", "pending_action": "", "type4": "staticv4", "%type4": "Static IPv4", "%type6": "Identity association"},
]}
VLANS = {"rows": [{"uuid": "v1", "if": "vtnet1", "tag": "10", "pcp": "0", "descr": "", "vlanif": "vtnet1_vlan10 [LAN]"}]}
VIPS = {"rows": [{"uuid": "vip1", "interface": "lan", "%interface": "LAN", "mode": "carp", "address": "192.168.1.1/24",
                  "vhid": "10", "advbase": "1", "advskew": "0", "descr": "CARP LAN", "password": "SUPER-SECRET-CARP"}]}
GATEWAYS = {"rows": [{"uuid": "g1", "name": "WAN_GW", "descr": "", "interface": "wan", "interface_descr": "WAN",
                      "ipprotocol": "inet", "gateway": "10.20.30.1", "defaultgw": True, "disabled": False,
                      "priority": "255", "status": "Online"}]}
DNS_HOSTS = {"rows": [
    {"uuid": "d1", "enabled": "1", "hostname": "nas", "domain": "lan.example", "rr": "A", "server": "192.168.1.5", "description": "NAS", "isAlias": False},
    {"uuid": "d1-a", "enabled": "1", "hostname": "files", "domain": "lan.example", "rr": "A", "server": "192.168.1.5", "isAlias": True}]}
DHCP_HOSTS = {"rows": [{"uuid": "h1", "host": "printer", "domain": "lan.example", "ip": "192.168.1.30", "hwaddr": "02:00:00:aa:bb:cc",
                         "lease_time": "", "descr": "Printer"}]}
KEA_SUBNETS = {"rows": [{"uuid": "s1", "subnet": "192.168.77.0/24", "pools": "192.168.77.100-192.168.77.150",
                          "option_data.routers": "192.168.77.1", "option_data.domain_name_servers": "",
                          "description": "lab", "ddns_domain_key_secret": "KEA-DDNS-SECRET"}]}
KEA_RESERVATIONS = {"rows": [{"uuid": "k1", "subnet": "s1", "%subnet": "192.168.77.0/24", "ip_address": "192.168.77.120",
                               "hw_address": "02:00:00:aa:bb:dd", "hostname": "kearsv", "description": "Reservation"}]}
ROUTES = {"rows": [{"uuid": "r1", "network": "10.9.0.0/16", "gateway": "WAN_GW", "descr": "lab", "disabled": "0"}]}


async def _host(client, respx_mock, base=BASE, name="fw-net"):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post("/api/hosts", json={"name": name, "base_url": base, "api_key": "k", "api_secret": "s"})
    return resp.json()["id"]


def _mock(respx_mock, base=BASE, info=INTERFACES_INFO, **overrides):
    routes = {
        "/interfaces/overview/interfacesInfo": info,
        "/interfaces/assignment/searchItem": ASSIGNMENTS,
        "/interfaces/vlan_settings/searchItem": VLANS,
        "/interfaces/vip_settings/searchItem": VIPS,
        "/routing/settings/searchGateway": GATEWAYS,
        "/routes/routes/searchroute": ROUTES,
        "/unbound/settings/searchHostOverride": DNS_HOSTS,
        "/dnsmasq/settings/searchHost": DHCP_HOSTS,
        "/kea/dhcpv4/searchSubnet": KEA_SUBNETS,
        "/kea/dhcpv4/searchReservation": KEA_RESERVATIONS,
    }
    for path, body in routes.items():
        reply = overrides.get(path, httpx.Response(200, json=body))
        respx_mock.get(f"{base}/api{path}").mock(return_value=reply)


@pytest.mark.asyncio
async def test_network_inventory_parses_real_shapes(client, respx_mock):
    host_id = await _host(client, respx_mock, base="https://10.20.30.57")
    _mock(respx_mock, base="https://10.20.30.57")
    resp = await client.get(f"/api/hosts/{host_id}/network")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    by_device = {i["device"]: i for i in data["interfaces"]}
    assert by_device["vtnet0"]["identifier"] == "wan"
    assert by_device["vtnet0"]["ipv4_type"] == "Static IPv4"
    assert by_device["vtnet1"]["identifier"] is None
    assert by_device["vtnet0"]["assigned"] is True and by_device["vtnet1"]["assigned"] is False
    lan = by_device["vtnet1_vlan10"]
    assert lan["vlan_tag"] == "10" and lan["vlan_parent"] == "vtnet1"
    assert [a["carp_status"] for a in lan["addresses"]] == [None, "MASTER"]
    assert data["vlans"][0]["device"] == "vtnet1_vlan10"
    assert data["gateways"][0]["is_default"] is True
    assert data["routes"][0]["network"] == "10.9.0.0/16"
    assert data["unavailable"] == []
    assert data["can_configure_interfaces"] is True


@pytest.mark.asyncio
async def test_network_marks_management_interface(client, respx_mock):
    host_id = await _host(client, respx_mock, base="https://10.20.30.57")
    _mock(respx_mock, base="https://10.20.30.57")
    data = (await client.get(f"/api/hosts/{host_id}/network")).json()
    assert data["management_device"] == "vtnet0"
    assert [i["device"] for i in data["interfaces"] if i["is_management"]] == ["vtnet0"]


@pytest.mark.asyncio
async def test_network_carp_address_is_not_management(client, respx_mock):
    # Reached over the shared CARP address: that is not one interface's own IP, so no guess.
    host_id = await _host(client, respx_mock, base="https://192.168.1.1", name="fw-carp")
    _mock(respx_mock, base="https://192.168.1.1")
    data = (await client.get(f"/api/hosts/{host_id}/network")).json()
    assert data["management_device"] is None


@pytest.mark.asyncio
async def test_network_never_returns_carp_password(client, respx_mock):
    host_id = await _host(client, respx_mock, base="https://10.20.30.58", name="fw-pw")
    _mock(respx_mock, base="https://10.20.30.58")
    resp = await client.get(f"/api/hosts/{host_id}/network")
    assert "SUPER-SECRET-CARP" not in resp.text
    assert "password" not in resp.json()["vips"][0]


@pytest.mark.asyncio
async def test_network_missing_section_is_reported_not_hidden(client, respx_mock):
    host_id = await _host(client, respx_mock, base="https://10.20.30.59", name="fw-old")
    _mock(respx_mock, base="https://10.20.30.59", **{
        "/interfaces/assignment/searchItem": httpx.Response(404, json={"errorMessage": "Endpoint not found"}),
    })
    resp = await client.get(f"/api/hosts/{host_id}/network")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["unavailable"] == ["assignments"]
    assert len(data["interfaces"]) == 3


@pytest.mark.asyncio
async def test_network_host_unreachable_returns_502(client, respx_mock):
    host_id = await _host(client, respx_mock, base="https://10.20.30.60", name="fw-down")
    respx_mock.get(url__regex=r".*/interfaces/overview/interfacesInfo").mock(side_effect=httpx.ConnectError("refused"))
    assert (await client.get(f"/api/hosts/{host_id}/network")).status_code == 502


@pytest.mark.asyncio
async def test_network_unknown_host_404(client):
    resp = await client.get("/api/hosts/00000000-0000-0000-0000-000000000000/network")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_network_tolerates_numbers_where_strings_were_expected(client, respx_mock):
    # Seen live on 26.7.x: a gateway's priority arrived as the integer 254.
    host_id = await _host(client, respx_mock, base="https://10.20.30.61", name="fw-int")
    gateways = {"rows": [{**GATEWAYS["rows"][0], "priority": 254}]}
    _mock(respx_mock, base="https://10.20.30.61", **{
        "/routing/settings/searchGateway": httpx.Response(200, json=gateways),
    })
    resp = await client.get(f"/api/hosts/{host_id}/network")
    assert resp.status_code == 200, resp.text
    assert resp.json()["gateways"][0]["priority"] == "254"


@pytest.mark.asyncio
async def test_network_older_26_7_assignment_list_cannot_configure(client, respx_mock):
    # OPNsense 26.7.1: the assignment list only has identifier/descr/if, no type4/ipaddr.
    host_id = await _host(client, respx_mock, base="https://10.20.30.62", name="fw-2671")
    thin = {"rows": [{"uuid": "wan", "identifier": "wan", "descr": "", "if": "vtnet0"}]}
    _mock(respx_mock, base="https://10.20.30.62", **{
        "/interfaces/assignment/searchItem": httpx.Response(200, json=thin),
    })
    data = (await client.get(f"/api/hosts/{host_id}/network")).json()
    assert data["can_configure_interfaces"] is False
    assert data["unavailable"] == []


@pytest.mark.asyncio
async def test_network_lists_dns_and_dhcp_objects_without_secrets(client, respx_mock):
    host_id = await _host(client, respx_mock, base="https://10.20.30.63", name="fw-dns")
    _mock(respx_mock, base="https://10.20.30.63")
    resp = await client.get(f"/api/hosts/{host_id}/network")
    assert "KEA-DDNS-SECRET" not in resp.text
    data = resp.json()
    assert [(d["hostname"], d["server"]) for d in data["dns_hosts"]] == [("nas", "192.168.1.5")]  # alias children are not rows of their own
    assert data["dhcp_hosts"][0]["hwaddr"] == "02:00:00:aa:bb:cc"
    assert (data["kea_subnets"][0]["subnet"], data["kea_subnets"][0]["routers"]) == ("192.168.77.0/24", "192.168.77.1")
    assert data["kea_reservations"][0]["subnet"] == "192.168.77.0/24"
    assert data["unavailable"] == []
