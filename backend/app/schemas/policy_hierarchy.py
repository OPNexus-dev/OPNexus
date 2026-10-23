import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.rule import DeploymentResult


class PolicyHierarchyGroupWrite(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    parent_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Name darf nicht leer sein")
        return value.strip()


class PolicyHierarchyGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    description: str
    parent_id: uuid.UUID | None
    created_at: datetime
    host_ids: list[uuid.UUID] = Field(default_factory=list)


class PolicyHierarchyRuleCreate(BaseModel):
    hierarchy_group_id: uuid.UUID
    rulebase: Literal["pre", "post"] = "pre"
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

    @field_validator("description", "interface", "protocol", "source_net", "destination_net")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()


class PolicyHierarchyRuleEdit(PolicyHierarchyRuleCreate):
    expected_updated_at: datetime


class PolicyHierarchyRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    config_key: str
    hierarchy_group_id: uuid.UUID
    rulebase: Literal["pre", "post"]
    sequence: int
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
    effective_host_ids: list[uuid.UUID] = Field(default_factory=list)
    workflow_state: Literal["attention", "pending", "cleanup", "unassigned", "disabled", "draft", "synced"]
    workflow_counts: dict[str, int]
    deployments: list[DeploymentResult]


class PolicyHierarchyDeployApproval(BaseModel):
    preview_token: str = Field(min_length=64, max_length=64)


class PolicyHierarchyMoveDirection(BaseModel):
    direction: Literal["up", "down"]


class PolicyHierarchyReorderItem(BaseModel):
    id: uuid.UUID
    expected_updated_at: datetime


class PolicyHierarchyReorder(BaseModel):
    items: list[PolicyHierarchyReorderItem] = Field(min_length=1)
