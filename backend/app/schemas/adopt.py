import uuid

from pydantic import BaseModel, Field


class AdoptObjectRequest(BaseModel):
    host_id: uuid.UUID
    # Link to this existing central object instead of creating a new one (services/adopt_link_service.py).
    # Declared before `uuid`: that field's name shadows the module inside the class body.
    link_to: uuid.UUID | None = None
    uuid: str = Field(min_length=1, max_length=64)  # the object's UUID on the firewall


class AdoptAllRequest(BaseModel):
    host_id: uuid.UUID
    # Link entries that have exactly one exact central twin instead of creating a duplicate.
    link_existing: bool = False


class LinkDiff(BaseModel):
    field: str  # model field ("content", "destination_port", ...) or "variables" (a {{VARIABLE}} has no value on this host)
    central: str
    live: str


class LinkTarget(BaseModel):
    """An existing central object that a live object could be linked to (exact match) or is similar to."""

    id: uuid.UUID
    name: str
    targets: list[str]  # names of the hosts it is deployed to
    diff: list[LinkDiff] = Field(default_factory=list)  # empty for an exact match


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
    # Central objects whose content, resolved for this host, equals the live object exactly (first = preferred).
    matches: list[LinkTarget] = Field(default_factory=list)
    # Objects with the same name/description but different content: a hint with the differing fields, never linked.
    similar: list[LinkTarget] = Field(default_factory=list)
    linkable: bool = False  # can be linked to `matches[0]` (or another match) on its own
    # What "adopt all" would do: "link" | "new" | "skip" (None only on the adopt-all result).
    bulk_action: str | None = None


class AdoptedObject(BaseModel):
    uuid: str
    title: str
    id: uuid.UUID
    linked: bool = False  # linked to an existing central object instead of newly created


class SkippedObject(BaseModel):
    uuid: str
    title: str
    reasons: list[str]
    reason_details: dict[str, str] = Field(default_factory=dict)


class AdoptAllResult(BaseModel):
    adopted: list[AdoptedObject]
    skipped: list[SkippedObject]
