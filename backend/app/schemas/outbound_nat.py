import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.rule import DeployApproval, DeploymentResult  # noqa: F401  (re-exported for routers/outbound_nat.py)


class OutboundNatCreate(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    interface: str = Field(min_length=1, max_length=64)
    source_net: str = Field(default="any", min_length=1, max_length=128)
    destination_net: str = Field(default="any", min_length=1, max_length=128)
    source_not: bool = False
    destination_not: bool = False
    enabled: bool = True
    ipprotocol: Literal["inet", "inet6"] = "inet"
    protocol: str = Field(default="any", min_length=1, max_length=16)
    source_port: str | None = Field(default=None, max_length=32)
    destination_port: str | None = Field(default=None, max_length=32)
    target: str = Field(default="", max_length=128)
    target_port: str | None = Field(default=None, max_length=32)
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)
    policy_hierarchy_group_id: uuid.UUID | None = None

    @field_validator('description', 'interface', 'source_net', 'destination_net', 'protocol')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()


class OutboundNatRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    description: str
    interface: str
    source_net: str
    destination_net: str
    source_not: bool
    destination_not: bool
    enabled: bool
    ipprotocol: Literal["inet", "inet6"]
    protocol: str
    source_port: str | None
    destination_port: str | None
    target: str
    target_port: str | None
    created_at: datetime
    updated_at: datetime
    target_host_ids: list[uuid.UUID]
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)
    policy_hierarchy_group_id: uuid.UUID | None = None
    effective_host_ids: list[uuid.UUID] = Field(default_factory=list)
    workflow_state: Literal["attention", "pending", "cleanup", "unassigned", "disabled", "draft", "synced"]
    workflow_counts: dict[str, int]
    deployments: list[DeploymentResult]


class OutboundNatEdit(OutboundNatCreate):
    expected_updated_at: datetime
