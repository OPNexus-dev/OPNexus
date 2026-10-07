import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class AdminUser(Base):
    """A user account. Two roles, no orgs: "admin" (full read/write, incl.
    managing other users) and "read_only" (every GET works, every write is
    rejected by AuthMiddleware before it reaches a router — see there for
    the exact allowlist). See auth_service.bootstrap_admin for how the
    first account is created."""

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, index=True)
    # Argon2id PHC string — never the raw password.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="admin", server_default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    # Optional TOTP second factor. The secret itself is on disk
    # (services/totp_service), not here; only SHA-256 hashes of the one-time
    # recovery codes and the last accepted time-step (replay guard) are.
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    totp_last_step: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    totp_recovery_hashes: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    sessions: Mapped[list["Session"]] = relationship(
        "Session", back_populates="user", cascade="all, delete-orphan"
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # SHA-256 hex of the random session token. The raw token only ever lives
    # in the client's httpOnly cookie, so a DB leak yields no usable sessions.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    user: Mapped["AdminUser"] = relationship("AdminUser", back_populates="sessions")
