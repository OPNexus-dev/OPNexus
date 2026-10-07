"""Detects CARP-role transitions and records them to carp_events for a
historical timeline (see app/models/carp_event.py). Called periodically from
main.py's existing _notification_poll_loop, on the same cadence as the other
live-status checks — a deliberate extra get_statuses() round trip rather than
reworking notification_checks.check_host_offline's own call to it.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.carp_event import CarpEvent
from app.models.host import OpnsenseHost
from app.services.host_status_service import get_statuses


async def _last_status(db: AsyncSession, host_id) -> str | None:
    result = await db.execute(
        select(CarpEvent.to_status)
        .where(CarpEvent.host_id == host_id)
        .order_by(CarpEvent.detected_at.desc())
        .limit(1)
    )
    row = result.first()
    return row[0] if row else None


async def detect_and_record(db: AsyncSession) -> list[CarpEvent]:
    result = await db.execute(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)))
    hosts = [h for h in result.scalars().all() if h.cluster_name]
    if not hosts:
        return []

    statuses = await get_statuses(hosts)
    created: list[CarpEvent] = []
    for host in hosts:
        info = statuses.get(host.id, {})
        if info.get("status") != "online":
            # Unreachable readings are noise here, not a role change — the
            # last confirmed role stays on record until it's seen again.
            continue
        current = info.get("carp_status")
        previous = await _last_status(db, host.id)
        if current == previous:
            continue
        event = CarpEvent(
            host_id=host.id, host_name=host.name, cluster_name=host.cluster_name,
            from_status=previous, to_status=current,
        )
        db.add(event)
        created.append(event)

    if created:
        await db.commit()
    return created
