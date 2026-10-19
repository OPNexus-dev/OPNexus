from pydantic import BaseModel, ConfigDict, Field, field_validator


class OidcSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    display_name: str
    issuer: str | None
    client_id: str | None
    client_secret_set: bool
    scopes: str
    groups_claim: str
    admin_group: str | None
    operator_group: str | None
    read_only_group: str | None


class OidcSettingsUpdate(BaseModel):
    """Partial update; the client secret is write-only (set it or clear it)."""

    enabled: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=60)
    issuer: str | None = Field(default=None, max_length=500)
    client_id: str | None = Field(default=None, max_length=255)
    client_secret: str | None = Field(default=None, min_length=1, max_length=512)
    clear_client_secret: bool = False
    scopes: str | None = Field(default=None, min_length=1, max_length=255)
    groups_claim: str | None = Field(default=None, min_length=1, max_length=64)
    admin_group: str | None = Field(default=None, max_length=255)
    operator_group: str | None = Field(default=None, max_length=255)
    read_only_group: str | None = Field(default=None, max_length=255)

    @field_validator("issuer")
    @classmethod
    def https_only(cls, value):
        if value and not value.startswith("https://"):
            raise ValueError("issuer must be an https:// URL")
        return value.strip().rstrip("/") if value else value
