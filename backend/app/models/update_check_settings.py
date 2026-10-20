"""Version check configured in the web UI (singleton row, id fixed at 1).

Off by default: OPNexus makes no outgoing request until an admin switches it on. An
UPDATE_CHECK_URL in the server's .env still wins over this row (server-managed)."""
from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

DEFAULT_FEED_URL = "https://updates.opnexus.dev/latest.json"


class UpdateCheckSettings(Base):
    __tablename__ = "update_check_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    feed_url: Mapped[str] = mapped_column(String(500), nullable=False, default=DEFAULT_FEED_URL, server_default=DEFAULT_FEED_URL)
