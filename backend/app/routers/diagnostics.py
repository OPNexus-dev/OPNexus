import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.services import diagnostics_service

router = APIRouter(prefix="/hosts/{host_id}/diagnostics", tags=["diagnostics"])
IpProto = Literal["inet", "inet6"]


class PingRequest(BaseModel):
    target: str = Field(min_length=1, max_length=253)
    ipproto: IpProto = "inet"
    count: int = Field(default=4, ge=1, le=10)
    packetsize: int | None = Field(default=None, ge=1, le=9000)
    source: str | None = Field(default=None, max_length=64)


class TracerouteRequest(BaseModel):
    target: str = Field(min_length=1, max_length=253)
    ipproto: IpProto = "inet"
    protocol: Literal["udp", "icmp"] = "icmp"
    source: str | None = Field(default=None, max_length=64)


class PortprobeRequest(BaseModel):
    target: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    ipproto: IpProto = "inet"
    source: str | None = Field(default=None, max_length=64)


class DnsRequest(BaseModel):
    name: str = Field(min_length=1, max_length=253)
    server: str | None = Field(default=None, max_length=64)


async def _host(db: AsyncSession, host_id: uuid.UUID) -> OpnsenseHost:
    host = await db.get(OpnsenseHost, host_id)
    if not host:
        raise api_error(404, ErrorCode.HOST_NICHT_GEFUNDEN)
    return host


@router.post("/ping")
async def ping(host_id: uuid.UUID, p: PingRequest, db: AsyncSession = Depends(get_db)):
    return await diagnostics_service.ping(await _host(db, host_id), p.target, p.ipproto, p.count, p.packetsize, p.source)


@router.post("/traceroute")
async def traceroute(host_id: uuid.UUID, p: TracerouteRequest, db: AsyncSession = Depends(get_db)):
    return await diagnostics_service.traceroute(await _host(db, host_id), p.target, p.ipproto, p.protocol, p.source)


@router.post("/portprobe")
async def portprobe(host_id: uuid.UUID, p: PortprobeRequest, db: AsyncSession = Depends(get_db)):
    return await diagnostics_service.portprobe(await _host(db, host_id), p.target, p.port, p.ipproto, p.source)


@router.post("/dns")
async def dns(host_id: uuid.UUID, p: DnsRequest, db: AsyncSession = Depends(get_db)):
    return await diagnostics_service.dns_lookup(await _host(db, host_id), p.name, p.server)
