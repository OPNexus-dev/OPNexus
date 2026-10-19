"""Management overlay: a WireGuard hub so OPNexus can reach firewalls behind NAT/CGNAT.

The firewall dials OUT to the hub; OPNexus then talks to its API at the firewall's overlay address. Keys live
on disk (services/overlay_service.py), never in the database.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OverlaySettings(Base):
    __tablename__ = "overlay_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # host or host:port that firewalls dial (public name/IP of this server and the published UDP port)
    endpoint: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    subnet: Mapped[str] = mapped_column(String(18), nullable=False, default="10.99.0.0/24")
    listen_port: Mapped[int] = mapped_column(Integer, nullable=False, default=51820)


class OverlayPeer(Base):
    __tablename__ = "overlay_peers"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    ip: Mapped[str] = mapped_column(String(15), nullable=False, unique=True)
    # the firewall's WireGuard public key, entered once the firewall side exists
    public_key: Mapped[Optional[str]] = mapped_column(String(44), nullable=True, unique=True)
    host_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
