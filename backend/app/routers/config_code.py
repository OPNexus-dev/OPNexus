"""Konfiguration als Code (1.32): YAML-Export, Plan und Übernahme. Schreibt nur in die OPNexus-Datenbank.

Rechte (durchgesetzt in AuthMiddleware/api_token_service, hier nicht noch einmal):
* Export: jede angemeldete Rolle, Token-Scope `read` genügt (nichts, was die GET-Endpunkte nicht schon zeigen).
* Plan: jede Rolle inkl. Nur-Lese und `read`-Token; schreibt nichts (wie eine Vorschau, nicht im Audit).
* Übernehmen: nur Admins (Sitzung oder `admin`-Token): Massenänderung inkl. Prune und Host-Hierarchie-Zuordnung.
"""
import asyncio
import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config_code import apply as applier
from app.config_code import plan as planner
from app.config_code import schema
from app.config_code.export import export_yaml
from app.config_code.loader import MAX_BYTES, ConfigError, parse
from app.database import get_db
from app.errors import ErrorCode, api_error

router = APIRouter(prefix="/config", tags=["config"])

# JSON escapes can grow the YAML text (a quote becomes \", a control character \u001b): room for that, but bounded,
# and read before parsing so a huge body never reaches the JSON parser.
MAX_REQUEST_BYTES = 4 * MAX_BYTES
# One plan/apply at a time per process: the pure-Python YAML parser costs seconds for a full-size file, and plan is
# open to read-only accounts and read tokens.
_busy = asyncio.Semaphore(1)
# ... and a short queue: every waiting request holds its body (up to MAX_REQUEST_BYTES) in memory.
MAX_WAITING = 4
_waiting = 0


class _Slot:
    """Admission before the body is read (a waiting request holds its body in memory), then one at a time."""

    async def __aenter__(self):
        global _waiting
        if _waiting >= MAX_WAITING:
            raise api_error(429, ErrorCode.CONFIG_BUSY)
        _waiting += 1
        return self

    async def run(self, coro_fn):
        async with _busy:
            return await coro_fn()

    async def __aexit__(self, *exc):
        global _waiting
        _waiting -= 1


def _encode(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a JSON \ud800 escape
        raise api_error(422, ErrorCode.CONFIG_INVALID, errors=[{"code": "bad_encoding", "path": "", "line": None, "params": {}}]) from None


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    yaml: str
    prune: bool = False


class ApplyRequest(ImportRequest):
    plan_token: str = Field(min_length=64, max_length=64)


def _config_error(exc: ConfigError):
    status = 413 if exc.code == "CONFIG_TOO_LARGE" else 422
    return api_error(status, ErrorCode(exc.code), errors=exc.errors)


async def _read_body(request: Request, model: type[BaseModel]):
    if int(request.headers.get("content-length") or 0) > MAX_REQUEST_BYTES:
        raise api_error(413, ErrorCode.CONFIG_TOO_LARGE, errors=[{"code": "too_large", "path": "", "line": None, "params": {"max": MAX_BYTES}}])
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BYTES:
            raise api_error(413, ErrorCode.CONFIG_TOO_LARGE, errors=[{"code": "too_large", "path": "", "line": None, "params": {"max": MAX_BYTES}}])
        chunks.append(chunk)
    try:
        return model.model_validate(json.loads(b"".join(chunks)))
    except (ValueError, ValidationError):
        raise api_error(422, ErrorCode.CONFIG_INVALID, errors=[{"code": "bad_request", "path": "", "line": None, "params": {}}]) from None


@router.get("/export")
async def export(kinds: str | None = Query(default=None, max_length=300), db: AsyncSession = Depends(get_db)):
    wanted = None
    if kinds:
        wanted = {k.strip() for k in kinds.split(",") if k.strip()}
        if not wanted <= set(schema.KINDS):
            raise api_error(422, ErrorCode.CONFIG_INVALID, errors=[{"code": "unknown_kind", "path": "kinds", "line": None, "params": {}}])
    text = await export_yaml(db, wanted)
    return Response(content=text.encode("utf-8"), media_type="text/yaml; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="opnexus-config.yaml"', "Cache-Control": "no-store"})


@router.post("/import/plan")
async def plan(request: Request, db: AsyncSession = Depends(get_db)):
    async with _Slot() as slot:
        body = await _read_body(request, ImportRequest)
        raw = _encode(body.yaml)

        async def work():
            try:
                parsed = await run_in_threadpool(parse, raw)
                return await planner.build_plan(db, parsed, body.prune)
            except ConfigError as exc:
                raise _config_error(exc) from None
            finally:
                await db.rollback()  # never leave anything in the session: plan is read-only

        result = await slot.run(work)
    return result.public()


@router.post("/import/apply")
async def apply(request: Request, db: AsyncSession = Depends(get_db)):
    async with _Slot() as slot:
        body = await _read_body(request, ApplyRequest)
        raw = _encode(body.yaml)

        async def work():
            try:
                parsed = await run_in_threadpool(parse, raw)
                result = await applier.apply_config(db, parsed, body.prune, body.plan_token)
            except ConfigError as exc:
                await db.rollback()
                raise _config_error(exc) from None
            except applier.PlanStale:
                await db.rollback()
                raise api_error(409, ErrorCode.CONFIG_PLAN_STALE) from None
            except applier.PlanBlocked as exc:
                await db.rollback()
                raise api_error(409, ErrorCode.CONFIG_BLOCKED, summary=exc.plan.summary()) from None
            except applier.SelfCheckFailed as exc:
                await db.rollback()
                raise api_error(500, ErrorCode.CONFIG_APPLY_SELFCHECK, objects=exc.differences[:20]) from None
            except Exception:
                await db.rollback()
                raise
            await db.commit()
            return parsed, result

        parsed, result = await slot.run(work)
    return {"summary": result.summary(), "sha256": parsed.sha256, "kinds": result.touched}
