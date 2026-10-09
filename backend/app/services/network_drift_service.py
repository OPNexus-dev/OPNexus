"""Compares the network configuration of the nodes of an HA cluster.

Interfaces, VLANs, virtual IPs, gateways, routes, DNS and DHCP objects should
be the same on both nodes of a pair; a VLAN or gateway that exists on one node
only is exactly the kind of difference that breaks a failover months later.

Per-node values are deliberately NOT compared: interface addresses, CARP
advskew/password and the active default gateway legitimately differ between
the nodes. What is compared is the identity of each object (its key) and the
settings that must agree (its signature). A category is skipped when any
reachable node cannot deliver it (older OPNsense), instead of reporting that
as a difference.
"""
from app.models.host import OpnsenseHost
from app.schemas.network import HostNetwork
from app.services import network_service
from app.services.opnsense_client import OpnsenseAPIError

CATEGORIES = ["interfaces", "vlans", "vips", "gateways", "routes", "dns_hosts", "dhcp_hosts", "kea_subnets", "kea_reservations"]


def signatures(net: HostNetwork) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {c: {} for c in CATEGORIES}
    for i in net.interfaces:
        if i.assigned and i.identifier:
            out["interfaces"][i.identifier] = f"{i.description}|{i.vlan_tag or ''}|{i.ipv4_type or ''}"
    for v in net.vlans:
        out["vlans"][f"{v.tag}@{v.parent}"] = v.description
    for v in net.vips:
        if v.mode == "carp":
            out["vips"][f"carp:{v.vhid}"] = f"{v.address}|{v.advbase or ''}|{v.description}"
        else:
            out["vips"][f"{v.mode}:{v.address}"] = v.description
    for g in net.gateways:
        out["gateways"][g.name] = f"{g.interface}|{g.ip_protocol}|{g.address}|{g.disabled}"
    for r in net.routes:
        out["routes"][r.network] = f"{r.gateway}|{r.disabled}"
    for d in net.dns_hosts:
        out["dns_hosts"][f"{d.hostname}.{d.domain}|{d.record_type}"] = f"{d.server}|{d.enabled}"
    for h in net.dhcp_hosts:
        out["dhcp_hosts"][f"{h.host}.{h.domain}"] = f"{h.ip}|{h.hwaddr}"
    for s in net.kea_subnets:
        out["kea_subnets"][s.subnet] = f"{s.pools}|{s.routers}|{s.dns_servers}"
    for k in net.kea_reservations:
        out["kea_reservations"][f"{k.ip_address}|{k.hw_address}"] = k.hostname
    return out


# Which HostNetwork.unavailable entry makes a category incomparable.
_UNAVAILABLE_KEY = {"interfaces": "assignments"}


def compare(per_host: dict[str, HostNetwork]) -> list[dict]:
    """per_host: host name -> its network. Returns one row per object that is missing on a node or differs."""
    names = sorted(per_host)
    sigs = {n: signatures(per_host[n]) for n in names}
    rows: list[dict] = []
    for category in CATEGORIES:
        skip_key = _UNAVAILABLE_KEY.get(category, category)
        if any(skip_key in per_host[n].unavailable for n in names):
            continue
        keys = sorted({k for n in names for k in sigs[n][category]})
        for key in keys:
            present = {n: sigs[n][category][key] for n in names if key in sigs[n][category]}
            if len(present) < len(names):
                rows.append({"category": category, "key": key, "status": "missing",
                             "present_on": sorted(present), "missing_from": sorted(set(names) - set(present)),
                             "signatures": present})
            elif len(set(present.values())) > 1:
                rows.append({"category": category, "key": key, "status": "different",
                             "present_on": names, "missing_from": [], "signatures": present})
    return rows


async def compute_cluster(hosts: list[OpnsenseHost]) -> tuple[list[dict], list[str], list[str]]:
    """Returns (differences, compared host names, unreachable host names)."""
    per_host: dict[str, HostNetwork] = {}
    unreachable: list[str] = []
    for host in hosts:
        try:
            per_host[host.name] = await network_service.get_host_network(host)
        except OpnsenseAPIError:
            unreachable.append(host.name)
    if len(per_host) < 2:
        return [], sorted(per_host), sorted(unreachable)
    return compare(per_host), sorted(per_host), sorted(unreachable)
