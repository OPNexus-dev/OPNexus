import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class MoveRequest(BaseModel):
    rule_uuid: str = Field(min_length=1, max_length=64)
    # Directly in front of this rule; null = to the end of the rule's group.
    before_uuid: str | None = Field(default=None, max_length=64)


class OrderChangeRead(BaseModel):
    id: uuid.UUID
    host_id: uuid.UUID
    host_name: str | None = None
    rule_uuid: str
    description: str
    status: str
    message: str | None
    actor: str
    created_at: datetime
    revert_deadline: datetime | None


class OrderRow(BaseModel):
    uuid: str
    description: str
    action: str
    action_label: str
    direction: str
    direction_label: str
    interface: str | None
    interface_label: str | None
    protocol: str | None
    protocol_label: str | None
    source_net: str
    source_port: str | None
    destination_net: str
    destination_port: str | None
    enabled: bool
    is_automatic: bool
    prio_group: str
    sort_order: str
    movable: bool
    # "central" (an OPNexus rule), "hierarchy" (policy hierarchy), "local" (made by hand on this firewall) or "system".
    origin: str
    central_rule_id: uuid.UUID | None = None


class HostOrderRead(BaseModel):
    host_id: uuid.UUID
    rows: list[OrderRow]
    open: list[OrderChangeRead]
