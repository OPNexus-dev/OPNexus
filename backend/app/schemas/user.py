import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class UserRole(str, Enum):
    admin = "admin"
    read_only = "read_only"


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=150)
    password: str = Field(min_length=10, max_length=1024)
    # Defaults to the least-privileged role — an admin has to explicitly
    # opt a new account into write access rather than it happening by
    # omission.
    role: UserRole = UserRole.read_only


class UserRoleUpdate(BaseModel):
    role: UserRole


class UserPasswordReset(BaseModel):
    new_password: str = Field(min_length=10, max_length=1024)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    username: str
    role: UserRole
    created_at: datetime
    last_login_at: datetime | None
    totp_enabled: bool = False
