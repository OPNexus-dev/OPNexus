from datetime import datetime
import uuid

from pydantic import BaseModel, Field


class OverlaySettingsRead(BaseModel):
    enabled: bool
    endpoint: str | None
    subnet: str
    listen_port: int
    hub_public_key: str | None
    hub_address: str
    hub_state: str  # running | stale | absent


class OverlaySettingsUpdate(BaseModel):
    enabled: bool | None = None
    endpoint: str | None = Field(default=None, max_length=255)
    subnet: str | None = Field(default=None, max_length=18)
    listen_port: int | None = Field(default=None, ge=1, le=65535)


class PeerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class PeerUpdate(BaseModel):
    public_key: str | None = Field(default=None, max_length=44)
    host_id: uuid.UUID | None = None
    clear_host: bool = False


class PeerRead(BaseModel):
    id: uuid.UUID
    name: str
    ip: str
    public_key: str | None
    host_id: uuid.UUID | None
    host_name: str | None
    connected: bool
    last_handshake_at: datetime | None
    rx_bytes: int
    tx_bytes: int
    remote_endpoint: str | None
