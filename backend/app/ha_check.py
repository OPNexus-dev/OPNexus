"""Are the secret files complete for what the database refers to? (HA, docs/HA-H2-DESIGN.md 3.4)

The secrets are not in the database, and `opnexus ha sync` copies them on a timer, so a firewall or a two-factor account
that was created in the last minutes can exist in the standby's database without its secret file. After a promote that
would be a firewall OPNexus cannot talk to or an admin who cannot sign in. `opnexus ha status` and `ha promote` run this
inside the backend container:

    docker compose exec -T backend python -m app.ha_check

Prints one JSON object (names only, never secrets) and exits 0 if everything is there, 3 if something is missing.
"""
import asyncio
import json
import sys

from sqlalchemy import select

from app.database import SessionLocal
from app.models.host import OpnsenseHost
from app.models.user import AdminUser
from app.services import opnsense_secret_store, totp_service


async def missing() -> dict[str, list[str]]:
    async with SessionLocal() as db:
        hosts = (await db.execute(select(OpnsenseHost.id, OpnsenseHost.name))).all()
        users = (await db.execute(select(AdminUser.id, AdminUser.username).where(AdminUser.totp_enabled.is_(True)))).all()
    return {
        "hosts": sorted(name for host_id, name in hosts if opnsense_secret_store.read_secret(host_id) is None),
        "totp": sorted(name for user_id, name in users if totp_service.read_secret(user_id) is None),
    }


def main() -> int:
    result = asyncio.run(missing())
    print(json.dumps(result))
    return 3 if result["hosts"] or result["totp"] else 0


if __name__ == "__main__":
    sys.exit(main())
