from typing import Any, Literal

from pydantic import BaseModel, Field


class NetAddress(BaseModel):
    address: str  # CIDR, e.g. 192.168.1.2/24
    # Set for CARP addresses only
    vhid: str | None = None
    carp_status: str | None = None


class NetInterface(BaseModel):
    device: str
    identifier: str | None  # OPNsense's own name (wan, lan, opt3) — None when unassigned
    description: str
    status: str
    enabled: bool
    is_physical: bool
    mtu: str | None
    mac: str | None
    media: str | None
    vlan_tag: str | None
    vlan_parent: str | None
    ipv4_type: str | None  # "Static IPv4" / "DHCP" ... (assigned interfaces only)
    ipv6_type: str | None
    addresses: list[NetAddress]
    pending_action: str | None = None
    # Listed in the firewall's interface assignment (wan, lan, optN) — only those can be edited or removed.
    assigned: bool = False
    # This is the interface the firewall answers OPNexus on. Changing it can cut the connection.
    is_management: bool = False


class NetVlan(BaseModel):
    uuid: str
    parent: str
    tag: str
    pcp: str | None
    description: str
    device: str | None


class NetVip(BaseModel):
    """No CARP password here, by design: OPNsense returns it in plain text."""
    uuid: str
    interface: str
    interface_label: str
    mode: str
    address: str
    vhid: str | None
    advbase: str | None
    advskew: str | None
    description: str


class NetGateway(BaseModel):
    uuid: str
    name: str
    description: str
    interface: str
    interface_label: str
    ip_protocol: str
    address: str
    is_default: bool
    disabled: bool
    priority: str | None
    status: str


class NetRoute(BaseModel):
    uuid: str
    network: str
    gateway: str
    description: str
    disabled: bool


class NetDnsHost(BaseModel):
    uuid: str
    hostname: str
    domain: str
    record_type: str
    server: str
    enabled: bool
    description: str


class NetDhcpHost(BaseModel):
    uuid: str
    host: str
    domain: str
    ip: str
    hwaddr: str
    lease_time: str
    description: str


class NetKeaSubnet(BaseModel):
    uuid: str
    subnet: str
    pools: str
    routers: str
    dns_servers: str
    description: str


class NetKeaReservation(BaseModel):
    uuid: str
    subnet: str
    ip_address: str
    hw_address: str
    hostname: str
    description: str


class HostNetwork(BaseModel):
    host_id: str
    host_name: str
    interfaces: list[NetInterface]
    vlans: list[NetVlan]
    vips: list[NetVip]
    gateways: list[NetGateway]
    routes: list[NetRoute]
    dns_hosts: list[NetDnsHost] = []
    dhcp_hosts: list[NetDhcpHost] = []
    kea_subnets: list[NetKeaSubnet] = []
    kea_reservations: list[NetKeaReservation] = []
    management_device: str | None
    # The firewall's own API can set interface IPs/types (OPNsense >= ~26.7.6). Older 26.7.x
    # only lists assignments. Detected from the data, not from a version number.
    can_configure_interfaces: bool
    # Sections the firewall could not deliver (e.g. older OPNsense without that API) — never silently empty.
    unavailable: list[str]


class NetworkChangeRequest(BaseModel):
    kind: Literal["vlan", "vip", "gateway", "route", "interface", "dns_host", "dhcp_host", "kea_subnet", "kea_reservation"]
    action: Literal["add", "set", "del"]
    # Goes into a URL path, so only plain identifiers.
    uuid: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    fields: dict[str, Any] = Field(default_factory=dict)


class NetworkChangeExecute(NetworkChangeRequest):
    preview_token: str = Field(min_length=64, max_length=64)
    acknowledge: bool = False


class NetworkChangeRisk(BaseModel):
    code: str
    params: dict[str, Any] = Field(default_factory=dict)


class NetworkFieldChange(BaseModel):
    field: str
    before: str | None
    after: str | None


class NetworkChangePreview(BaseModel):
    token: str
    kind: str
    action: str
    uuid: str | None
    host_id: str
    host_name: str
    changes: list[NetworkFieldChange]
    risks: list[NetworkChangeRisk]
    warnings: list[NetworkChangeRisk]
    blocks: list[NetworkChangeRisk]
    requires_acknowledgement: bool
    management_device: str | None
    can_execute: bool


class NetworkChangeResult(BaseModel):
    status: str
    uuid: str | None
    verified: bool
    mismatches: list[str]
    backup_id: str
