"""High availability state (singleton row, id fixed at 1; docs/HA-H2-DESIGN.md 3.5/3.6).

Written by `opnexus ha promote` (epoch + 1, the new primary, the time), read by the backend: /api/health reports the
epoch, the partner guard compares epochs, the scheduler gives overdue rollout steps a grace period after a promote.
The row travels to the standby through replication like everything else. Without HA it stays at epoch 0."""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class HaState(Base):
    __tablename__ = "ha_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    primary_node: Mapped[str | None] = mapped_column(String(32), nullable=True)
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # `opnexus ha sync` stamps this after every successful run (so the standby and the alarm see it, not only the host's file).
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The leader stamps this whenever the standby is seen streaming; "no standby for 5 minutes" counts from here.
    standby_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
