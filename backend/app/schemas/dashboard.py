import uuid
from datetime import datetime

from pydantic import BaseModel


class DashboardHost(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    carp_status: str | None
    cluster_name: str | None


class PendingAction(BaseModel):
    kind: str
    status: str
    entity_name: str
    host_name: str
    message: str | None
    created_at: datetime
    link: str


class ActivityItem(BaseModel):
    id: uuid.UUID
    created_at: datetime
    actor: str
    kind: str
    action: str
    status: str
    entity_name: str | None
    host_name: str | None
    message: str | None


class DashboardSummary(BaseModel):
    hosts: list[DashboardHost]
    pending_actions: list[PendingAction]
    recent_activity: list[ActivityItem]
