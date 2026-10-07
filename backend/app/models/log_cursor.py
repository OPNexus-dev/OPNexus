"""Per-host log-ingest cursor. A separate table rather than a column on
`opnsense_hosts` — deliberately, so this brand-new feature needs no manual
`ALTER TABLE` on the existing table for already-running installs (see
host.py's `policy_hierarchy_group_id` comment for the alternative that DID
need one). `Base.metadata.create_all()` alone is enough since this whole
table is new.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HostLogCursor(Base):
    __tablename__ = "host_log_cursor"

    host_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), primary_key=True
    )
    # None until the first successful poll of this host.
    last_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
