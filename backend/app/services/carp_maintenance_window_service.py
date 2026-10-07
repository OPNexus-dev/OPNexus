"""Optional auto-revert timer layered on top of the existing CARP
maintenance-mode toggle (opnsense_client.toggle_carp_maintenance, driven by
routers/maintenance.py's carp-preview/carp-execute/{id}/acknowledge flow).

Unlike a rule/NAT deploy, entering CARP maintenance is a role handover, not
a config write that could sever the management path — so, unlike the VPN
trilogy's deliberate no-auto-revert stance, automatically calling the same
toggle back is safe and prevents the real, already-observed failure mode of
a host being left demoted indefinitely because a human forgot to switch it
back. The sweep bypasses the human confirm-name/acknowledge gate entirely
(same rationale _deploy_revert_sweep_loop already uses elsewhere): the
human already opted in by choosing a duration when entering maintenance.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.carp_maintenance_window import CarpMaintenanceWindow
from app.models.host import OpnsenseHost
from app.services import audit_service, host_status_service
from app.services import opnsense_client as client

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def open_window(db: AsyncSession, host: OpnsenseHost, auto_revert_minutes: int | None) -> CarpMaintenanceWindow | None:
    if not auto_revert_minutes or auto_revert_minutes <= 0:
        return None
    window = CarpMaintenanceWindow(
        host_id=host.id, host_name=host.name,
        auto_revert_at=_now() + timedelta(minutes=auto_revert_minutes),
        status="active",
    )
    db.add(window)
    await db.commit()
    return window


async def active_window(db: AsyncSession, host_id) -> CarpMaintenanceWindow | None:
    return await db.scalar(
        select(CarpMaintenanceWindow)
        .where(CarpMaintenanceWindow.host_id == host_id, CarpMaintenanceWindow.status == "active")
        .order_by(CarpMaintenanceWindow.entered_at.desc())
    )


async def close_windows_for_manual_leave(db: AsyncSession, host: OpnsenseHost) -> None:
    """Called when a human completes a carp_leave themselves, pre-empting
    any running timer for that host."""
    result = await db.execute(
        select(CarpMaintenanceWindow).where(CarpMaintenanceWindow.host_id == host.id, CarpMaintenanceWindow.status == "active")
    )
    for window in result.scalars().all():
        window.status = "cancelled_manual"
        window.resolved_at = _now()
        window.message = "Manuell vor Ablauf des Zeitfensters in den Normalbetrieb zurückgeschaltet."
    await db.commit()


async def sweep_expired(db: AsyncSession) -> list[CarpMaintenanceWindow]:
    result = await db.execute(
        select(CarpMaintenanceWindow).where(
            CarpMaintenanceWindow.status == "active", CarpMaintenanceWindow.auto_revert_at <= _now(),
        )
    )
    windows = list(result.scalars().all())
    resolved: list[CarpMaintenanceWindow] = []
    for window in windows:
        host = await db.get(OpnsenseHost, window.host_id)
        if not host:
            window.status = "revert_failed"
            window.resolved_at = _now()
            window.message = "Host wurde gelöscht, bevor das Zeitfenster ablief."
            resolved.append(window)
            continue
        try:
            statuses = await host_status_service.get_statuses([host])
            if not statuses.get(host.id, {}).get("carp_maintenance_mode"):
                # Already left maintenance some other way (manual action that
                # didn't go through close_windows_for_manual_leave, e.g. a
                # direct OPNsense-side change) — nothing to do.
                window.status = "cancelled_manual"
                window.resolved_at = _now()
                window.message = "Host war beim Ablauf bereits nicht mehr im CARP-Wartungsmodus."
                resolved.append(window)
                continue
            result_payload = await client.toggle_carp_maintenance(host)
            if result_payload.get("action") != "leave_maintenance":
                raise client.OpnsenseAPIError("CARP-Antwort passt nicht zur erwarteten Richtung (leave_maintenance)")
            window.status = "reverted"
            window.resolved_at = _now()
            window.message = "Automatisch nach Ablauf des Zeitfensters in den Normalbetrieb zurückgeschaltet."
            await audit_service.persist(
                kind="host", action="carp_auto_revert", status="submitted",
                host_id=str(host.id), host_name=host.name, entity_id=str(window.id),
                message=window.message, details={"response": result_payload},
            )
        except client.OpnsenseAPIError as exc:
            window.status = "revert_failed"
            window.resolved_at = _now()
            window.message = f"Automatischer Rückschalt-Versuch fehlgeschlagen: {exc}"[:800]
            await audit_service.persist(
                kind="host", action="carp_auto_revert", status="revert_failed",
                host_id=str(host.id), host_name=host.name, entity_id=str(window.id),
                message=window.message,
            )
        resolved.append(window)

    if resolved:
        await db.commit()
    return resolved
