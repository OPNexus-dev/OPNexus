"""The three "take over what exists on the firewall" endpoints that aliases, port forwards, 1:1 NAT and
outbound NAT share (services/object_adopt_service.py has the logic and the evidence):

  GET  <prefix>/unmanaged?host_id=…   candidates found live on the host, with reasons for the refused ones
  POST <prefix>/adopt                 take over one object {host_id, uuid}; returns the new central object
  POST <prefix>/adopt-all             take over everything adoptable {host_id}; returns adopted + skipped

Like rule adoption this only creates central records; nothing is written to the firewall."""
import json
import uuid
from typing import Callable

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.schemas.adopt import AdoptAllRequest, AdoptAllResult, AdoptObjectRequest, UnmanagedObject
from app.services import object_adopt_service
from app.services.adopt_common import LINK_ERRORS, link_api_error
from app.services.opnsense_client import OpnsenseAPIError
from app.services.policy_hierarchy_target_service import hydrate_hierarchy_targets
from app.services.policy_service import policy_lock


async def _host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


def add_adopt_routes(router: APIRouter, *, kind: str, model, load_options: Callable, to_read: Callable, read_model) -> None:
    @router.get("/unmanaged", response_model=list[UnmanagedObject])
    async def list_unmanaged(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
        host = await _host(db, host_id)
        try:
            return await object_adopt_service.list_unmanaged(db, kind, host)
        except OpnsenseAPIError as exc:
            raise api_error(502, ErrorCode.HOST_REQUEST_FAILED, host=host.name) from exc

    @router.post("/adopt", response_model=read_model, status_code=201)
    async def adopt(payload: AdoptObjectRequest, db: AsyncSession = Depends(get_db)):
        await policy_lock(db)
        host = await _host(db, payload.host_id)
        try:
            created = await object_adopt_service.adopt(db, kind, host, payload.uuid, link_to=payload.link_to)
        except object_adopt_service.NotFoundOnHost as exc:
            raise api_error(404, ErrorCode.ADOPT_OBJECT_NOT_FOUND) from exc
        except object_adopt_service.NotAdoptable as exc:
            raise api_error(409, ErrorCode.OBJECT_NOT_ADOPTABLE, reasons=", ".join(exc.reasons),
                            reason_details=json.dumps(exc.details, ensure_ascii=False)) from exc
        except LINK_ERRORS as exc:
            raise link_api_error(exc) from exc
        except OpnsenseAPIError as exc:
            raise api_error(502, ErrorCode.HOST_REQUEST_FAILED, host=host.name) from exc
        await db.commit()
        # The new object sits in the session without its relationships loaded; db.get() would return it as-is.
        created = (await db.execute(
            select(model).where(model.id == created.id).options(*load_options())
            .execution_options(populate_existing=True)
        )).scalar_one()
        await hydrate_hierarchy_targets(db, [created])
        return to_read(created, created.targets, created.deployments)

    @router.post("/adopt-all", response_model=AdoptAllResult)
    async def adopt_all(payload: AdoptAllRequest, db: AsyncSession = Depends(get_db)):
        await policy_lock(db)
        host = await _host(db, payload.host_id)
        try:
            result = await object_adopt_service.adopt_all(db, kind, host, link_existing=payload.link_existing)
        except LINK_ERRORS as exc:
            raise link_api_error(exc) from exc
        except OpnsenseAPIError as exc:
            raise api_error(502, ErrorCode.HOST_REQUEST_FAILED, host=host.name) from exc
        await db.commit()
        return result
