import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Role = Literal["server", "client"]
Proto = Literal["udp", "tcp"]
TlsKeyMode = Literal["auth", "crypt"]

_PORT_RE = re.compile(r"^\d{1,5}$")


class OpenVpnInstanceBase(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    role: Role = "server"
    proto: Proto = "udp"
    port: str = Field(default="1194", max_length=8)
    # Server-role bind address (optional — blank means "any").
    local: str = Field(default="", max_length=255)
    # Client-role remote peer ("host" or "host:port") — required if role=="client".
    remote: str = Field(default="", max_length=255)
    # Server-role tunnel network in CIDR notation — required if role=="server".
    server_network: str = Field(default="", max_length=255)
    # OPNsense-side certificate/CA UUIDs, already present on the target
    # host(s) — per-host maps ({host_id_str: uuid}) since a UUID is
    # host-local, see models/openvpn_instance.py's class docstring.
    cert_refs: dict[str, str] = Field(default_factory=dict)
    ca_refs: dict[str, str] = Field(default_factory=dict)
    tls_key_enabled: bool = False
    tls_key_mode: TlsKeyMode = "crypt"
    enabled: bool = True
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("description")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()

    @field_validator("cert_refs", "ca_refs")
    @classmethod
    def refs_nonblank(cls, value):
        for host_id, ref in value.items():
            if not ref.strip():
                raise ValueError(f"Zertifikat-/CA-Auswahl für Host {host_id} darf nicht leer sein")
        return {host_id: ref.strip() for host_id, ref in value.items()}

    @field_validator("port")
    @classmethod
    def valid_port(cls, value):
        if not _PORT_RE.match(value) or not (1 <= int(value) <= 65535):
            raise ValueError("Port muss eine Zahl zwischen 1 und 65535 sein")
        return value

    @model_validator(mode="after")
    def role_requires_matching_field(self):
        if self.role == "server" and not self.server_network.strip():
            raise ValueError("Server-Rolle benötigt ein Tunnelnetz (server_network)")
        if self.role == "client" and not self.remote.strip():
            raise ValueError("Client-Rolle benötigt ein Remote-Ziel (remote)")
        return self


class OpenVpnInstanceCreate(OpenVpnInstanceBase):
    # Optional even when tls_key_enabled — an operator may save a draft
    # before having key material ready; preview()/workflow() surface the
    # gap as a warning and block deploy until it's filled in (see
    # openvpn_service.py), the same "allow an incomplete draft" philosophy
    # already used elsewhere in this app.
    tls_key_material: str | None = Field(default=None, min_length=1, max_length=8192)


class OpenVpnInstanceEdit(OpenVpnInstanceBase):
    expected_updated_at: datetime
    # Optional on edit: omitted (None) keeps the previously stored key
    # material unchanged, mirroring IpsecTunnelEdit.pre_shared_key.
    tls_key_material: str | None = Field(default=None, min_length=1, max_length=8192)


class OpenVpnDeploymentResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    host_id: uuid.UUID
    host_name: str
    status: Literal["success", "error"]
    message: str | None = None


class OpenVpnInstanceRead(OpenVpnInstanceBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tls_key_material_set: bool
    created_at: datetime
    updated_at: datetime
    effective_host_ids: list[uuid.UUID] = Field(default_factory=list)
    workflow_state: Literal["attention", "cleanup", "unassigned", "disabled", "draft", "synced"]
    workflow_counts: dict[str, int]
    deployments: list[OpenVpnDeploymentResult]


class OpenVpnDeployApproval(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)
