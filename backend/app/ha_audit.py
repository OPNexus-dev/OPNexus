"""Writes one takeover into the audit trail (docs/DESIGN-HA-UI.md section 4).

The standby's backend cannot write while its database is a replica, so the person who clicked "take over" on the /standby page is
recorded after the fact: `opnexus ha promote` runs this inside the (restarted) backend container of the new primary once the
database is writable:

    docker compose exec -T backend python -m app.ha_audit promote ACCOUNT_ID MODE

Exit code 0 on success, 1 otherwise; a failure here never undoes the takeover.
"""
import asyncio
import re
import sys
import uuid

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.ha_state import HaState
from app.models.user import AdminUser

ACTOR_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")   # the account's id; the name is looked up here
MODES = {"normal": "Partner angehalten", "force": "erzwungen (Partner nicht erreichbar)"}


async def record(action: str, actor: str, mode: str) -> uuid.UUID:
    """`actor` is the id of the account that clicked "take over" (the free-form user name never travels through the updater)."""
    if action != "promote" or not ACTOR_RE.fullmatch(actor) or mode not in MODES:
        raise ValueError("unzulässige Angaben")
    async with SessionLocal() as db:
        name = (await db.execute(select(AdminUser.username).where(AdminUser.id == uuid.UUID(actor)))).scalar_one_or_none()
        state = (await db.execute(select(HaState).where(HaState.id == 1))).scalar_one_or_none()
        event = AuditEvent(
            correlation_id=str(uuid.uuid4()), actor=name or f"Konto {actor}", kind="ha", action="promote", status="ok",
            entity_name=settings.ha_node or None,
            message=f"{settings.ha_node or 'Knoten'} ist Primär geworden ({MODES[mode]})",
            details={"mode": mode, "node": settings.ha_node, "epoch": state.epoch if state else None},
        )
        db.add(event)
        await db.commit()
        return event.id


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("Aufruf: python -m app.ha_audit promote ACCOUNT_ID normal|force", file=sys.stderr)
        return 1
    try:
        asyncio.run(record(*argv))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
