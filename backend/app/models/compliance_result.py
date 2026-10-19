"""Latest security-posture result per host (see services/compliance_service.py).

One row per host, replaced on every check. The findings never contain
configuration values beyond rule descriptions and the protocol names they
report on; secrets from config.xml are never read into them.
"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ComplianceResult(Base):
    __tablename__ = "compliance_results"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # None when the check could not run (see error).
    score: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # None | "unreachable" | "invalid_config"
    error: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    error_detail: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
