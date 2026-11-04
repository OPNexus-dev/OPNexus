import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.adopt import LinkTarget


class RuleCreate(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    interface: str = Field(min_length=1, max_length=64)
    direction: Literal["in", "out"] = "in"
    ipprotocol: Literal["inet", "inet6"] = "inet"
    protocol: str = Field(default="any", min_length=1, max_length=16)
    action: Literal["pass", "block", "reject"] = "pass"
    source_net: str = Field(default="any", min_length=1, max_length=128)
    source_port: str | None = Field(default=None, max_length=32)
    destination_net: str = Field(default="any", min_length=1, max_length=128)
    destination_port: str | None = Field(default=None, max_length=32)
    enabled: bool = True
    log: bool = False
    placement: Literal["end", "top"] = "end"
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)


    @field_validator("description", "interface", "protocol", "source_net", "destination_net")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()

    @field_validator("interface")
    @classmethod
    def interface_or_role(cls, value):
        from app.services.variables_service import validate_interface_field
        return validate_interface_field(value)


class DeploymentResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    host_id: uuid.UUID
    host_name: str
    status: Literal["success", "error", "pending", "reverted", "revert_failed"]
    message: str | None = None
    # Only set while status == "pending" — the client counts down to this
    # and shows the Confirm button until it passes.
    revert_deadline: datetime | None = None


class RuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    config_key: str
    description: str
    interface: str
    direction: str
    ipprotocol: str
    protocol: str
    action: str
    source_net: str
    source_port: str | None
    destination_net: str
    destination_port: str | None
    enabled: bool
    log: bool = False
    placement: str = "end"
    created_at: datetime
    updated_at: datetime
    target_host_ids: list[uuid.UUID]
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)
    effective_host_ids: list[uuid.UUID] = Field(default_factory=list)
    workflow_state: Literal["attention", "pending", "cleanup", "unassigned", "disabled", "draft", "synced"]
    workflow_counts: dict[str, int]
    deployments: list[DeploymentResult]


class RuleEdit(RuleCreate):
    expected_updated_at: datetime


class DeployApproval(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)


class AdoptRequest(BaseModel):
    host_id: uuid.UUID
    # Link to this existing central rule instead of creating a new one (services/adopt_link_service.py).
    link_to: uuid.UUID | None = None
    rule_uuid: str = Field(min_length=1, max_length=64)
    # Store the new central rule with this interface role instead of the identifier (the host must have assigned it to that interface).
    interface_role: str | None = Field(default=None, max_length=40, pattern=r"^IF_[A-Z0-9][A-Z0-9_]{0,36}$")


class UnmanagedRule(BaseModel):
    uuid: str
    description: str
    interface: str
    interface_label: str
    interface_roles: list[str] = Field(default_factory=list)  # roles this host has assigned to that interface
    direction: str
    action: str
    protocol: str
    source_net: str
    source_port: str
    destination_net: str
    destination_port: str
    enabled: bool
    log: bool
    adoptable: bool
    # Codes such as "negated_source" or "not_quick"; the UI turns them into text.
    reasons: list[str]
    # Central rules equal to this one (resolved for the host), same-description rules that differ, link possible.
    matches: list[LinkTarget] = Field(default_factory=list)
    similar: list[LinkTarget] = Field(default_factory=list)
    linkable: bool = False
