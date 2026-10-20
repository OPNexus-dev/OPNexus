"""Single sign-on via OpenID Connect: settings (singleton) and in-flight login attempts.

The client secret lives on disk (notification_secret_store, name ``oidc_client_secret``), not here.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OidcSettings(Base):
    __tablename__ = "oidc_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    display_name: Mapped[str] = mapped_column(String(60), nullable=False, default="SSO")
    issuer: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    client_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    client_secret_set: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scopes: Mapped[str] = mapped_column(String(255), nullable=False, default="openid profile email")
    groups_claim: Mapped[str] = mapped_column(String(64), nullable=False, default="groups")
    admin_group: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    operator_group: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    read_only_group: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)


class OidcLoginState(Base):
    """One row per started login; consumed (deleted) by the callback, expires after minutes."""

    __tablename__ = "oidc_login_states"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
