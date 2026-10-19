import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TokenScope(str, Enum):
    read = "read"
    deploy = "deploy"
    admin = "admin"


class ApiTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # Least privilege by default.
    scope: TokenScope = TokenScope.read
    # None = never expires. The UI defaults to 90 days.
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)
    # Creating a long-lived credential needs the account password again.
    current_password: str = Field(min_length=1, max_length=1024)


class ApiTokenRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    scope: TokenScope
    prefix: str
    owner: str
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    last_used_ip: str | None


class ApiTokenCreated(ApiTokenRead):
    # The only time the raw token is ever returned.
    token: str
