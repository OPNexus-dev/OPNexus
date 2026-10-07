import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PORT_RE = re.compile(r"^\d{1,5}$")
# Same mask OPNsense's own Server.name/Client.name fields enforce
# (Wireguard\Server.xml / Wireguard\Client.xml, both
# `/^([0-9a-zA-Z._\-]){1,64}$/u`) — re-validated here so a bad name fails
# fast in this app's own form rather than only on push.
_NAME_RE = re.compile(r"^[0-9a-zA-Z._-]{1,64}$")


class WireguardTunnelBase(BaseModel):
    server_name: str = Field(min_length=1, max_length=64)
    server_public_key: str = Field(min_length=1, max_length=64)
    listen_port: str = Field(default="51820", max_length=8)
    server_tunnel_address: str = Field(min_length=1, max_length=255)
    dns: str = Field(default="", max_length=255)
    mtu: str = Field(default="", max_length=8)

    peer_name: str = Field(min_length=1, max_length=64)
    peer_public_key: str = Field(min_length=1, max_length=64)
    peer_psk_enabled: bool = False
    peer_allowed_ips: str = Field(min_length=1, max_length=255)
    # Optional Endpoint (host/port) this OPNsense box should dial to reach
    # the peer — blank means "this peer only ever connects inward" (e.g. a
    # road-warrior-style client with no fixed address). See
    # wireguard_deploy_service.py's module docstring for the verification
    # caveat on these two fields.
    peer_endpoint_address: str = Field(default="", max_length=255)
    peer_endpoint_port: str = Field(default="", max_length=8)
    peer_keepalive: str = Field(default="", max_length=8)

    enabled: bool = True
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("server_name", "peer_name")
    @classmethod
    def valid_name(cls, value):
        if not _NAME_RE.match(value):
            raise ValueError("Nur alphanumerische Zeichen, Punkt, Bindestrich und Unterstrich, 1-64 Zeichen")
        return value

    @field_validator("server_public_key", "peer_public_key", "server_tunnel_address", "peer_allowed_ips")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()

    @field_validator("listen_port", "peer_endpoint_port")
    @classmethod
    def valid_port(cls, value):
        if value == "":
            return value
        if not _PORT_RE.match(value) or not (1 <= int(value) <= 65535):
            raise ValueError("Port muss leer sein oder eine Zahl zwischen 1 und 65535")
        return value

    @model_validator(mode="after")
    def endpoint_address_requires_port(self):
        if self.peer_endpoint_address.strip() and not self.peer_endpoint_port.strip():
            raise ValueError("Endpoint-Port wird benötigt, wenn eine Endpoint-Adresse gesetzt ist")
        return self


class WireguardTunnelCreate(WireguardTunnelBase):
    # Required on create — a WireGuard interface with no private key is
    # non-functional, even though OPNsense's own Server model would still
    # accept an add() call with a blank privkey (it would just fail at the
    # protocol level, not the config-API level).
    server_private_key: str = Field(min_length=1, max_length=8192)
    # Optional even when peer_psk_enabled — an operator may save a draft
    # before having key material ready; preview()/workflow() surface the
    # gap as a warning and block deploy until it's filled in (see
    # wireguard_service.py), same "allow an incomplete draft" philosophy
    # already used for IPsec/OpenVPN.
    peer_psk_material: str | None = Field(default=None, min_length=1, max_length=8192)


class WireguardTunnelEdit(WireguardTunnelBase):
    expected_updated_at: datetime
    # Optional on edit: omitted (None) keeps the previously stored secret
    # unchanged, mirroring IpsecTunnelEdit.pre_shared_key/
    # OpenVpnInstanceEdit.tls_key_material.
    server_private_key: str | None = Field(default=None, min_length=1, max_length=8192)
    peer_psk_material: str | None = Field(default=None, min_length=1, max_length=8192)


class WireguardDeploymentResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    host_id: uuid.UUID
    host_name: str
    status: Literal["success", "error"]
    message: str | None = None


class WireguardTunnelRead(WireguardTunnelBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    server_private_key_set: bool
    peer_psk_set: bool
    created_at: datetime
    updated_at: datetime
    effective_host_ids: list[uuid.UUID] = Field(default_factory=list)
    workflow_state: Literal["attention", "cleanup", "unassigned", "disabled", "draft", "synced"]
    workflow_counts: dict[str, int]
    deployments: list[WireguardDeploymentResult]


class WireguardDeployApproval(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)
