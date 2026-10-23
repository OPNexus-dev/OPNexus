import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.firewall_url import validate_base_url


class HostCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str = Field(min_length=1, max_length=255)
    api_key: str = Field(min_length=1, max_length=255)
    api_secret: str = Field(min_length=1)
    verify_tls: bool = True
    # Pinned certificate of the firewall (PEM), obtained via POST /hosts/tls-probe.
    tls_ca_pem: str | None = Field(default=None, max_length=32768)
    enabled: bool = True
    cluster_name: str | None = Field(default=None, max_length=120)
    policy_hierarchy_group_id: uuid.UUID | None = None
    # Ask the firewall once with these credentials before saving (the UI and the setup assistant do). Off by default so
    # scripts can still register a firewall that is not reachable yet (behind NAT, tunnel not up).
    verify_connection: bool = False

    @field_validator("base_url")
    @classmethod
    def _base_url_is_safe(cls, value: str) -> str:
        return validate_base_url(value)


class TlsProbeRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=255)


class TlsProbeResult(BaseModel):
    pem: str
    sha256: str
    subject: str
    issuer: str
    self_signed: bool
    not_before: datetime
    not_after: datetime
    expired: bool
    san: list[str]


class HostUpdate(BaseModel):
    """Partial update — everything optional, only provided fields change.
    Credential rotation (api_key/api_secret) isn't included yet; delete +
    recreate is still the way to rotate a host's API key for now."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    # New address of the firewall, e.g. after its management IP was changed.
    base_url: str | None = Field(default=None, min_length=1, max_length=255)
    verify_tls: bool | None = None
    tls_ca_pem: str | None = Field(default=None, max_length=32768)
    clear_tls_pin: bool = False
    enabled: bool | None = None
    cluster_name: str | None = Field(default=None, max_length=120)
    clear_cluster_name: bool = False  # explicit flag, since cluster_name=None is ambiguous with "don't touch it"
    policy_hierarchy_group_id: uuid.UUID | None = None
    clear_policy_hierarchy_group_id: bool = False  # same ambiguity as clear_cluster_name above

    @field_validator("base_url")
    @classmethod
    def _base_url_is_safe(cls, value: str | None) -> str | None:
        return validate_base_url(value) if value is not None else None


class HostRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_url: str
    verify_tls: bool
    tls_pinned: bool = False
    tls_fingerprint: str | None = None
    enabled: bool
    cluster_name: str | None
    policy_hierarchy_group_id: uuid.UUID | None = None
    created_at: datetime
    status: Literal["online", "offline", "disabled"]
    carp_status: str | None = None
    status_detail: str | None = None


class LiveRule(BaseModel):
    uuid: str
    description: str
    action: str
    action_label: str
    direction: str
    direction_label: str
    interface: str | None
    interface_label: str | None
    protocol: str | None
    protocol_label: str | None
    source_net: str
    source_port: str | None
    destination_net: str
    destination_port: str | None
    enabled: bool
    is_automatic: bool


class FirmwareInfo(BaseModel):
    product_id: str
    product_version: str


class InterfaceOption(BaseModel):
    name: str
    label: str


class AliasOption(BaseModel):
    name: str
    type: str
    description: str
    content: str
    enabled: bool


class HostBackup(BaseModel):
    id: str
    created_at: datetime
    size: int


CertStatus = Literal["valid", "expiring_soon", "expired", "unknown"]


class HostCertificate(BaseModel):
    uuid: str
    # OPNsense's own cross-references (OpenVPN cert/ca, Trust-Store caref) use
    # this id, not the row UUID.
    refid: str = ""
    descr: str
    common_name: str | None
    issuer: str
    valid_from: datetime | None
    valid_to: datetime | None
    days_until_expiry: int | None
    status: CertStatus
    in_use: bool


class HostCa(BaseModel):
    uuid: str
    refid: str = ""
    descr: str
    common_name: str | None
    valid_from: datetime | None
    valid_to: datetime | None
    days_until_expiry: int | None
    status: CertStatus
