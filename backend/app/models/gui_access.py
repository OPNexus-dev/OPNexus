"""GUI access to managed firewalls through the separate gui-gateway container (1.27).

Settings are a singleton row (id fixed at 1), off by default. Tickets and gateway
sessions only ever store hashes: the raw ticket travels once through the browser,
the OPNexus session is referenced by the same SHA-256 that `sessions.token_hash` holds.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GuiAccessSettings(Base):
    __tablename__ = "gui_access_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    idle_timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=15, server_default="15")
    absolute_timeout_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60, server_default="60")
    slot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=4, server_default="4")
    updated_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GuiTicket(Base):
    """One-time, 30-second ticket, bound to the OPNexus session, host and slot."""
    __tablename__ = "gui_tickets"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    session_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GuiSession(Base):
    """A redeemed ticket: one firewall GUI open in one slot of one browser session."""
    __tablename__ = "gui_sessions"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot: Mapped[int] = mapped_column(Integer, nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
