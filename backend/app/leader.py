"""Leader gate: only one backend process runs the background loops (phase H0 of docs/HA-PLAN.md).

Two backends against the same database are not hypothetical: a second stack during a host move, an update that
briefly overlaps, more than one Uvicorn worker, or (later) a standby. Deploys and sweeps are already serialized by
``policy_lock``, but the loops are not: in the lab two backends sent every notification twice and aborted cycles on
``UniqueViolation``. So the loops run only in the process that holds a session-level advisory lock.

How it holds:
- The lock lives on a connection of its own that is never returned to the pool. If the process dies, PostgreSQL
  closes the session and frees the lock.
- The lock can also vanish while this process lives (PostgreSQL restart, network cut, the session terminated). The
  connection is therefore checked every ``check_interval`` seconds and before every loop iteration (``checkpoint``).
  On the first failure every loop is cancelled at once and the process applies for the lock again.
- A process that just got the lock waits ``takeover_delay`` before starting its loops: longer than the previous
  holder needs to notice its loss (one check interval plus the check timeout). Without that wait both could run an
  iteration at the same moment right after a takeover.
- Losing the lock cancels the loops mid-iteration, like a hard restart would; every loop is built to survive that
  (deadlines and phases live in the database, ambiguous firewall calls are reconciled). The check timeout is generous
  (5 s) so a merely sluggish database does not make a single installation drop its loops.
- A database in recovery (a streaming replica) never yields a leader: advisory locks work there, the loops would
  not (they write). The process stays in standby until the database is promoted.
- Partner guard (HA, docs/HA-H2-DESIGN.md 3.6): with ``HA_PEER_IP`` set, the gate asks the partner node's /api/health
  before it takes the lock, once more before it starts the loops, and then every ``partner_interval`` seconds. A
  partner that reports ``leader`` with an epoch at least as high as ours means two primaries (the old one came back
  without being fenced): this process becomes ``fenced``, runs no loops and reports ``split_brain``. An unreachable
  partner raises no objection: stopping the old primary is the operator's job (fencing in ``opnexus ha promote``);
  the guard only catches the case where that went wrong. Equal epochs fence both sides: the safe way to fail.

The API is served by every process regardless of its role; writes through the API are serialized by the database
as before. ``role`` is shown by /api/health and the metrics.
"""
import asyncio
import logging
import ipaddress
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy import text

from app.config import settings
from app.database import SessionLocal, engine

logger = logging.getLogger(__name__)

# Next to policy_lock (728194031) and the admin-role lock (728194032).
LOCK_KEY = 728194033

STARTING, STANDBY, TAKEOVER, LEADER, REPLICA, FENCED = "starting", "standby", "takeover", "leader", "replica", "fenced"


class LeadershipLost(Exception):
    """Raised by ``checkpoint`` in a loop of a process that is no longer the leader. Loops must not catch it."""


LoopFactory = Callable[[], Awaitable[None]]
# What the partner's /api/health said: (role, epoch), or None if it could not be asked.
PartnerView = tuple[str, int] | None
PartnerProbe = Callable[[], Awaitable[PartnerView]]
PARTNER_TIMEOUT = 3.0
HEALTH_DB_TIMEOUT = 1.0


def http_partner(peer_ip: str, port: int | None = None) -> PartnerProbe | None:
    """Probe of the partner node over HTTPS (certificate not verified: only role and epoch are read, and an attacker
    who can forge them gains nothing a fenced process would not lose anyway). None if the address is no IP."""
    try:
        address = ipaddress.ip_address(peer_ip.strip())
    except ValueError:
        logger.warning("HA_PEER_IP ist keine IP-Adresse (%r): der Partner-Wächter ist aus", peer_ip)
        return None
    host = f"[{address}]" if address.version == 6 else str(address)
    port = port or settings.ha_peer_port
    authority = host if port == 443 else f"{host}:{port}"

    async def probe() -> PartnerView:
        try:
            async with httpx.AsyncClient(verify=False, timeout=PARTNER_TIMEOUT) as http:
                response = await http.get(f"https://{authority}/api/health")
            data = response.json()
            return str(data.get("role")), int(data.get("epoch") or 0)
        except Exception:
            return None

    return probe


async def read_epoch() -> int | None:
    """Epoch of this node's database for /api/health (None if the database cannot be read right now)."""
    from app.services import ha_service  # late: the services import models that import app.database
    async def read() -> int:
        async with SessionLocal() as db:
            return await ha_service.own_epoch(db)

    try:  # /api/health must answer within the Docker healthcheck's and the partner's timeouts even if the database is slow
        return await asyncio.wait_for(read(), HEALTH_DB_TIMEOUT)
    except Exception:
        return None


class LeaderGate:
    def __init__(self, loops: dict[str, LoopFactory], *, check_interval: float = 5.0, check_timeout: float = 5.0,
                 retry_interval: float = 15.0, takeover_delay: float | None = None, connect=engine.connect,
                 partner: PartnerProbe | None = None, partner_interval: float = 30.0):
        self._loops = loops
        self._partner = partner or (http_partner(settings.ha_peer_ip) if settings.ha_peer_ip.strip() else None)
        self.partner_interval = partner_interval
        self._partner_at = float("-inf")
        self._partner_objection = False
        self._epoch = 0
        self.partner_view: PartnerView = None  # last answer of the partner, for the alarms and the metrics
        self.partner_probed = False  # partner_view has been filled at least once (None then means: not reachable)
        self.split_brain = False
        self.check_interval = check_interval
        self.check_timeout = check_timeout
        self.retry_interval = retry_interval
        self.takeover_delay = check_interval + check_timeout + 2.0 if takeover_delay is None else takeover_delay
        self._connect = connect
        self._conn = None
        self._conn_lock = asyncio.Lock()  # one statement at a time on the lock connection
        self._tasks: dict[str, asyncio.Task] = {}
        self._lost = asyncio.Event()
        self._last_ok = 0.0
        self.role = STARTING

    # ── lock connection ──────────────────────────────────────────────────

    async def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            await asyncio.wait_for(conn.invalidate(), self.check_timeout)  # never back to the pool: it may hold the lock
        except Exception:
            pass
        try:
            await asyncio.wait_for(conn.close(), self.check_timeout)
        except Exception:
            pass

    async def _own_epoch(self) -> int:
        """Epoch in this node's database (the last known one if it cannot be read)."""
        if self._conn is None:
            return self._epoch
        try:
            async with self._conn_lock:
                value = await asyncio.wait_for(
                    self._conn.scalar(text("SELECT epoch FROM ha_state WHERE id = 1")), self.check_timeout)
                await self._conn.commit()
            self._epoch = int(value or 0)
        except Exception:
            try:
                await asyncio.wait_for(self._conn.rollback(), self.check_timeout)
            except Exception:
                pass
        return self._epoch

    async def _partner_objects(self, *, fresh: bool = False) -> bool:
        """True if the HA partner is a leader with an epoch at least as high as ours. Throttled; ``fresh`` asks anyway."""
        if self._partner is None:
            return False
        now = asyncio.get_running_loop().time()
        if not fresh and now - self._partner_at < self.partner_interval:
            return self._partner_objection
        view = await self._partner()
        self.partner_view = view
        self.partner_probed = True
        self._partner_at = asyncio.get_running_loop().time()
        objection = view is not None and view[0] == LEADER and view[1] >= await self._own_epoch()
        if objection != self._partner_objection:
            if objection:
                logger.error("Split-Brain: der Partner ist Leader (Epoche %s, eigene %s); dieser Knoten führt keine "
                             "Hintergrunddienste aus, bis der Partner gestoppt oder neu aufgebaut ist", view[1], self._epoch)
            else:
                logger.warning("Split-Brain-Wächter: der Partner ist nicht mehr Leader mit gleicher oder höherer Epoche")
        self._partner_objection = objection
        self.split_brain = objection
        return objection

    async def _try_acquire(self) -> str:
        """STANDBY (lock held elsewhere or database unreachable), REPLICA, FENCED (the HA partner is leader), or
        TAKEOVER (lock is ours)."""
        try:
            self._conn = await asyncio.wait_for(self._connect(), self.check_timeout)
            async with self._conn_lock:
                if await asyncio.wait_for(self._conn.scalar(text("SELECT pg_is_in_recovery()")), self.check_timeout):
                    await self._close()
                    self.split_brain = self._partner_objection = False
                    return REPLICA
        except Exception as exc:
            logger.warning("Leader-Sperre: Datenbank nicht erreichbar (%s)", type(exc).__name__)
            await self._close()
            return STANDBY
        if await self._partner_objects(fresh=True):
            await self._close()
            return FENCED
        try:
            async with self._conn_lock:
                got = await asyncio.wait_for(
                    self._conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": LOCK_KEY}), self.check_timeout)
                await self._conn.commit()  # leave no transaction open on a connection that lives for days
        except Exception as exc:
            logger.warning("Leader-Sperre: Datenbank nicht erreichbar (%s)", type(exc).__name__)
            await self._close()
            return STANDBY
        if not got:
            await self._close()
            return STANDBY
        self._last_ok = asyncio.get_running_loop().time()
        return TAKEOVER

    async def check(self) -> bool:
        """True while the lock connection answers and the database is still a primary. A False is final for this
        tenure: the caller demotes."""
        if self._conn is None:
            return False
        try:
            async with self._conn_lock:
                recovery = await asyncio.wait_for(self._conn.scalar(text("SELECT pg_is_in_recovery()")),
                                                  self.check_timeout)
                await self._conn.commit()
        except Exception as exc:
            logger.error("Leader-Sperre verloren: Verbindung zur Datenbank gestört (%s)", type(exc).__name__)
            self._lost.set()
            return False
        if recovery:
            logger.error("Leader-Sperre aufgegeben: die Datenbank ist jetzt eine Replica")
            self._lost.set()
            return False
        self._last_ok = asyncio.get_running_loop().time()
        return True

    async def checkpoint(self) -> None:
        """Called at the top of every loop iteration. Raises LeadershipLost unless this process is still the leader;
        a recent successful check (under a second old) is reused so a draining loop does not hammer the database."""
        if self.role != LEADER or self._lost.is_set():
            raise LeadershipLost()
        if asyncio.get_running_loop().time() - self._last_ok < 1.0:
            return
        if not await self.check():
            raise LeadershipLost()

    # ── loops ────────────────────────────────────────────────────────────

    def _start_loops(self) -> None:
        for name, factory in self._loops.items():
            self._tasks[name] = asyncio.create_task(factory(), name=f"loop:{name}")

    async def _stop_loops(self) -> None:
        tasks, self._tasks = list(self._tasks.values()), {}
        for task in tasks:
            task.cancel()
        # return_exceptions collects the loops' own CancelledError; a cancellation of THIS task still propagates.
        await asyncio.gather(*tasks, return_exceptions=True)

    def _restart_crashed(self) -> None:
        for name, task in list(self._tasks.items()):
            if not task.done() or task.cancelled():
                continue
            exc = task.exception()
            if isinstance(exc, LeadershipLost):
                self._lost.set()
                continue
            logger.error("Hintergrundschleife %s beendet (%s), wird neu gestartet", name,
                         type(exc).__name__ if exc else "ohne Fehler")
            self._tasks[name] = asyncio.create_task(self._loops[name](), name=f"loop:{name}")

    # ── supervisor ───────────────────────────────────────────────────────

    async def _wait_or_lost(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._lost.wait(), seconds)
        except asyncio.TimeoutError:
            pass

    async def _tenure(self) -> None:
        """From the moment the lock is ours until it is lost."""
        self.role = TAKEOVER
        logger.info("Leader-Sperre erhalten; Hintergrunddienste starten in %.0f s", self.takeover_delay)
        deadline = asyncio.get_running_loop().time() + self.takeover_delay
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            await self._wait_or_lost(min(left, self.check_interval))
            if self._lost.is_set() or not await self.check() or await self._partner_objects():
                return
        if await self._partner_objects(fresh=True):  # the last look before the loops start
            return
        self.role = LEADER
        self._start_loops()
        logger.info("Dieser Prozess führt die Hintergrunddienste aus (Leader)")
        while not self._lost.is_set():
            await self._wait_or_lost(self.check_interval)
            if self._lost.is_set() or not await self.check() or await self._partner_objects():
                return
            self._restart_crashed()

    async def run(self) -> None:
        announced = None
        while True:
            self._lost.clear()
            state = await self._try_acquire()
            if state == TAKEOVER:
                try:
                    await self._tenure()
                finally:
                    # Order matters: no loop may outlive the lock.
                    await self._stop_loops()
                    await self._close()
                    self.role = FENCED if self._partner_objection else STANDBY
                if self._partner_objection:
                    logger.warning("Hintergrunddienste angehalten (Split-Brain-Wächter); bewerbe mich später neu")
                else:
                    logger.warning("Hintergrunddienste angehalten (Leader-Sperre verloren); bewerbe mich neu")
                announced = None
            else:
                self.role = state
                if announced != state:
                    logger.info(
                        "Bereitschaft: %s", {
                            REPLICA: "die Datenbank ist eine Replica",
                            FENCED: "der HA-Partner ist Leader (Split-Brain-Wächter)",
                        }.get(state, "ein anderer Prozess führt die Hintergrunddienste aus oder die Datenbank fehlt"))
                    announced = state
            await asyncio.sleep(self.retry_interval)

    async def stop(self) -> None:
        """Shutdown: loops first, then the lock connection (closing it releases the lock)."""
        self._lost.set()
        await self._stop_loops()
        await self._close()
        self.role = STANDBY


# Set by main.lifespan; read by /api/health and the metrics.
gate: LeaderGate | None = None


def current_role() -> str:
    return gate.role if gate is not None else STARTING


async def checkpoint() -> None:
    """For the loops in main.py. Without a gate (tests that drive a loop directly) there is nothing to check."""
    if gate is not None:
        await gate.checkpoint()
