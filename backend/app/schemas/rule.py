import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)


    @field_validator("description", "interface", "protocol", "source_net", "destination_net")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()


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
