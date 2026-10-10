import uuid
from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database import Base

class UserPreferences(Base):
    __tablename__ = "user_preferences"
    user_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey('admin_users.id',ondelete='CASCADE'), primary_key=True)
    theme: Mapped[str] = mapped_column(String(10), default='system')
    language: Mapped[str] = mapped_column(String(5), nullable=False, default='de', server_default='de')
    # Set once the user finished or dismissed the first-run setup assistant
    # (frontend /setup); it is then no longer offered automatically.
    setup_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
