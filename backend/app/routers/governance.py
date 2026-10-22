"""Separation of duties: with ``operators_may_deploy`` off, only admins roll changes out (AuthMiddleware enforces it).

The rule-cleanup settings (1.31) live here too: how many rules one cleanup may touch, how long a rule must stay
switched off before it may be deleted, the management sources (addresses the firewalls are managed from; no rule
that could match them is ever cleaned up) and extra management ports (besides the API port, 443, 80 and 22). Only an admin writes any of it (middleware: ``/api/settings/...`` is
admin-only for operators and tokens below scope admin; checked again here) and every change is audited with the
old and the new values."""
import ipaddress
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.governance import GovernanceSettings
from app.models.user import AdminUser
from app.routers.auth import current_user
from app.services import audit_service

router = APIRouter(prefix="/settings/governance", tags=["governance"])

MAX_CLEANUP_RULES = 25      # same ceiling as services/rule_cleanup_service.MAX_RULES_CEILING
MAX_DELETE_MIN_DAYS = 365
MAX_MANAGEMENT_SOURCES = 20
MAX_MANAGEMENT_PORTS = 20


class GovernanceRead(BaseModel):
    operators_may_deploy: bool
    cleanup_max_rules: int = 10
    cleanup_delete_min_days: int = 7
    # The list itself is shown to admins only; everybody else sees just whether it is set.
    cleanup_management_sources: list[str] = Field(default_factory=list)
    cleanup_management_sources_configured: bool = False
    # Extra management ports (besides API port, 443, 80, 22): also admins only, others see the flag.
    cleanup_management_ports: list[int] = Field(default_factory=list)
    cleanup_management_ports_configured: bool = False


class GovernanceUpdate(BaseModel):
    operators_may_deploy: bool | None = None
    cleanup_max_rules: int | None = None
    cleanup_delete_min_days: int | None = None
    cleanup_management_sources: list[str] | None = None
    cleanup_management_ports: list[Any] | None = None  # validated in clean_management_ports (no bool coercion)


def _bad(field: str, reason: str):
    return api_error(422, ErrorCode.BEREINIGUNG_EINSTELLUNG_UNGUELTIG, field=field, reason=reason)


def clean_management_sources(values: list[str]) -> list[str]:
    """IP addresses and CIDR networks only (no host names: they could resolve differently later), canonical form,
    without duplicates; not the whole address space, which would protect every rule."""
    if len(values) > MAX_MANAGEMENT_SOURCES:
        raise _bad("cleanup_management_sources", "too_many")
    cleaned: list[str] = []
    for raw in values:
        text = str(raw).strip()
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError:
            raise _bad("cleanup_management_sources", "not_an_address") from None
        if net.prefixlen == 0:
            raise _bad("cleanup_management_sources", "everything")
        entry = str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net)
        if entry not in cleaned:
            cleaned.append(entry)
    return cleaned


def clean_management_ports(values) -> list[int]:
    """TCP ports 1..65535, at most MAX_MANAGEMENT_PORTS, without duplicates, sorted."""
    if len(values) > MAX_MANAGEMENT_PORTS:
        raise _bad("cleanup_management_ports", "too_many")
    ports: set[int] = set()
    for raw in values:
        text = str(raw).strip()
        if isinstance(raw, bool) or not text.isdigit() or not 1 <= int(text) <= 65535:
            raise _bad("cleanup_management_ports", "not_a_port")
        ports.add(int(text))
    return sorted(ports)


def _read(row: GovernanceSettings, user: AdminUser) -> GovernanceRead:
    sources = list(row.cleanup_management_sources or [])
    ports = list(row.cleanup_management_ports or [])
    return GovernanceRead(
        operators_may_deploy=row.operators_may_deploy, cleanup_max_rules=row.cleanup_max_rules or 10,
        cleanup_delete_min_days=row.cleanup_delete_min_days or 7,
        cleanup_management_sources=sources if user.role == "admin" else [],
        cleanup_management_sources_configured=bool(sources),
        cleanup_management_ports=ports if user.role == "admin" else [],
        cleanup_management_ports_configured=bool(ports))


async def get_settings(db: AsyncSession) -> GovernanceSettings:
    row = await db.get(GovernanceSettings, 1)
    if row is None:
        row = GovernanceSettings(id=1, operators_may_deploy=True)
        db.add(row)
        await db.flush()
    return row


@router.get("", response_model=GovernanceRead)
async def read(db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    """Readable by everybody signed in (the UI explains why a deploy was refused)."""
    return _read(await get_settings(db), user)


@router.put("", response_model=GovernanceRead)
async def update(payload: GovernanceUpdate, db: AsyncSession = Depends(get_db), user: AdminUser = Depends(current_user)):
    if user.role != "admin":
        raise api_error(403, ErrorCode.ADMIN_REQUIRED)
    row = await get_settings(db)
    changes: dict[str, dict] = {}
    if payload.cleanup_max_rules is not None:
        if not 1 <= payload.cleanup_max_rules <= MAX_CLEANUP_RULES:
            raise _bad("cleanup_max_rules", f"1..{MAX_CLEANUP_RULES}")
    if payload.cleanup_delete_min_days is not None:
        if not 1 <= payload.cleanup_delete_min_days <= MAX_DELETE_MIN_DAYS:
            raise _bad("cleanup_delete_min_days", f"1..{MAX_DELETE_MIN_DAYS}")
    sources = clean_management_sources(payload.cleanup_management_sources) if payload.cleanup_management_sources is not None else None
    ports = clean_management_ports(payload.cleanup_management_ports) if payload.cleanup_management_ports is not None else None
    for field, value in (("cleanup_max_rules", payload.cleanup_max_rules), ("cleanup_delete_min_days", payload.cleanup_delete_min_days),
                         ("cleanup_management_sources", sources), ("cleanup_management_ports", ports)):
        if value is not None and value != getattr(row, field):
            changes[field] = {"old": getattr(row, field), "new": value}
            setattr(row, field, value)
    if payload.operators_may_deploy is not None:
        row.operators_may_deploy = payload.operators_may_deploy
    if changes:
        db.add(audit_service.make_event(
            kind="settings", action="cleanup_settings", status="accepted", entity_id="governance",
            message="Einstellungen der Regel-Bereinigung geändert", details={"changes": changes}))
    await db.commit()
    return _read(row, user)
