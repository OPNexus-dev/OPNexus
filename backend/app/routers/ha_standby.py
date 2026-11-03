"""The standby's own door (docs/DESIGN-HA-UI.md section 4): sign in without writing, see the pair, take over.

Only answers on a node of an HA pair whose database is the streaming replica (`NOT_STANDBY` otherwise). Everything else on this
node is refused by `StandbyNodeMiddleware`. The takeover itself is not done here: the updater container on this node runs
`opnexus ha promote` (it has the Docker socket, the backend never does), and this router decides whether it may and in which mode.
"""
import logging

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import ha_check, leader
from app.config import settings
from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.user import AdminUser
from app.routers.auth import LoginRequest, client_ip
from app.services import ha_service, standby_session, updater_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ha/standby", tags=["ha-standby"])

# The sentence that has to be typed to take over although the partner cannot be asked to stop (same as `opnexus ha promote --force`).
FORCE_SENTENCE = "alter Primaer ist aus"


def _require_standby() -> None:
    if not ha_service.configured() or leader.current_role() != leader.REPLICA:
        raise api_error(409, ErrorCode.NOT_STANDBY)


def _user(request: Request) -> str:
    username = standby_session.verify(request.cookies.get(standby_session.cookie_name()))
    if username is None:
        raise api_error(401, ErrorCode.NICHT_AUTHENTIFIZIERT)
    return username


def decide_promote(agent_reachable: bool | None, partner_health: tuple[str, int] | None) -> str:
    """How a takeover would go right now.

    - normal:      the partner's agent answers, so `ha promote` can stop it first (fencing) and nothing is lost;
    - force:       neither the agent nor the partner's proxy answers: the partner is down or cut off. Allowed, but only after the
                   operator confirmed in writing that it is really off (two primaries with access to the same firewalls do more damage
                   than an unreachable OPNexus);
    - refused:     the agent is silent but the partner still answers as leader: it is alive, a takeover would make two primaries;
    - unavailable: this node's own updater cannot be asked (not enabled or down).
    """
    if agent_reachable is None:
        return "unavailable"
    if agent_reachable:
        return "normal"
    if partner_health is not None and partner_health[0] == "leader":
        return "refused"
    return "force"


async def _agent() -> dict | None:
    try:
        return await updater_client.ha_local_status()
    except (updater_client.UpdaterUnavailable, updater_client.UpdaterRejected):
        return None


def _agent_reachable(agent: dict | None) -> bool | None:
    """Does the partner's agent answer? None = cannot be asked: this node's updater is silent, or the pair still runs over SSH (the updater
    holds no SSH keys, so it cannot fence the partner; the takeover then goes through `opnexus ha promote` in a terminal)."""
    if agent is None or agent.get("paired") is False:
        return None
    return bool(agent.get("peer_reachable"))


async def _partner_health() -> tuple[str, int] | None:
    probe = leader.http_partner(settings.ha_peer_ip)
    return await probe() if probe else None


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    _require_standby()
    username = await standby_session.authenticate(db, payload.username, payload.password, payload.totp_code, client_ip(request))
    response.set_cookie(key=standby_session.cookie_name(), value=standby_session.issue(username), max_age=standby_session.TTL_SECONDS,
                        httponly=True, samesite="strict", secure=settings.cookie_secure, path="/")
    logger.info("Standby sign-in of '%s' from %s", username, client_ip(request))
    return {"username": username, "expires_in": standby_session.TTL_SECONDS}


@router.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(key=standby_session.cookie_name(), path="/", secure=settings.cookie_secure, httponly=True, samesite="strict")
    return {"ok": True}


@router.get("/session")
async def session(request: Request) -> dict:
    _require_standby()
    return {"username": _user(request)}


@router.get("/status")
async def status(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    _require_standby()
    username = _user(request)
    snap = await ha_service.collect(db)
    replay = (await db.execute(text("SELECT extract(epoch from now() - pg_last_xact_replay_timestamp())"))).scalar()
    missing = await ha_check.missing()
    agent = await _agent()
    health = await _partner_health()
    reachable = _agent_reachable(agent)
    now = ha_service._now()
    return {
        "username": username,
        "node": settings.ha_node,
        "peer_ip": settings.ha_peer_ip,
        "vip": settings.ha_vip.split("/")[0] or None,
        "epoch": snap.epoch,
        "primary_node": snap.primary_node,
        "replication": {"state": snap.replication_state, "lag_bytes": snap.replication_lag_bytes, "lag_seconds": snap.replication_lag_seconds},
        "last_replay_age_seconds": None if replay is None else max(0, int(replay)),
        "sync_age_seconds": None if snap.last_sync_at is None else max(0, int((now - snap.last_sync_at).total_seconds())),
        "secrets": {"complete": not (missing["hosts"] or missing["totp"]), "hosts": missing["hosts"], "totp": missing["totp"]},
        "partner": {"agent": reachable, "health": None if health is None else {"role": health[0], "epoch": health[1]}},
        "promote": {"mode": decide_promote(reachable, health), "force_sentence": FORCE_SENTENCE},
        "job": (agent or {}).get("job"),
    }


class PromoteRequest(BaseModel):
    force: bool = False
    confirm: str = Field(default="", max_length=100)


@router.post("/promote", status_code=202)
async def promote(payload: PromoteRequest, request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    _require_standby()
    username = _user(request)
    # The audit entry is written later by the new primary, so the updater gets the account's id (a fixed, harmless form) and not the free-form
    # user name; `app.ha_audit` looks the name up again. An account deleted since the sign-in cannot take over.
    actor = (await db.execute(select(AdminUser.id).where(AdminUser.username == username))).scalar_one_or_none()
    if actor is None:
        raise api_error(401, ErrorCode.NICHT_AUTHENTIFIZIERT)
    agent = await _agent()
    reachable = _agent_reachable(agent)
    mode = decide_promote(reachable, await _partner_health())
    if mode == "unavailable":
        raise api_error(503, ErrorCode.UPDATER_UNAVAILABLE)
    if mode == "refused":
        raise api_error(409, ErrorCode.PEER_IS_LEADER)
    if mode == "force" and not (payload.force and payload.confirm.strip() == FORCE_SENTENCE):
        raise api_error(400, ErrorCode.STANDBY_CONFIRMATION_REQUIRED, sentence=FORCE_SENTENCE)
    try:
        return await updater_client.ha_local_promote(force=(mode == "force"), actor=str(actor))
    except updater_client.UpdaterUnavailable:
        raise api_error(503, ErrorCode.UPDATER_UNAVAILABLE)
    except updater_client.UpdaterRejected as rejected:
        if rejected.code == "busy":
            raise api_error(409, ErrorCode.UPDATER_BUSY)
        raise api_error(409, ErrorCode.UPDATER_REJECTED, reason=rejected.code)


@router.get("/promote/job")
async def promote_job(request: Request) -> dict:
    _user(request)
    try:
        status_ = await updater_client.ha_local_status()
    except (updater_client.UpdaterUnavailable, updater_client.UpdaterRejected):
        raise api_error(503, ErrorCode.UPDATER_UNAVAILABLE)
    return {"job": status_.get("job"), "log": status_.get("log") or []}
