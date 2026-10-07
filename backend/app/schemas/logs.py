import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class LogEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    host_id: uuid.UUID
    timestamp: datetime
    action: str | None
    interface: str | None
    direction: str | None
    protocol: str | None
    source_net: str | None
    source_port: str | None
    destination_net: str | None
    destination_port: str | None
    label: str | None


class LogSearchResult(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[LogEntryRead]


class LogStatCount(BaseModel):
    value: str
    count: int


class LogStats(BaseModel):
    by_action: dict[str, int]
    by_interface: dict[str, int]
    top_blocked_sources: list[LogStatCount]
    top_destination_ports: list[LogStatCount]


class LogFilterOptions(BaseModel):
    actions: list[str]
    interfaces: list[str]
    protocols: list[str]


class LogSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    poll_interval_seconds: int
    retention_days: int
    max_size_gb: float
    # Filled by the router from Postgres statistics, not stored.
    used_bytes: int = 0
    entries: int = 0
    oldest_at: datetime | None = None


class LogSettingsUpdate(BaseModel):
    enabled: bool | None = None
    poll_interval_seconds: int | None = Field(default=None, ge=1)
    # No upper bound enforced server-side beyond the floor below — it's a
    # disk-usage tradeoff for the operator to judge based on their own
    # firewalls' traffic volume and available disk, not a fixed limit
    # OPNexus can know in advance.
    retention_days: int | None = Field(default=None, ge=1)
    # 0 = unlimited; the upper bound only guards against typos.
    max_size_gb: float | None = Field(default=None, ge=0, le=100000)
