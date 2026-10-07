import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

IkeVersion = Literal["ike", "ikev1", "ikev2"]


class IpsecTunnelBase(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    local_address: str = Field(min_length=1, max_length=255)
    remote_address: str = Field(min_length=1, max_length=255)
    ike_version: IkeVersion = "ikev2"
    proposal: str = Field(default="default", min_length=1, max_length=128)
    local_identity: str = Field(min_length=1, max_length=255)
    remote_identity: str = Field(default="", max_length=255)
    local_networks: str = Field(min_length=1, max_length=255)
    remote_networks: str = Field(min_length=1, max_length=255)
    esp_proposal: str = Field(default="default", min_length=1, max_length=128)
    enabled: bool = True
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("description", "local_address", "remote_address", "local_identity", "local_networks", "remote_networks", "proposal", "esp_proposal")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()


class IpsecTunnelCreate(IpsecTunnelBase):
    # Required on create — a PSK tunnel with no key is pointless, even
    # though OPNsense's own model doesn't mark preSharedKey.Key required.
    pre_shared_key: str = Field(min_length=1, max_length=512)


class IpsecTunnelEdit(IpsecTunnelBase):
    expected_updated_at: datetime
    # Optional on edit: omitted (None) keeps the previously stored secret
    # unchanged, mirroring NotificationSettingsUpdate's smtp_password/
    # webhook_signing_secret pattern — the secret is write-only and never
    # sent back to the client, so the edit form can't "already have it".
    pre_shared_key: str | None = Field(default=None, min_length=1, max_length=512)


class IpsecDeploymentResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    host_id: uuid.UUID
    host_name: str
    status: Literal["success", "error"]
    message: str | None = None


class IpsecTunnelRead(IpsecTunnelBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pre_shared_key_set: bool
    created_at: datetime
    updated_at: datetime
    effective_host_ids: list[uuid.UUID] = Field(default_factory=list)
    workflow_state: Literal["attention", "cleanup", "unassigned", "disabled", "draft", "synced"]
    workflow_counts: dict[str, int]
    deployments: list[IpsecDeploymentResult]


class IpsecDeployApproval(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)
