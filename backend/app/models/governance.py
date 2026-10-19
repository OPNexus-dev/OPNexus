"""Separation of duties (singleton row): may operators roll changes out themselves?"""
from sqlalchemy import Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GovernanceSettings(Base):
    __tablename__ = "governance_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # False: an operator prepares changes (edit, preview) but only an admin may deploy them.
    operators_may_deploy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
