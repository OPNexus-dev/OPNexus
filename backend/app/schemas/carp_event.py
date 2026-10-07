import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CarpEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    host_id: uuid.UUID
    host_name: str
    cluster_name: str | None
    from_status: str | None
    to_status: str | None
    detected_at: datetime
