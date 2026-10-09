"""Read-only network inventory of one firewall: interfaces, VLANs, virtual
IPs (CARP), gateways and static routes, straight from the firewall.

Also works out which interface the firewall answers OPNexus on. Every later
write feature in this area (VLAN/IP/gateway changes) must refuse or escalate
changes to that interface — an apply there can cut the very connection OPNexus
would need to undo it.

Shapes verified live on OPNsense 26.7.1 and 26.7.6 (see opnsense_client).
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from app.models.host import OpnsenseHost
from app.schemas.network import (
    HostNetwork,
    NetAddress,
    NetGateway,
    NetDhcpHost,
    NetDnsHost,
    NetInterface,
    NetKeaReservation,
    NetKeaSubnet,
    NetRoute,
    NetVip,
    NetVlan,
)
from app.services import opnsense_client
from app.services.opnsense_client import OpnsenseAPIError


def _rows(data) -> list[dict]:
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [r for r in rows if isinstance(r, dict)]


def _flag(value) -> bool:
    return value in ("1", 1, True)


def _text(value) -> str:
    return "" if value is None else str(value)


def _opt(value) -> str | None:
    """OPNsense is inconsistent about number vs. string (a gateway priority came back as 254, not "254")."""
    return None if value in (None, "") else str(value)


async def management_addresses(host: OpnsenseHost) -> set:
    """IP addresses OPNexus uses to reach this firewall (literal or resolved)."""
    hostname = urlparse(host.base_url).hostname
    if not hostname:
        return set()
    try:
        return {ipaddress.ip_address(hostname)}
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return set()
    found = set()
    for info in infos:
        try:
            found.add(ipaddress.ip_address(info[4][0].split("%")[0]))
        except ValueError:
            continue
    return found


def _addresses(row: dict) -> list[NetAddress]:
    result = []
    for key in ("ipv4", "ipv6"):
        for entry in row.get(key) or []:
            if isinstance(entry, dict) and entry.get("ipaddr"):
                result.append(NetAddress(
                    address=str(entry["ipaddr"]),
                    vhid=_opt(entry.get("vhid")),
                    carp_status=_opt(entry.get("status")),
                ))
    return result


def _address_ip(address: str):
    try:
        return ipaddress.ip_interface(address).ip
    except ValueError:
        return None


def _interface(row: dict, assignments: dict[str, dict], management: set) -> NetInterface:
    identifier = row.get("identifier") or None
    assigned = assignments.get(identifier or "") or {}
    addresses = _addresses(row)
    vlan = row.get("vlan") or {}
    return NetInterface(
        device=_text(row.get("device")),
        identifier=identifier,
        description=_text(row.get("description")),
        status=_text(row.get("status")),
        enabled=_flag(row.get("enabled", True)) if "enabled" in row else True,
        is_physical=bool(row.get("is_physical")),
        mtu=_opt(row.get("mtu")),
        mac=_opt(row.get("macaddr")),
        media=_opt(row.get("media")),
        vlan_tag=_opt((vlan.get("tag") if isinstance(vlan, dict) else None) or row.get("vlan_tag")),
        vlan_parent=_opt(vlan.get("parent")) if isinstance(vlan, dict) else None,
        ipv4_type=assigned.get("%type4") or None,
        ipv6_type=assigned.get("%type6") or None,
        addresses=addresses,
        pending_action=assigned.get("pending_action") or None,
        assigned=bool(identifier and identifier in assignments),
        is_management=any(_address_ip(a.address) in management for a in addresses if not a.vhid),
    )


def _vlan(row: dict) -> NetVlan:
    return NetVlan(
        uuid=_text(row.get("uuid")),
        parent=_text(row.get("if")),
        tag=_text(row.get("tag")),
        pcp=_opt(row.get("pcp")),
        description=_text(row.get("descr")),
        device=(row.get("vlanif") or "").split(" ")[0] or None,
    )


def _vip(row: dict) -> NetVip:
    # Deliberately picks fields by name: the row also carries the CARP password.
    return NetVip(
        uuid=_text(row.get("uuid")),
        interface=_text(row.get("interface")),
        interface_label=_text(row.get("%interface") or row.get("interface")),
        mode=_text(row.get("mode")),
        address=_text(row.get("address") or row.get("subnet")),
        vhid=_opt(row.get("vhid")),
        advbase=_opt(row.get("advbase")),
        advskew=_opt(row.get("advskew")),
        description=_text(row.get("descr")),
    )


def _gateway(row: dict) -> NetGateway:
    return NetGateway(
        uuid=_text(row.get("uuid")),
        name=_text(row.get("name")),
        description=_text(row.get("descr")),
        interface=_text(row.get("interface")),
        interface_label=_text(row.get("interface_descr") or row.get("interface")),
        ip_protocol=_text(row.get("ipprotocol")),
        address=_text(row.get("gateway")),
        is_default=bool(row.get("defaultgw")),
        disabled=bool(row.get("disabled")),
        priority=_opt(row.get("priority")),
        status=_text(row.get("status")),
    )


def _route(row: dict) -> NetRoute:
    return NetRoute(
        uuid=_text(row.get("uuid")),
        network=_text(row.get("network")),
        gateway=_text(row.get("gateway")),
        description=_text(row.get("descr")),
        # list rows carry "enabled" ("1"/"0"); older shapes a "disabled" flag
        disabled=_flag(row.get("disabled")) or row.get("enabled") in ("0", 0, False),
    )


def _dns_host(row: dict) -> NetDnsHost:
    return NetDnsHost(
        uuid=_text(row.get("uuid")), hostname=_text(row.get("hostname")), domain=_text(row.get("domain")),
        record_type=_text(row.get("rr")), server=_text(row.get("server")), enabled=_flag(row.get("enabled")),
        description=_text(row.get("description")),
    )


def _dhcp_host(row: dict) -> NetDhcpHost:
    return NetDhcpHost(
        uuid=_text(row.get("uuid")), host=_text(row.get("host")), domain=_text(row.get("domain")),
        ip=_text(row.get("ip")), hwaddr=_text(row.get("hwaddr")), lease_time=_text(row.get("lease_time")),
        description=_text(row.get("descr")),
    )


def _kea_subnet(row: dict) -> NetKeaSubnet:
    # Picked by name on purpose: the row also carries the DDNS key secret.
    return NetKeaSubnet(
        uuid=_text(row.get("uuid")), subnet=_text(row.get("subnet")), pools=_text(row.get("pools")),
        routers=_text(row.get("option_data.routers")), dns_servers=_text(row.get("option_data.domain_name_servers")),
        description=_text(row.get("description")),
    )


def _kea_reservation(row: dict) -> NetKeaReservation:
    return NetKeaReservation(
        uuid=_text(row.get("uuid")), subnet=_text(row.get("%subnet") or row.get("subnet")),
        ip_address=_text(row.get("ip_address")), hw_address=_text(row.get("hw_address")),
        hostname=_text(row.get("hostname")), description=_text(row.get("description")),
    )


async def _optional(name: str, call, unavailable: list[str]) -> list[dict]:
    try:
        return _rows(await call)
    except OpnsenseAPIError:
        unavailable.append(name)
        return []


async def get_host_network(host: OpnsenseHost) -> HostNetwork:
    """Raises OpnsenseAPIError when the interface list itself is unavailable
    (host down); every other section degrades to `unavailable` instead."""
    info_rows = _rows(await opnsense_client.get_interfaces_info(host))
    unavailable: list[str] = []
    (assignment_rows, vlan_rows, vip_rows, gateway_rows, route_rows,
     dns_rows, dhcp_rows, kea_subnet_rows, kea_reservation_rows) = await asyncio.gather(
        _optional("assignments", opnsense_client.search_interface_assignments(host), unavailable),
        _optional("vlans", opnsense_client.search_vlans(host), unavailable),
        _optional("vips", opnsense_client.search_vips(host), unavailable),
        _optional("gateways", opnsense_client.search_gateways(host), unavailable),
        _optional("routes", opnsense_client.search_static_routes(host), unavailable),
        _optional("dns_hosts", opnsense_client.search_dns_hosts(host), unavailable),
        _optional("dhcp_hosts", opnsense_client.search_dhcp_hosts(host), unavailable),
        _optional("kea_subnets", opnsense_client.search_kea_subnets(host), unavailable),
        _optional("kea_reservations", opnsense_client.search_kea_reservations(host), unavailable),
    )
    management = await management_addresses(host)
    assignments = {_text(r.get("identifier")): r for r in assignment_rows if r.get("identifier")}
    interfaces = [_interface(r, assignments, management) for r in info_rows]
    management_device = next((i.device for i in interfaces if i.is_management), None)
    return HostNetwork(
        host_id=str(host.id),
        host_name=host.name,
        interfaces=interfaces,
        vlans=[_vlan(r) for r in vlan_rows],
        vips=[_vip(r) for r in vip_rows],
        gateways=[_gateway(r) for r in gateway_rows],
        routes=[_route(r) for r in route_rows],
        dns_hosts=[_dns_host(r) for r in dns_rows if not r.get("isAlias")],
        dhcp_hosts=[_dhcp_host(r) for r in dhcp_rows],
        kea_subnets=[_kea_subnet(r) for r in kea_subnet_rows],
        kea_reservations=[_kea_reservation(r) for r in kea_reservation_rows],
        management_device=management_device,
        can_configure_interfaces=any("type4" in r for r in assignment_rows),
        unavailable=sorted(unavailable),
    )
