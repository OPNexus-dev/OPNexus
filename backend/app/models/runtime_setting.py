"""Values saved in Settings › Betrieb (app/runtime_settings.py); one row per changed setting."""
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    # Number or text in the unit of the spec; NULL for secrets (they live in the notification secret store).
    value: Mapped[object | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())
    updated_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
