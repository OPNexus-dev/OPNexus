"""Per-host values for ``{{NAME}}`` placeholders in centrally authored objects (see services/variables_service.py)."""
import uuid

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HostVariable(Base):
    __tablename__ = "host_variables"
    __table_args__ = (UniqueConstraint("host_id", "name", name="uq_host_variable"),)

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    host_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("opnsense_hosts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(41), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
