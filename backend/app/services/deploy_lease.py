"""An exclusive per-object lock for flows that make several remote calls and commit in between (VPN deploys).

``policy_lock`` is transaction-scoped and is released by every checkpoint commit; this one is a session-level advisory
lock on a connection of its own, so it spans the commits of the request session. If the process dies the connection
closes and PostgreSQL frees the lock. It never waits: a second deploy/removal of the same object is refused at once.
"""
import uuid
import zlib
from contextlib import asynccontextmanager

from sqlalchemy import text

from app.database import engine
from app.errors import ErrorCode, api_error

# Written when the first row is committed, before any remote call: a crash or cancellation leaves this, and a new
# deploy continues from the UUIDs recorded after it.
VPN_INTERRUPTED = (
    "Ausrollen unterbrochen oder Ausgang unklar: Die Firewall hat möglicherweise schon Teile der Konfiguration "
    "erhalten. Erneutes Ausrollen setzt dort fort."
)


@asynccontextmanager
async def resource_lease(kind: str, resource_id: uuid.UUID):
    params = {"a": zlib.crc32(kind.encode()) & 0x7FFFFFFF, "b": resource_id.int & 0x7FFFFFFF}
    async with engine.connect() as conn:
        if not await conn.scalar(text("SELECT pg_try_advisory_lock(:a, :b)"), params):
            raise api_error(409, ErrorCode.VPN_DEPLOY_LAEUFT_BEREITS)
        try:
            yield
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:a, :b)"), params)
            except Exception:
                await conn.invalidate()  # never hand a connection that may still hold the lock back to the pool
            except BaseException:  # cancelled while unlocking
                await conn.invalidate()
                raise
