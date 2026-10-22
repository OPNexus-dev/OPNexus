"""Separation of duties (singleton row): may operators roll changes out themselves?"""
from sqlalchemy import JSON, Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class GovernanceSettings(Base):
    __tablename__ = "governance_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # False: an operator prepares changes (edit, preview) but only an admin may deploy them.
    operators_may_deploy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Rule cleanup (1.31). At most this many rules per cleanup operation (default 10, ceiling 25).
    cleanup_max_rules: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    # A rule must have been switched off (and confirmed) this many days before it may be deleted (minimum 1).
    cleanup_delete_min_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    # IPs/CIDRs the admin manages the firewalls from; no rule that could match them may be cleaned up. Empty list =
    # the cleanup is locked (fail closed). Only an admin may change it.
    cleanup_management_sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Extra TCP ports the firewalls are managed on (besides the API port of base_url, 443, 80 and 22): a rule that
    # could match a management source on one of them is protected too. Only an admin may change it.
    cleanup_management_ports: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
