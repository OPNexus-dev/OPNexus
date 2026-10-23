import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.rule import DeployApproval, DeploymentResult  # noqa: F401  (re-exported for routers/one_to_one.py)


class OneToOneCreate(BaseModel):
    description: str = Field(min_length=1, max_length=255)
    interface: str = Field(min_length=1, max_length=64)
    source_net: str = Field(default="any", min_length=1, max_length=128)
    destination_net: str = Field(default="any", min_length=1, max_length=128)
    source_not: bool = False
    destination_not: bool = False
    enabled: bool = True
    type: Literal["binat", "nat"] = "binat"
    external: str = Field(min_length=1, max_length=128)
    natreflection: Literal["", "enable", "disable"] = ""
    log: bool = False
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)
    policy_hierarchy_group_id: uuid.UUID | None = None

    @field_validator('description', 'interface', 'source_net', 'destination_net', 'external')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Feld darf nicht nur Leerzeichen enthalten")
        return value.strip()


class OneToOneRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    config_key: str
    description: str
    interface: str
    source_net: str
    destination_net: str
    source_not: bool
    destination_not: bool
    enabled: bool
    type: Literal["binat", "nat"]
    external: str
    natreflection: Literal["", "enable", "disable"]
    log: bool
    created_at: datetime
    updated_at: datetime
    target_host_ids: list[uuid.UUID]
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)
    policy_hierarchy_group_id: uuid.UUID | None = None
    effective_host_ids: list[uuid.UUID] = Field(default_factory=list)
    workflow_state: Literal["attention", "pending", "cleanup", "unassigned", "disabled", "draft", "synced"]
    workflow_counts: dict[str, int]
    deployments: list[DeploymentResult]


class OneToOneEdit(OneToOneCreate):
    expected_updated_at: datetime
