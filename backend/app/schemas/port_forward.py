import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.rule import DeployApproval, DeploymentResult  # noqa: F401  (re-exported for routers/port_forward.py)


class PortForwardCreate(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    interface: str = Field(min_length=1, max_length=64)
    ipprotocol: Literal["inet", "inet6"] = "inet"
    protocol: str = Field(default="tcp", min_length=1, max_length=16)
    source_net: str = Field(default="any", min_length=1, max_length=128)
    source_port: str | None = Field(default=None, max_length=32)
    destination_net: str = Field(default="any", min_length=1, max_length=128)
    destination_port: str | None = Field(default=None, max_length=32)
    target: str = Field(min_length=1, max_length=128)
    local_port: str | None = Field(default=None, max_length=32)
    natreflection: Literal["", "purenat", "disable"] = ""
    enabled: bool = True
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)
    policy_hierarchy_group_id: uuid.UUID | None = None

    @field_validator("description", "interface", "protocol", "source_net", "destination_net", "target")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()


class PortForwardRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    description: str
    interface: str
    ipprotocol: str
    protocol: str
    source_net: str
    source_port: str | None
    destination_net: str
    destination_port: str | None
    target: str
    local_port: str | None
    natreflection: str
    enabled: bool
    created_at: datetime
    updated_at: datetime
    target_host_ids: list[uuid.UUID]
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)
    policy_hierarchy_group_id: uuid.UUID | None = None
    effective_host_ids: list[uuid.UUID] = Field(default_factory=list)
    workflow_state: Literal["attention", "pending", "cleanup", "unassigned", "disabled", "draft", "synced"]
    workflow_counts: dict[str, int]
    deployments: list[DeploymentResult]


class PortForwardEdit(PortForwardCreate):
    expected_updated_at: datetime
