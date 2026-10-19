"""Personal API tokens for scripts, CI jobs, Prometheus and MCP clients.

Only the SHA-256 of the token is stored (the token is 256 random bits, so a
fast hash is enough and lets a request be resolved with one indexed lookup);
the raw value is shown exactly once, at creation. A token belongs to a user and
never exceeds that user's role: see services/api_token_service.py for the
scopes and AuthMiddleware for where they are enforced.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # "read" | "deploy" | "admin"
    scope: Mapped[str] = mapped_column(String(10), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # First characters of the token, so a list can tell tokens apart. Not secret.
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)

    user: Mapped["AdminUser"] = relationship("AdminUser")
