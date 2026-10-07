import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.rule import DeployApproval, DeploymentResult  # generic, reused as-is

# The OPNsense alias types this app manages centrally — content is a plain
# newline-separated list for all of these (verified against OPNsense's own
# alias model). Deliberately excludes: "internal" (auto-generated
# __if_network, not user-creatable), "external" (built-ins like
# bogons/sshlockout), and "url"/"urltable"/"urljson"/"geoip"/"dynipv6host"/
# "authgroup" (need a URL or other external config instead of/alongside a
# content list — different form shape, not yet built).
AliasType = Literal["host", "network", "port", "networkgroup", "mac", "asn"]


class AliasCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_]+$")
    type: AliasType = "host"
    # One entry per line — IPs/hostnames (host), CIDRs (network), or
    # numbers/ranges (port). Not validated further here: OPNsense is the
    # authority on what's a legal entry for a given type, and rejects an
    # invalid one at push time (surfaced as a per-host deploy error, same
    # as an unknown interface today).
    content: str = Field(default="", max_length=8000)
    description: str = Field(default="", max_length=255)
    enabled: bool = True
    target_host_ids: list[uuid.UUID] = Field(default_factory=list)
    target_group_ids: list[uuid.UUID] = Field(default_factory=list)
    policy_hierarchy_group_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Name darf nicht leer sein")
        return value.strip()

    @field_validator("content")
    @classmethod
    def clean_content(cls, value):
        return "\n".join(line.strip() for line in value.splitlines() if line.strip())


class AliasRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    type: str
    content: str
    description: str
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


class AliasEdit(AliasCreate):
    expected_updated_at: datetime


__all__ = ["AliasCreate", "AliasRead", "AliasEdit", "DeployApproval", "DeploymentResult"]
