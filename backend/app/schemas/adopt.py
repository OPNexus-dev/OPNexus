import uuid

from pydantic import BaseModel, Field


class AdoptObjectRequest(BaseModel):
    host_id: uuid.UUID
    uuid: str = Field(min_length=1, max_length=64)  # the object's UUID on the firewall


class AdoptAllRequest(BaseModel):
    host_id: uuid.UUID


class UnmanagedObject(BaseModel):
    """An alias / NAT rule found on a host that OPNexus does not manage yet (see object_adopt_service.py)."""

    uuid: str
    title: str
    enabled: bool
    adoptable: bool  # can be adopted on its own right now
    # Adoptable in the same "adopt all" run although not on its own (an alias whose referenced aliases come first).
    bulk_adoptable: bool
    # Codes such as "unsupported_type" or "no_target"; the UI turns them into text.
    reasons: list[str]
    reason_details: dict[str, str] = Field(default_factory=dict)  # code -> extra text (alias names, type, ...)
    # Settings OPNexus does not manage that stay untouched on the firewall (log, pool options, tags, ...).
    kept: list[str] = Field(default_factory=list)
    # Facts for display, depend on the object type.
    info: dict[str, str] = Field(default_factory=dict)


class AdoptedObject(BaseModel):
    uuid: str
    title: str
    id: uuid.UUID


class SkippedObject(BaseModel):
    uuid: str
    title: str
    reasons: list[str]
    reason_details: dict[str, str] = Field(default_factory=dict)


class AdoptAllResult(BaseModel):
    adopted: list[AdoptedObject]
    skipped: list[SkippedObject]
