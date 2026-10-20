"""Takes over aliases, port forwards, 1:1 NAT and outbound NAT rules that were created by hand on a
firewall, so they can be edited, versioned and deployed from OPNexus like any other object. This is
the generalisation of rule_adopt_service.py (filter rules keep their own module: their model has
more special cases and the code is relied upon).

Adoption itself writes NOTHING to the firewall. It records the object as it is right now (read fresh
from the host at adoption time, never from what the browser sent) together with its OPNsense UUID
(`remote_uuid` = the deployment's opnsense_*_uuid), and stores the payload OPNexus would send for it as
the deployment's last_payload. So the preview directly afterwards says "unchanged", a later deploy
updates the same object via setItem/setRule (no duplicate), and the normal preview → push →
confirm/auto-revert flow applies from the first change on.

Only objects OPNexus can describe without distortion are adopted. Anything the model cannot express
is refused with machine-readable reasons (the UI translates them) instead of being imported in a way
that would silently change its meaning on the next edit. Fields OPNexus does not manage (log flag of a
port forward, pool options, tags, categories, ...) are never sent, and OPNsense's set*/setItem only
overwrites the posted fields (ApiMutableModelControllerBase::setBase -> setNodes), so they stay as
they are; the candidate list shows them as `kept`.

Evidence (OPNsense core master, src/opnsense/mvc/app/…, read 2026-10-20, and live against OPNsense
26.7 on test-fw-3 on the same day with hand-made objects of every case below):

* Aliases — controllers/OPNsense/Firewall/Api/AliasController.php::searchItemAction -> searchBase
  ("aliases.alias"), model models/OPNsense/Firewall/Alias.xml. Row keys used: uuid, name, type
  ("host", "network", "port", "networkgroup", "mac", "asn", plus the unsupported "url", "urltable",
  "urljson", "geoip", "dynipv6host", "authgroup", and the system types "internal"/"external"),
  enabled "1"/"0", content (entries separated by "\n", live-verified; for "authgroup" the controller
  rewrites it to group names), description, proto (only meaningful for asn/geoip). The built-in rows
  are bogons, bogonsv6, sshlockout, virusprot (type external) and one __<if>_network per interface
  (type internal); they are OPNsense's own and never adoptable. A "networkgroup" lists alias NAMES;
  host/network/port aliases may also contain alias names (nesting).
* Port forward — controllers/OPNsense/Firewall/Api/DNatController.php::searchRuleAction merges the
  configured rules (getFlatNodes: dotted keys "source.network", "source.port", "source.not",
  "destination.network", "destination.port", "destination.not", plus descr, disabled, interface,
  ipprotocol, protocol, target, local-port, natreflection, nordr, pass, log, poolopts, tag, tagged,
  nosync, categories, sequence) with synthetic automatic rows (uuid "automatic_*"/"lockout_*",
  is_automatic true) that setRule/delRule cannot target. models/OPNsense/Firewall/DNat.xml: interface is
  Multiple=Y (comma separated), ipprotocol may be blank/inet/inet6/inet46, protocol blank = any,
  `nordr` ("no redirect") requires an empty target/local-port (live: "Target must be empty when No
  RDR is set"), `pass` ("" manual / "pass" / "rule") links an associated filter rule, natreflection is
  ""/purenat/disable. OPNexus sends pass "" and not "0", so a rule using one of those would change
  meaning on its first edit and is refused.
* 1:1 NAT — OneToOneController::searchRuleAction (flat keys), model Filter.xml onetoone.rule:
  enabled, log, interface, type (binat/nat), source_net/source_not, destination_net/destination_not,
  external, natreflection ""/enable/disable, description. All of these are managed by OPNexus.
* Outbound NAT — SourceNatController::searchRuleAction returns the configured rules ONLY when the
  global mode (filter.settings.nat.snat_mode) is hybrid or advanced; in automatic/disabled mode the
  manual rules do not show up at all, so adopting is refused up front (outbound_nat_mode.py) instead
  of presenting an empty list. Model Filter.xml snatrules.rule: `nonat` rules ("do not NAT") are not
  expressible and refused; staticnatport, poolopts, log, tag, tagged, nosync, endpoint-independent
  are left alone.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, get_args

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.models.alias import FirewallAlias
from app.models.alias_deployment import AliasDeployment
from app.models.host import OpnsenseHost
from app.models.one_to_one import OneToOneRule
from app.models.one_to_one_deployment import OneToOneDeployment
from app.models.outbound_nat import OutboundNatRule
from app.models.outbound_nat_deployment import OutboundNatDeployment
from app.models.port_forward import PortForwardRule
from app.models.port_forward_deployment import PortForwardDeployment
from app.schemas.alias import AliasType
from app.services import alias_deploy_service, alias_policy_service
from app.services import one_to_one_deploy_service, one_to_one_policy_service
from app.services import opnsense_client, outbound_nat_deploy_service, outbound_nat_policy_service
from app.services import port_forward_deploy_service, port_forward_policy_service
from app.services.adopt_common import NotAdoptable, NotFoundOnHost, clean, on
from app.services.nat_live_service import split_rows
from app.services.outbound_nat_mode import host_mode

ADOPT_NOTE = "Von Firewall übernommen"
ADOPT_MESSAGE = "Von der Firewall übernommen"

ALIAS_NAME = re.compile(r"^[A-Za-z0-9_]+$")  # schemas/alias.py AliasCreate.name
ALIAS_TYPES = frozenset(get_args(AliasType))
ALIAS_CONTENT_MAX = 8000  # schemas/alias.py AliasCreate.content
# OPNsense's own aliases that are not user objects (Firewall/Alias.php: bogons & co. are `external`
# rows created by the system, __<if>_network are `internal`).
SYSTEM_ALIAS_NAMES = frozenset({"bogons", "bogonsv6", "sshlockout", "virusprot"})


@dataclass
class Entry:
    row: dict
    reasons: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)
    bulk: bool = False  # not adoptable alone yet, but adoptable in the same run (its references come first)

    @property
    def uuid(self) -> str:
        return clean(self.row.get("uuid"))


@dataclass(frozen=True)
class Kind:
    key: str
    model: type
    deployment: type
    fk: str  # deployment column pointing at the central object
    uuid_attr: str  # deployment column holding the OPNsense UUID (= remote_uuid)
    fetch: Callable[[OpnsenseHost], Awaitable[list[dict]]]
    check: Callable[[dict, dict], tuple[list[str], dict[str, str]]]
    title: Callable[[dict], str]
    info: Callable[[dict], dict[str, str]]
    build: Callable[[dict], dict]
    payload: Callable[[Any], dict]
    revision: Callable
    kept: Callable[[dict], list[str]] = lambda row: []
    context: Callable[[AsyncSession, list[dict]], Awaitable[dict]] | None = None
    refs: Callable[[dict], set[str]] | None = None
    name: Callable[[dict], str] | None = None


# ── shared helpers ───────────────────────────────────────────────────────

def _single_interface(row: dict) -> bool:
    interface = clean(row.get("interface"))
    return bool(interface) and "," not in interface


def _too_long(kind_model: type, values: dict) -> list[str]:
    """Columns are bounded; a live value that does not fit would fail (or be cut) on the first edit."""
    columns = kind_model.__table__.columns
    return [name for name, value in values.items()
            if name in columns and getattr(columns[name].type, "length", None)
            and isinstance(value, str) and len(value) > columns[name].type.length]


def _port(value) -> str | None:
    return clean(value) or None


def _kept(row: dict, keys: tuple[str, ...]) -> list[str]:
    return [key for key in keys if clean(row.get(key)) not in ("", "0")]


def _fetch_nat(search) -> Callable[[OpnsenseHost], Awaitable[list[dict]]]:
    async def fetch(host: OpnsenseHost) -> list[dict]:
        data = await search(host)
        # split_rows drops automatic_*/lockout_* and is_automatic rows: they are OPNsense's own
        return [r for r in split_rows(data)["rules"] if clean(r.get("uuid"))]
    return fetch


# ── aliases ──────────────────────────────────────────────────────────────

def _alias_content(row: dict) -> str:
    # same normalisation as AliasCreate.clean_content
    return "\n".join(line.strip() for line in str(row.get("content") or "").splitlines() if line.strip())


def _alias_is_system(row: dict) -> bool:
    name = clean(row.get("name"))
    return clean(row.get("type")) == "internal" or name.startswith("__") or name in SYSTEM_ALIAS_NAMES


async def _fetch_aliases(host: OpnsenseHost) -> list[dict]:
    data = await opnsense_client.search_aliases(host)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [r for r in rows if isinstance(r, dict) and clean(r.get("uuid")) and clean(r.get("name"))]


async def _alias_context(db: AsyncSession, rows: list[dict]) -> dict:
    return {"central_names": set((await db.scalars(select(FirewallAlias.name))).all())}


def _alias_refs(row: dict) -> set[str]:
    return {line.lstrip("!").strip() for line in _alias_content(row).splitlines()} - {""}


def _alias_check(row: dict, ctx: dict) -> tuple[list[str], dict[str, str]]:
    if _alias_is_system(row):
        return ["system"], {}
    reasons: list[str] = []
    details: dict[str, str] = {}
    name, type_ = clean(row.get("name")), clean(row.get("type"))
    if type_ not in ALIAS_TYPES:
        reasons.append("unsupported_type")
        details["unsupported_type"] = clean(row.get("%type")) or type_
    if not ALIAS_NAME.fullmatch(name) or len(name) > 64:
        reasons.append("name_invalid")
    if name in ctx["central_names"]:
        reasons.append("name_exists")
    if len(_alias_content(row)) > ALIAS_CONTENT_MAX:
        reasons.append("content_too_large")
    if type_ == "asn" and clean(row.get("proto")):
        reasons.append("proto_set")  # OPNexus has no field for it: a deploy elsewhere would drop the restriction
    if _too_long(FirewallAlias, {"description": clean(row.get("description"))}):
        reasons.append("value_too_long")
    return reasons, details


def _alias_info(row: dict) -> dict[str, str]:
    lines = _alias_content(row).splitlines()
    return {
        "type": clean(row.get("type")),
        "type_label": clean(row.get("%type")) or clean(row.get("type")),
        "description": clean(row.get("description")),
        "content": "\n".join(lines[:6]) + ("\n…" if len(lines) > 6 else ""),
        "entries": str(len(lines)),
    }


def _alias_build(row: dict) -> dict:
    return {
        "name": clean(row.get("name")),
        "type": clean(row.get("type")),
        "content": _alias_content(row),
        "description": clean(row.get("description")),
        "enabled": on(row.get("enabled")),
    }


# ── port forward ─────────────────────────────────────────────────────────

def _pf_check(row: dict, ctx: dict) -> tuple[list[str], dict[str, str]]:
    reasons = []
    if not _single_interface(row):
        reasons.append("interface_not_single")
    if clean(row.get("ipprotocol")) not in ("inet", "inet6"):
        reasons.append("ipprotocol_unsupported")  # blank / inet46 have no counterpart in OPNexus
    if not clean(row.get("protocol")):
        reasons.append("protocol_any")
    if on(row.get("nordr")):
        reasons.append("no_redirect")
    elif not clean(row.get("target")):
        reasons.append("no_target")
    if clean(row.get("pass")):
        reasons.append("filter_rule_linked")  # OPNexus sends pass "" (manual) and would cut the link
    if on(row.get("source.not")):
        reasons.append("negated_source")
    if on(row.get("destination.not")):
        reasons.append("negated_destination")
    if clean(row.get("natreflection")) not in ("", "purenat", "disable"):
        reasons.append("natreflection_unsupported")
    if not clean(row.get("descr")):
        reasons.append("no_description")
    if _too_long(PortForwardRule, _pf_build(row)):
        reasons.append("value_too_long")
    return reasons, {}


def _pf_build(row: dict) -> dict:
    return {
        "description": clean(row.get("descr")),
        "interface": clean(row.get("interface")),
        "ipprotocol": clean(row.get("ipprotocol")),
        "protocol": clean(row.get("protocol")),
        "source_net": clean(row.get("source.network")) or "any",
        "source_port": _port(row.get("source.port")),
        "destination_net": clean(row.get("destination.network")) or "any",
        "destination_port": _port(row.get("destination.port")),
        "target": clean(row.get("target")),
        "local_port": _port(row.get("local-port")),
        "natreflection": clean(row.get("natreflection")),
        "enabled": not on(row.get("disabled")),
    }


def _pf_info(row: dict) -> dict[str, str]:
    return {
        "interface_label": clean(row.get("%interface")) or clean(row.get("interface")),
        "protocol": clean(row.get("%protocol")) or clean(row.get("protocol")) or "any",
        "source": clean(row.get("source.network")) or "any",
        "source_port": clean(row.get("source.port")),
        "destination": clean(row.get("destination.network")) or "any",
        "destination_port": clean(row.get("destination.port")),
        "target": clean(row.get("target")),
        "target_port": clean(row.get("local-port")),
    }


# ── 1:1 NAT ──────────────────────────────────────────────────────────────

def _o2o_check(row: dict, ctx: dict) -> tuple[list[str], dict[str, str]]:
    reasons = []
    if not _single_interface(row):
        reasons.append("interface_not_single")
    if clean(row.get("type")) not in ("binat", "nat"):
        reasons.append("type_unsupported")
    if clean(row.get("natreflection")) not in ("", "enable", "disable"):
        reasons.append("natreflection_unsupported")
    if not clean(row.get("source_net")) or not clean(row.get("external")):
        reasons.append("incomplete")
    if not clean(row.get("description")):
        reasons.append("no_description")
    if _too_long(OneToOneRule, _o2o_build(row)):
        reasons.append("value_too_long")
    return reasons, {}


def _o2o_build(row: dict) -> dict:
    return {
        "description": clean(row.get("description")),
        "interface": clean(row.get("interface")),
        "source_net": clean(row.get("source_net")),
        "destination_net": clean(row.get("destination_net")) or "any",
        "source_not": on(row.get("source_not")),
        "destination_not": on(row.get("destination_not")),
        "enabled": on(row.get("enabled")),
        "type": clean(row.get("type")),
        "external": clean(row.get("external")),
        "natreflection": clean(row.get("natreflection")),
        "log": on(row.get("log")),
    }


def _o2o_info(row: dict) -> dict[str, str]:
    return {
        "interface_label": clean(row.get("%interface")) or clean(row.get("interface")),
        "type": clean(row.get("type")),
        "source": ("! " if on(row.get("source_not")) else "") + clean(row.get("source_net")),
        "destination": ("! " if on(row.get("destination_not")) else "") + (clean(row.get("destination_net")) or "any"),
        "external": clean(row.get("external")),
    }


# ── outbound NAT ─────────────────────────────────────────────────────────

async def _fetch_outbound(host: OpnsenseHost) -> list[dict]:
    # In automatic/disabled mode OPNsense does not list manual rules at all (see module docstring):
    # say so instead of showing an empty list.
    state = await host_mode(host)
    if not state["can_deploy"]:
        raise api_error(409, ErrorCode.OUTBOUND_MODE_BLOCKS_ADOPT, host=host.name, mode=state["mode"])
    return await _fetch_nat(opnsense_client.search_outbound_nat_rules)(host)


def _snat_check(row: dict, ctx: dict) -> tuple[list[str], dict[str, str]]:
    reasons = []
    if not _single_interface(row):
        reasons.append("interface_not_single")
    if on(row.get("nonat")):
        reasons.append("no_nat_rule")
    if clean(row.get("ipprotocol")) not in ("inet", "inet6"):
        reasons.append("ipprotocol_unsupported")
    if not clean(row.get("protocol")):
        reasons.append("protocol_missing")
    if not clean(row.get("description")):
        reasons.append("no_description")
    if _too_long(OutboundNatRule, _snat_build(row)):
        reasons.append("value_too_long")
    return reasons, {}


def _snat_build(row: dict) -> dict:
    return {
        "description": clean(row.get("description")),
        "interface": clean(row.get("interface")),
        "source_net": clean(row.get("source_net")) or "any",
        "destination_net": clean(row.get("destination_net")) or "any",
        "source_not": on(row.get("source_not")),
        "destination_not": on(row.get("destination_not")),
        "enabled": on(row.get("enabled")),
        "ipprotocol": clean(row.get("ipprotocol")),
        "protocol": clean(row.get("protocol")),
        "source_port": _port(row.get("source_port")),
        "destination_port": _port(row.get("destination_port")),
        "target": clean(row.get("target")),
        "target_port": _port(row.get("target_port")),
    }


def _snat_info(row: dict) -> dict[str, str]:
    return {
        "interface_label": clean(row.get("%interface")) or clean(row.get("interface")),
        "protocol": clean(row.get("protocol")) or "any",
        "source": ("! " if on(row.get("source_not")) else "") + (clean(row.get("source_net")) or "any"),
        "source_port": clean(row.get("source_port")),
        "destination": ("! " if on(row.get("destination_not")) else "") + (clean(row.get("destination_net")) or "any"),
        "destination_port": clean(row.get("destination_port")),
        "target": clean(row.get("target")),
        "target_port": clean(row.get("target_port")),
    }


KINDS: dict[str, Kind] = {
    "alias": Kind(
        key="alias", model=FirewallAlias, deployment=AliasDeployment, fk="alias_id",
        uuid_attr="opnsense_alias_uuid", fetch=_fetch_aliases, check=_alias_check,
        title=lambda row: clean(row.get("name")), info=_alias_info, build=_alias_build,
        payload=alias_deploy_service._build_payload, revision=alias_policy_service.revision,
        context=_alias_context, refs=_alias_refs, name=lambda row: clean(row.get("name")),
    ),
    "port_forward": Kind(
        key="port_forward", model=PortForwardRule, deployment=PortForwardDeployment, fk="port_forward_id",
        uuid_attr="opnsense_port_forward_uuid", fetch=_fetch_nat(opnsense_client.search_port_forward_rules),
        check=_pf_check, title=lambda row: clean(row.get("descr")), info=_pf_info, build=_pf_build,
        payload=port_forward_deploy_service._build_payload, revision=port_forward_policy_service.revision,
        kept=lambda row: _kept(row, ("log", "poolopts", "tag", "tagged", "nosync", "categories")),
    ),
    "one_to_one": Kind(
        key="one_to_one", model=OneToOneRule, deployment=OneToOneDeployment, fk="one_to_one_id",
        uuid_attr="opnsense_one_to_one_uuid", fetch=_fetch_nat(opnsense_client.search_one_to_one_rules),
        check=_o2o_check, title=lambda row: clean(row.get("description")), info=_o2o_info, build=_o2o_build,
        payload=one_to_one_deploy_service._build_payload, revision=one_to_one_policy_service.revision,
        kept=lambda row: _kept(row, ("categories",)),
    ),
    "outbound_nat": Kind(
        key="outbound_nat", model=OutboundNatRule, deployment=OutboundNatDeployment, fk="outbound_nat_id",
        uuid_attr="opnsense_outbound_nat_uuid", fetch=_fetch_outbound,
        check=_snat_check, title=lambda row: clean(row.get("description")), info=_snat_info, build=_snat_build,
        payload=outbound_nat_deploy_service._build_payload, revision=outbound_nat_policy_service.revision,
        kept=lambda row: _kept(row, ("log", "staticnatport", "poolopts", "poolopts_sourcehashkey", "tag", "tagged",
                                      "nosync", "endpoint-independent", "categories")),
    ),
}


# ── engine ───────────────────────────────────────────────────────────────

async def _managed_uuids(db: AsyncSession, kind: Kind, host: OpnsenseHost) -> set[str]:
    column = getattr(kind.deployment, kind.uuid_attr)
    rows = await db.scalars(select(column).where(kind.deployment.host_id == host.id, column.is_not(None)))
    return set(rows.all())


def _resolve_references(kind: Kind, entries: list[Entry], rows: list[dict], managed: set[str], ctx: dict) -> list[Entry]:
    """Aliases may refer to other aliases by name. A reference to a live alias OPNexus does not manage
    (and that is not OPNsense's own) would be dangling in the central model, so the referring alias
    waits until its target is managed. Returns the entries in an order in which they can be adopted
    in ONE run (dependencies first); entries whose references can never be satisfied (target is a
    url alias, a user-defined external alias, ...) keep the reason `references_unmanaged_alias`."""
    live = {kind.name(r): r for r in rows}
    available = set(ctx["central_names"])
    available |= {n for n, r in live.items() if _alias_is_system(r) or clean(r.get("uuid")) in managed}

    candidates = [e for e in entries if not e.reasons]
    for entry in candidates:
        missing = {ref for ref in kind.refs(entry.row) if ref in live and ref not in available}
        if missing:
            entry.reasons.append("references_unmanaged_alias")
            entry.details["references_unmanaged_alias"] = ", ".join(sorted(missing))

    order: list[Entry] = []
    ready = set(available)
    progress = True
    while progress:
        progress = False
        for entry in candidates:
            if entry in order:
                continue
            if all(ref not in live or ref in ready for ref in kind.refs(entry.row)):
                order.append(entry)
                ready.add(kind.name(entry.row))
                progress = True
    for entry in order:
        entry.bulk = "references_unmanaged_alias" in entry.reasons
    return order


async def _plan(db: AsyncSession, kind: Kind, host: OpnsenseHost) -> tuple[list[Entry], list[Entry]]:
    """(all unmanaged entries with their reasons, the ones adoptable in one run in a safe order)."""
    rows = await kind.fetch(host)
    managed = await _managed_uuids(db, kind, host)
    ctx = await kind.context(db, rows) if kind.context else {}
    entries = []
    for row in rows:
        if clean(row.get("uuid")) in managed:
            continue
        reasons, details = kind.check(row, ctx)
        entries.append(Entry(row, list(reasons), dict(details)))
    if kind.refs:
        order = _resolve_references(kind, entries, rows, managed, ctx)
    else:
        order = [e for e in entries if not e.reasons]
    return entries, order


def _describe(kind: Kind, entry: Entry) -> dict:
    return {
        "uuid": entry.uuid,
        "title": kind.title(entry.row),
        "enabled": on(entry.row.get("enabled")) if "disabled" not in entry.row else not on(entry.row.get("disabled")),
        "adoptable": not entry.reasons,
        "bulk_adoptable": not entry.reasons or entry.bulk,
        "reasons": entry.reasons,
        "reason_details": entry.details,
        "kept": kind.kept(entry.row),
        "info": kind.info(entry.row),
    }


async def list_unmanaged(db: AsyncSession, kind_key: str, host: OpnsenseHost) -> list[dict]:
    kind = KINDS[kind_key]
    entries, _ = await _plan(db, kind, host)
    return [_describe(kind, e) for e in entries]


def _create(db: AsyncSession, kind: Kind, host: OpnsenseHost, entry: Entry):
    obj = kind.model(**kind.build(entry.row), targets=[host], groups=[])
    db.add(obj)
    return obj


async def _record(db: AsyncSession, kind: Kind, host: OpnsenseHost, obj, remote_uuid: str) -> None:
    await db.flush()
    kind.revision(db, obj, ADOPT_NOTE)
    db.add(kind.deployment(**{
        kind.fk: obj.id, "host_id": host.id, "status": "success", "message": ADOPT_MESSAGE,
        kind.uuid_attr: remote_uuid, "last_payload": kind.payload(obj), "previous_payload": None,
    }))
    await db.flush()


async def adopt(db: AsyncSession, kind_key: str, host: OpnsenseHost, remote_uuid: str):
    kind = KINDS[kind_key]
    entries, _ = await _plan(db, kind, host)
    entry = next((e for e in entries if e.uuid == remote_uuid), None)
    if entry is None:
        # either gone from the host or already managed
        if remote_uuid in await _managed_uuids(db, kind, host):
            raise NotAdoptable(["already_managed"])
        raise NotFoundOnHost(remote_uuid)
    if entry.reasons:
        raise NotAdoptable(entry.reasons, entry.details)
    obj = _create(db, kind, host, entry)
    await _record(db, kind, host, obj, remote_uuid)
    return obj


async def adopt_all(db: AsyncSession, kind_key: str, host: OpnsenseHost) -> dict:
    """Adopts everything adoptable in one transaction (the caller commits), nested aliases in
    dependency order. System objects are not reported; everything else that stays behind is, with reasons."""
    kind = KINDS[kind_key]
    entries, order = await _plan(db, kind, host)
    adopted = []
    for entry in order:
        obj = _create(db, kind, host, entry)
        await _record(db, kind, host, obj, entry.uuid)
        adopted.append({"uuid": entry.uuid, "title": kind.title(entry.row), "id": obj.id})
    done = {e.uuid for e in order}
    skipped = [
        {"uuid": e.uuid, "title": kind.title(e.row), "reasons": e.reasons, "reason_details": e.details}
        for e in entries if e.uuid not in done and e.reasons != ["system"]
    ]
    return {"adopted": adopted, "skipped": skipped}
