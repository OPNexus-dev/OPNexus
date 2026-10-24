"""Thin async client for the OPNsense REST API.

Endpoint paths verified against docs.opnsense.org and the OPNsense core
source (FilterController.php, Diagnostics/Api/InterfaceController.php,
Diagnostics/Api/FirewallController.php) — see README for the full table.
All of these are CORE endpoints; no os-firewall plugin exists or is
required.

Auth is HTTP Basic: username = API key, password = API secret.
"""
import ipaddress
import logging
from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from typing import Any

import httpx

from app import runtime_settings
from app.config import settings
from app.models.host import OpnsenseHost
from app.services import firewall_url, opnsense_secret_store, overlay_service, tls_pin_service
from app.services.redaction import redact_data, redact_text

logger = logging.getLogger(__name__)


class OpnsenseAPIError(Exception):
    pass


class OpnsenseAmbiguousError(OpnsenseAPIError):
    """The request may have been executed although no usable answer arrived (lost
    reply, timeout while waiting, server error, no UUID returned). A refusal the
    firewall actually answered is a plain OpnsenseAPIError; this one means the
    outcome of a write is unknown and must be reconciled, not retried blindly."""


class OpnsenseNotFoundError(OpnsenseAPIError):
    """The firewall answered, but there is no such object (an empty `getRule` answer: unknown UUID, or a rule that is
    not part of the rules model - legacy or automatic). A refusal, never an unknown outcome."""


class OpnsenseForbiddenError(OpnsenseAPIError):
    """The firewall answered 403: the API key lacks the privilege for this endpoint. A refusal, not an unknown
    outcome; callers that only read can tell "no rights" from "unreachable". 401 (wrong key or secret) stays a
    plain OpnsenseAPIError."""


def _client(host: OpnsenseHost) -> httpx.AsyncClient:
    # Before the secret is even read: an http:// host must never receive credentials.
    reason = firewall_url.check_base_url(host.base_url)
    if reason:
        raise OpnsenseAPIError(f"Host '{host.name}': {reason}")
    secret = opnsense_secret_store.read_secret(host.id)
    if not secret:
        raise OpnsenseAPIError(f"Kein API-Secret für Host '{host.name}' hinterlegt")
    return httpx.AsyncClient(
        base_url=host.base_url.rstrip("/") + "/api",
        auth=(host.api_key, secret),
        verify=tls_pin_service.verify_argument(host.verify_tls, host.tls_ca_pem),
        timeout=runtime_settings.value("opnsense_timeout"),
        # Firewalls reached through the management overlay (WireGuard hub) go via the hub's CONNECT proxy.
        proxy=overlay_service.proxy_for(host.base_url),
    )


# ── Rule counters before a reload (B11) ────────────────────────────────────
# Every request below that makes OPNsense reload pf (filter/NAT apply; alias and IPsec reconfigure run
# `filter reload skip_alias` too, verified in core stable/26.7; OpenVPN/WireGuard/network may through interface
# registration) zeroes every rule counter. Whatever was hit since the last observation would be lost and a rule
# could later look "unused". So the counters are read right before, through rule_hit_service. A writer that has
# just sampled itself (the rule cleanup) switches this off for its own call with ``counters_already_sampled()``.
_sampled_already: ContextVar[bool] = ContextVar("opnexus_counters_sampled", default=False)


@contextmanager
def counters_already_sampled():
    token = _sampled_already.set(True)
    try:
        yield
    finally:
        _sampled_already.reset(token)


async def _before_reload(host: OpnsenseHost) -> None:
    if _sampled_already.get():
        return
    from app.services.rule_hit_service import sample_before_reload  # needs the database; this module does not
    try:
        await sample_before_reload(host)
    except Exception as exc:  # noqa: BLE001 - a failed sample must never block or fail the write itself
        logger.info("Zählerprobe vor dem Anwenden auf %s fehlgeschlagen: %s", host.name, exc)


def _refuse_unresolved_placeholders(method: str, path: str, kwargs: dict) -> None:
    """Last line of defence for site variables: a payload that still contains a {{NAME}} placeholder was never
    resolved for this host and must not reach a firewall."""
    if method.upper() == "GET" or "json" not in kwargs:
        return
    from app.services.variables_service import PLACEHOLDER
    if PLACEHOLDER.search(json.dumps(kwargs["json"], default=str)):
        raise OpnsenseAPIError(f"{path}: Die Anfrage enthält eine nicht aufgelöste Variable ({{{{NAME}}}}) und wird nicht gesendet")


async def _do_request(host: OpnsenseHost, method: str, path: str, **kwargs) -> httpx.Response:
    _refuse_unresolved_placeholders(method, path, kwargs)
    async with _client(host) as client:
        try:
            resp = await client.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            # The body may reflect submitted secrets: keep only a scrubbed, bounded excerpt.
            secret = opnsense_secret_store.read_secret(host.id)
            status = exc.response.status_code
            error = OpnsenseAmbiguousError if status >= 500 else OpnsenseForbiddenError if status == 403 else OpnsenseAPIError
            raise error(
                f"{exc.response.status_code} {path}: {redact_text(exc.response.text, host.api_key, secret)}"
            ) from exc
        except httpx.HTTPError as exc:
            if host.tls_ca_pem and "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise OpnsenseAPIError(
                    f"Das TLS-Zertifikat von {host.name} passt nicht zum festgesetzten Zertifikat "
                    "(Zertifikat erneuert oder Verbindung manipuliert?)"
                ) from exc
            if isinstance(exc, httpx.RemoteProtocolError):
                # The firewall answered, but the HTTP framing is broken (seen with
                # OPNsense 26.7: kernel TLS corrupts responses above ~64 KiB).
                raise OpnsenseAmbiguousError(
                    f"{host.name} hat eine beschädigte HTTP-Antwort geliefert ({exc}). "
                    "Bei OPNsense 26.x hilft meist `kern.ipc.tls.enable=0` (System ▸ Einstellungen ▸ "
                    "Tunables), siehe Handbuch „Fehlerbehebung“."
                ) from exc
            # Once the request was (partly) sent, a failed read/write says nothing about
            # whether the firewall executed it; a failed connect means it never did.
            error = OpnsenseAmbiguousError if isinstance(
                exc, (httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.WriteError)
            ) else OpnsenseAPIError
            raise error(f"Verbindung zu {host.name} fehlgeschlagen: {exc}") from exc


async def _request(host: OpnsenseHost, method: str, path: str, **kwargs) -> Any:
    resp = await _do_request(host, method, path, **kwargs)
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise OpnsenseAPIError(f"Ungültige API-Antwort von {host.name} bei {path}") from exc


async def _request_raw(host: OpnsenseHost, method: str, path: str, **kwargs) -> bytes:
    """Like _request(), but for endpoints that return a raw byte payload
    (e.g. backup/download's application/octet-stream) instead of JSON."""
    resp = await _do_request(host, method, path, **kwargs)
    return resp.content


async def get_vip_status(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/diagnostics/interface/getVipStatus")


# ── Resource monitoring (read-only) ─────────────────────────────────────
# Diagnostics\Api\SystemController + InterfaceController, verified against
# core source (github.com/opnsense/core, master) AND live-verified with a
# real read-only GET against fw-primary (2026-09-17):
# https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/controllers/OPNsense/Diagnostics/Api/SystemController.php
# https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/controllers/OPNsense/Diagnostics/Api/InterfaceController.php
# Confirmed real shapes:
#   systemResources -> {"memory": {"total": "<bytes str>", "used": <bytes int>, ...}}
#   systemTime      -> {"uptime": "...", "loadavg": "0.28, 0.30, 0.26", ...}
#   systemDisk      -> {"devices": [{"mountpoint": "/", "used_pct": 2, ...}, ...]}
#   getInterfaceStatistics -> {"statistics": {"<label>": {"name": "igb0", "received-bytes": ..., "sent-bytes": ..., ...}, ...}}
async def get_system_resources(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/diagnostics/system/systemResources")


async def get_system_time(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/diagnostics/system/systemTime")


async def get_system_disk(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/diagnostics/system/systemDisk")


async def get_interface_statistics(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/diagnostics/interface/getInterfaceStatistics")


# Swap and CPU count, for the resource-threshold alert (1.24). Verified against core source
# (Diagnostics/Api/SystemController::systemSwapAction -> configd "system show swapinfo") and live
# (read-only GET, test-fw-3, OPNsense 26.7):
#   systemSwap  -> {"swap": [{"device": "/dev/da0p3", "total": "8388608", "used": "0"}]}   (KiB, strings;
#                  an empty list when the firewall has no swap)
#   cpu_usage/getCPUType -> ["QEMU Virtual CPU version 2.5+ (6 cores, 6 threads)"]
async def get_system_swap(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/diagnostics/system/systemSwap")


async def get_cpu_type(host: OpnsenseHost) -> Any:
    return await _request(host, "GET", "/diagnostics/cpu_usage/getCPUType")


# Gateway health (read-only). Routes\Api\GatewayController::statusAction runs configd "interface gateways status"
# -> scripts/routes/gateway_status.php, which merges the configured gateways with dpinger's live numbers
# (src/etc/inc/plugins.inc.d/dpinger.inc, dpinger_status()). Live-verified on test-fw-3 (26.7):
#   {"items": [{"name": "WAN_GW", "address": "203.0.113.1", "status": "none", "loss": "~", "delay": "~",
#               "stddev": "~", "monitor": "~", "status_translated": "Online"}, ...], "status": "ok"}
# "status" is the machine-readable verdict: none (fine), down, force_down (switched off by the admin), delay,
# loss, delay+loss (thresholds of the gateway itself), anything else = pending. loss/delay/stddev are "12.3 %" /
# "4.5 ms" strings, or "~" while nothing is measured (e.g. monitoring disabled on that gateway). "status" is
# "failed" with no items when the firewall has no gateways. searchGateway carries the same numbers but with a
# translated status text ("Online"), so it is not used here.
async def get_gateway_status(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/routes/gateway/status")


async def toggle_carp_maintenance(host: OpnsenseHost) -> dict:
    """Toggle persistent CARP maintenance mode.

    Verified against OPNsense Core's InterfaceController::carpStatusAction
    and carp_set_status.php. Because the upstream action is a toggle, callers
    must bind it to freshly observed state and never retry an unclear result.
    """
    return _mutation_result(
        await _request(host, "POST", "/diagnostics/interface/carp_status/maintenance"),
        "CARP-Wartungsmodus umschalten", set(),
    )


async def search_rules(host: OpnsenseHost, *, all_rows: bool = False) -> dict:
    params = {"rowCount": -1} if all_rows else None
    return await _request(host, "GET", "/firewall/filter/searchRule", params=params)


async def search_aliases(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/firewall/alias/searchItem")


async def add_alias_item(host: OpnsenseHost, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", "/firewall/alias/addItem", json={"alias": payload}), "Alias anlegen", {"saved"})


async def set_alias_item(host: OpnsenseHost, alias_uuid: str, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/alias/setItem/{alias_uuid}", json={"alias": payload}), "Alias ändern", {"saved"})


async def del_alias_item(host: OpnsenseHost, alias_uuid: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/alias/delItem/{alias_uuid}"), "Alias entfernen", {"deleted", "not found"})


async def reconfigure_aliases(host: OpnsenseHost) -> dict:
    await _before_reload(host)
    return _mutation_result(await _request(host, "POST", "/firewall/alias/reconfigure"), "Aliase anwenden", set())


async def get_rule_template(host: OpnsenseHost) -> dict:
    """`getRule` with no uuid (ApiMutableModelControllerBase's template
    response for a fresh, unsaved rule) — carries the exact `interface`
    selectOptionValues OPNsense's own rule-edit UI populates its dropdown
    from, including custom-named/opt/VLAN interfaces. Works even on a host
    with zero rules ever created, unlike deriving it from an existing
    rule's field metadata via plain `get`."""
    return await _request(host, "GET", "/firewall/filter/getRule")


def _mutation_result(data: Any, operation: str, results: set[str]) -> dict:
    # OPNsense's model API reports failures in JSON as well as HTTP errors.
    # See ApiMutableModelControllerBase::{add,set,del}Base and FilterBaseController::applyAction.
    if not isinstance(data, dict):
        raise OpnsenseAPIError(f"{operation}: Keine auswertbare Bestätigung der Firewall")
    result = str(data.get("result", "")).strip().lower()
    status = str(data.get("status", "")).strip().lower()
    accepted = result in results if "result" in data else status in {"ok", "done"}
    if data.get("validations") or not accepted or status in {"error", "failed"}:
        raise OpnsenseAPIError(f"{operation} von Firewall nicht bestätigt: {redact_data(data)}")
    return data


# -- Filter rules (FilterController -> ApiMutableModelControllerBase, rules.rule) --------------------------------
# Evidence for add/set/get/del/toggle/moveRuleBefore, checked 2026-10-22 against BOTH
#   source  opnsense/core tags 26.7.7, 26.1.9 and 25.7.11, src/opnsense/mvc/app/controllers/OPNsense/:
#           Firewall/Api/FilterController.php (the *RuleAction methods, moveRuleBeforeAction, ittrRules),
#           Firewall/Api/FilterBaseController.php (applyAction),
#           Base/ApiMutableModelControllerBase.php (addBase, setBase, getBase, delBase, toggleBase)
#   live    OPNsense 26.7.7 on two lab test firewalls (scratch rules whose description began with
#           `opnexus-b4-probe`, deleted again and applied afterwards; nothing was left behind).
# Same in all three source lines unless said otherwise; the live checks cover 26.7.7 only.
#
# addRule      POST, body {"rule": {...}}. Answer {"result": "saved", "uuid": "<new>"}; a validation failure is HTTP 200
#              {"result": "failed", "validations": {"rule.<field>": "<text>"}} (so _mutation_result raises).
#              `sequence` is a FilterSequenceField (models/OPNsense/Firewall/Filter.xml: 1..999999, default 1;
#              `sort_order` and `prio_group` are volatile, derived). Live: without `sequence` the rule gets the
#              highest sequence of ALL rules + 100 (566, 666, 766, ...), so a copy always lands at the END of its
#              prio_group; an explicit `sequence` IS honoured (sequence "3" -> sort_order 400000.0000003) and is not
#              checked for uniqueness. A restore can therefore either send the old number or call moveRuleBefore.
# setRule      POST /{uuid}, body {"rule": {...}}; answer {"result": "saved"}. It is an upsert (a well-formed unknown
#              UUID creates the rule) - never call it for a rule that may be gone.
# delRule      POST /{uuid}. {"result": "deleted"} or {"result": "not found"} (unknown, legacy and automatic UUIDs all
#              answer "not found", HTTP 200, nothing changes). 26.1 and 26.7 accept a comma-separated list; 25.7 a
#              single UUID. Consequence: "not found" proves nothing about a rule being gone - re-read it.
# getRule      GET /{uuid} -> {"rule": {...}} (every field of the rules model, select fields as
#              {"<option>": {"value": "<label>", "selected": 0|1}}, `categories` a select dict - or [] when the rule has
#              none -; no `uuid` key; read-only: `sequence`, `sort_order`, `prio_group` and the nested `audit`).
#              Live: an unknown, legacy (hex label) or automatic UUID answers HTTP 200 with a bare `[]`. 25.7 and 26.1
#              also know `?fetchmode=copy` (pre-fills `sequence`); 26.7.7 no longer has it - not used here.
#              Without a UUID it returns the template of a new rule (see get_rule_template above).
# toggleRule   POST /{uuid}/{enabled} - `enabled` is "0" or "1" and EXPLICIT; without it the rule flips (never do that).
#              Answer {"result": "Enabled"|"Disabled", "changed": true|false}. The explicit form is idempotent: repeating
#              it answers the same result with "changed": false (live: 0,0 -> true,false; 1,1 -> true,false). Any other
#              value answers {"result": "failed", "changed": false}; an unknown or legacy UUID, a GET, or a model that
#              has no such node answers {"result": "failed"} without "changed" - all HTTP 200. It only edits the
#              configuration; nothing reaches pf before apply() (the rule keeps its sort_order, searchRule shows
#              enabled "0"/"1" at once). 26.1/26.7 take a list of UUIDs (explicit value only); 25.7 one UUID.
# moveRuleBefore  POST /{selected}/{target}. {"status": "ok"} (no "result" key). Both rules must be in the same
#              `prio_group` - the sort_order prefix - which is NOT the interface: every single-interface rule of every
#              interface shares 400000 (lan and wan rules can be moved before each other), floating rules (several or
#              no interface) are 200000, interface-group rules 3000xx. Live: unknown UUID, same UUID twice and
#              different groups all answer HTTP 500 {"errorMessage": "Unexpected error, check log for details",
#              "errorTitle": "Filter"}, a GET answers 200 {"status": "error"}. A 500 is Ambiguous in _do_request, so a
#              caller that wants to tell "refused" from "unknown" must read sort_order back. The move only touches
#              `sequence` of the moved rule (and, when the gap is too small, of the rules behind the target); legacy
#              rules are not in the model and cannot be an anchor.
# apply        POST. {"status": "OK\n\n"} after `filter reload skip_alias`. FilterBaseController has no savepoint here.
async def add_rule(host: OpnsenseHost, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", "/firewall/filter/addRule", json={"rule": payload}), "Regel anlegen", {"saved"})


async def set_rule(host: OpnsenseHost, rule_uuid: str, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/filter/setRule/{rule_uuid}", json={"rule": payload}), "Regel ändern", {"saved"})


async def del_rule(host: OpnsenseHost, rule_uuid: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/filter/delRule/{rule_uuid}"), "Regel entfernen", {"deleted", "not found"})


_RULE_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _rule_ref(rule_uuid: str) -> str:
    """A rule reference ends up in the URL path: refuse anything that could leave it."""
    if not isinstance(rule_uuid, str) or not _RULE_REF.match(rule_uuid):
        raise OpnsenseAPIError("Ungültige Regel-UUID")
    return rule_uuid


async def get_rule(host: OpnsenseHost, rule_uuid: str) -> dict:
    """The full model record of one rule (`getRule/{uuid}`, the inner `rule` dict, select fields still in
    OPNsense's option form - reduce them with rule_write_payload()). A rule that is not in the rules model (unknown,
    legacy hex label, automatic) is answered with `[]`: raises OpnsenseNotFoundError."""
    data = await _request(host, "GET", f"/firewall/filter/getRule/{_rule_ref(rule_uuid)}")
    rule = data.get("rule") if isinstance(data, dict) else None
    if isinstance(rule, dict) and rule:
        return rule
    if data == [] or data == {}:
        raise OpnsenseNotFoundError(f"Regel {rule_uuid} gibt es auf {host.name} nicht (oder sie ist keine bearbeitbare Regel)")
    raise OpnsenseAPIError(f"Regel lesen: Keine auswertbare Antwort von {host.name}")


async def toggle_rule(host: OpnsenseHost, rule_uuid: str, enabled: bool) -> dict:
    """Sets the rule's `enabled` flag to exactly `enabled` (`toggleRule/{uuid}/1|0`; never the flipping form) and
    returns OPNsense's answer, e.g. {"result": "Disabled", "changed": False}. Repeating it is harmless. Only the
    configuration changes; call apply() afterwards. A refusal (unknown or legacy UUID) is a plain OpnsenseAPIError,
    a lost answer an OpnsenseAmbiguousError - then read the rule back before doing anything else."""
    if not isinstance(enabled, bool):
        raise TypeError("toggle_rule: 'enabled' must be a bool (an implicit flip would not be idempotent)")
    data = await _request(host, "POST", f"/firewall/filter/toggleRule/{_rule_ref(rule_uuid)}/{1 if enabled else 0}")
    return _mutation_result(data, "Regel ein-/ausschalten", {"enabled" if enabled else "disabled"})


# Fields of a getRule record that a write must not send: the UUID is the rule's identity, `sequence` is assigned
# on creation (sort_order and prio_group are derived from it), `audit` is metadata OPNsense maintains itself.
RULE_READONLY_FIELDS = frozenset({"uuid", "sequence", "sort_order", "prio_group", "audit"})


def rule_write_payload(rule: dict) -> dict[str, str]:
    """Turns a getRule record into the flat form addRule/setRule accept: select fields become their selected
    option(s), comma-separated (`lan,wan`; none selected -> ""), the empty-list form of an empty multi-select
    becomes "", read-only fields are left out. Live round trip on 26.7.7 (getRule -> this -> addRule -> getRule)
    gave an identical record except uuid/sequence/sort_order/audit for single-interface, multi-interface (floating),
    no-interface (floating), interface-group, ICMP and any-protocol rules with categories, aliases, port ranges,
    TCP flags, tags and state limits."""
    out: dict[str, str] = {}
    for key, value in rule.items():
        if key in RULE_READONLY_FIELDS:
            continue
        if isinstance(value, dict):
            if value and all(isinstance(o, dict) and "selected" in o for o in value.values()):
                out[key] = ",".join(k for k, o in value.items() if o.get("selected") and k != "")
            # any other nested dict is metadata and is not sent
        elif isinstance(value, list):
            out[key] = ",".join(str(v) for v in value)
        else:
            out[key] = "" if value is None else str(value)
    return out


async def apply(host: OpnsenseHost) -> dict:
    await _before_reload(host)
    return _mutation_result(await _request(host, "POST", "/firewall/filter/apply"), "Änderungen anwenden", set())


async def get_hasync(host: OpnsenseHost) -> dict:
    """System > High Availability settings, read-only: `core/hasync/get` (Core/Api/HasyncController extends
    ApiMutableModelControllerBase, model Core/Hasync.xml; identical in opnsense/core 25.7.11, 26.1.9 and 26.7.7).
    Answer {"hasync": {"pfsyncinterface": {"": {"value": "Disabled", ...}, "lan": {"value": "LAN", "selected": 1},
    ...}, "pfsyncpeerip": "<address or empty>", "synchronizetoip": ..., "syncitems": {...}}} - `pfsyncinterface` is an
    InterfaceField (the selected option, "" = disabled), `pfsyncpeerip` a NetworkField without mask. Live on 26.7.7
    (both nodes of the lab cluster): pfsyncinterface `lan`, a peer address, synchronizetoip empty. Used by the rule
    cleanup to keep pfsync rules out of reach (rule_analysis/protection.py); it reads nothing secret (the sync
    password field comes back, but is never stored or logged by the caller)."""
    return await _request(host, "GET", "/core/hasync/get")


async def move_rule_before(host: OpnsenseHost, selected_uuid: str, target_uuid: str) -> dict:
    """`firewall/filter/moveRuleBefore/{selected_uuid}/{target_uuid}`
    (FilterController::moveRuleBeforeAction) — repositions `selected_uuid`
    to sit directly before `target_uuid` via gap-numbered `sequence`
    floats, scoped to rules sharing the same `prio_group` (the sort_order
    prefix, see the evidence block above - not the interface). Verified against OPNsense core source and live
    (26.7.7): no "move to top/bottom"
    sentinel exists, and both UUIDs must resolve to existing rules in the
    SAME prio_group or the controller throws (HTTP 500, which this client
    reports as OpnsenseAmbiguousError). On success it returns
    `{"status": "ok"}` (confirmed against FilterController.php directly —
    unlike addRule/setRule/delRule this has no "result" key at all), so
    this is dispatched through _mutation_result with an empty `results`
    set exactly like apply()/reconfigure_aliases() above, which fall back
    to the status-based branch rather than the result-based one."""
    return _mutation_result(
        await _request(host, "POST", f"/firewall/filter/moveRuleBefore/{selected_uuid}/{target_uuid}"),
        "Regel verschieben", set(),
    )


async def get_log(host: OpnsenseHost, limit: int = 200, digest: str | None = None) -> Any:
    """`digest` (verified against OPNsense core's scripts/filter/read_log.py)
    is an MD5 hash of the last-seen log line: when set, OPNsense returns
    only lines strictly newer than it, reading in reverse-chronological
    order and stopping at the matching digest. Each returned record's own
    `__digest__` field is the next cursor — see log_ingest_service.py for
    the incremental-polling consumer. Omitted (None/empty) preserves the
    prior plain "latest N lines" behaviour."""
    params: dict[str, Any] = {"limit": limit}
    if digest:
        params["digest"] = digest
    return await _request(host, "GET", "/diagnostics/firewall/log", params=params)


async def get_firmware_info(host: OpnsenseHost) -> dict:
    """`core/firmware/info` (FirmwareController::infoAction) — deliberately
    not `core/firmware/status`, which triggers a slow network update-check.
    Response includes a `package` array with hundreds of entries; callers
    should only use product_id/product_version from it."""
    return await _request(host, "GET", "/core/firmware/info")


async def search_certificates(host: OpnsenseHost) -> dict:
    """`trust/cert/search` (Trust\\Api\\CertController::searchAction) — lists
    end-entity certificates only. CAs live in the separate `trust/ca/search`
    endpoint (CaController), see search_ca() below."""
    return await _request(host, "GET", "/trust/cert/search")


async def search_ca(host: OpnsenseHost) -> dict:
    """`trust/ca/search` (Trust\\Api\\CaController::searchAction ->
    searchBase('ca'), same ApiMutableModelControllerBase pattern as
    search_certificates() above — verified directly against
    CaController.php and Trust/Ca.xml on github.com/opnsense/core master,
    2026-09-09). The Ca.xml model declares commonname/valid_from/valid_to
    same as the cert model, so the response is parsed the same way — see
    live_certificates_service.get_live_cas()."""
    return await _request(host, "GET", "/trust/ca/search")


async def search_port_forward_rules(host: OpnsenseHost) -> dict:
    """`firewall/d_nat/searchRule` (Firewall\\Api\\DNatController::
    searchRuleAction). Controller-name-derived path: DNatController -> d_nat,
    same snake-casing convention as the other controllers here. The rows
    also include synthetic OPNsense-automatic entries (uuid like
    "automatic_wan_1", is_automatic true) that addRule/setRule/delRule
    cannot target — callers must filter those out before treating a row as
    editable/adoptable."""
    return await _request(host, "GET", "/firewall/d_nat/searchRule", params={"rowCount": -1})


async def add_port_forward_rule(host: OpnsenseHost, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", "/firewall/d_nat/addRule", json={"rule": payload}), "Port-Forward-Regel anlegen", {"saved"})


async def set_port_forward_rule(host: OpnsenseHost, rule_uuid: str, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/d_nat/setRule/{rule_uuid}", json={"rule": payload}), "Port-Forward-Regel ändern", {"saved"})


async def del_port_forward_rule(host: OpnsenseHost, rule_uuid: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/d_nat/delRule/{rule_uuid}"), "Port-Forward-Regel entfernen", {"deleted", "not found"})


async def apply_port_forward(host: OpnsenseHost) -> dict:
    """`firewall/d_nat/apply` — inherited from FilterBaseController, runs the
    exact same `configdRun('filter reload skip_alias')` as /firewall/filter/
    apply (verified in core source: neither DNatController nor
    FilterController override applyAction). Called via its own semantically
    named endpoint anyway, matching what OPNsense's own UI does."""
    await _before_reload(host)
    return _mutation_result(await _request(host, "POST", "/firewall/d_nat/apply"), "Änderungen anwenden", set())


async def list_backups(host: OpnsenseHost) -> dict:
    """`core/backup/backups/this` (Core\\Api\\BackupController::backupsAction)
    — "this" is the literal provider key for the local host itself, not the
    OPNexus host id. Lists OPNsense's own auto-created revisions under
    /conf/backup (every config save creates one — there is no API action to
    force a fresh one on demand), newest first."""
    return await _request(host, "GET", "/core/backup/backups/this")


async def download_backup(host: OpnsenseHost, backup_id: str | None = None) -> bytes:
    """`core/backup/download/this[/{backup}]` (BackupController::downloadAction)
    — raw config.xml bytes (application/octet-stream), not JSON. Omitting
    backup_id downloads the newest revision."""
    path = "/core/backup/download/this" + (f"/{backup_id}" if backup_id else "")
    return await _request_raw(host, "GET", path)


async def search_one_to_one_rules(host: OpnsenseHost) -> dict:
    """Read-only OneToOneController search of Filter.onetoone.rule."""
    return await _request(host, "GET", "/firewall/one_to_one/searchRule", params={"rowCount": -1})


async def add_one_to_one_rule(host: OpnsenseHost, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", "/firewall/one_to_one/addRule", json={"rule": payload}), "1:1 NAT-Regel anlegen", {"saved"})


async def set_one_to_one_rule(host: OpnsenseHost, rule_uuid: str, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/one_to_one/setRule/{rule_uuid}", json={"rule": payload}), "1:1 NAT-Regel ändern", {"saved"})


async def del_one_to_one_rule(host: OpnsenseHost, rule_uuid: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/one_to_one/delRule/{rule_uuid}"), "1:1 NAT-Regel entfernen", {"deleted", "not found"})


async def apply_one_to_one(host: OpnsenseHost) -> dict:
    """Apply through the type-specific controller, using FilterBaseController reload."""
    await _before_reload(host)
    return _mutation_result(await _request(host, "POST", "/firewall/one_to_one/apply"), "Änderungen anwenden", set())


async def search_outbound_nat_rules(host: OpnsenseHost) -> dict:
    """Read-only SourceNatController search; synthetic automatic rows are separated by the caller."""
    return await _request(host, "GET", "/firewall/source_nat/searchRule", params={"rowCount": -1})


async def add_outbound_nat_rule(host: OpnsenseHost, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", "/firewall/source_nat/addRule", json={"rule": payload}), "Outbound NAT-Regel anlegen", {"saved"})


async def set_outbound_nat_rule(host: OpnsenseHost, rule_uuid: str, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/source_nat/setRule/{rule_uuid}", json={"rule": payload}), "Outbound NAT-Regel ändern", {"saved"})


async def del_outbound_nat_rule(host: OpnsenseHost, rule_uuid: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/source_nat/delRule/{rule_uuid}"), "Outbound NAT-Regel entfernen", {"deleted", "not found"})


async def apply_outbound_nat(host: OpnsenseHost) -> dict:
    """Apply through the type-specific controller, using FilterBaseController reload."""
    await _before_reload(host)
    return _mutation_result(await _request(host, "POST", "/firewall/source_nat/apply"), "Änderungen anwenden", set())


async def get_source_nat_settings(host: OpnsenseHost) -> dict:
    """Read only. SourceNatController::getAction; shape differs by release."""
    return await _request(host, "GET", "/firewall/source_nat/get")


# Core controllers, checked against OPNsense core source:
# https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/controllers/OPNsense/Core/Api/FirmwareController.php
# https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/controllers/OPNsense/Core/Api/SystemController.php
async def get_pf_rule_statistics(host: OpnsenseHost) -> dict:
    """`diagnostics/firewall/pfStatistics/rules` (Diagnostics Api FirewallController):
    {"rules": {"filter rules": {"@N <pf rule text> label \"<uuid>\"": {evaluations, packets,
    bytes, states, ...}}}}. Verified live on OPNsense 26.7: the pf label of a rule created in
    the rule editor/API is its UUID, so counters can be tied to rules; automatic rules carry
    an md5 label instead."""
    return await _request(host, "GET", "/diagnostics/firewall/pfStatistics/rules")


# ── Rule analysis (read-only) ─────────────────────────────────────────────
# Checked against opnsense/core master (2026-10-21) and live on OPNsense 26.7:
# - Firewall/Api/FilterUtilController::ruleStatsAction runs configd `!filter rule stats` and
#   returns {"status": "ok", "stats": {"<label>": {pf_rules, evaluations, packets, bytes, states}}};
#   the label is the rule UUID (MVC), an md5 (automatic) or a 32-hex label (unmigrated legacy).
# - Firewall/Api/AliasUtilController::findReferencesAction requires POST with "ip" (only hex,
#   ":", ".", "/", "_" accepted, else {"status": "Not an IP address!"}), runs configd
#   `filter find_table_references` (a pf table test over all aliases) and returns
#   {"status": "ok", "matches": ["<alias>", ...]}. It changes nothing; POST is only the
#   controller's calling convention.
# - Firewall/Api/FilterController::getInterfaceListAction: {"floating"|"groups"|"interfaces"|"any":
#   {"label", "items": [{"value", "label", "type"}]}} (lo0 omitted).
# - Firewall/Api/GroupController::searchItemAction -> searchBase("ifgroupentry"): rows with
#   ifname, members (comma separated identifiers), sequence; the built-in groups (enc0, openvpn,
#   wireguard) appear with their name as uuid and empty members.
def _stream_addr(response: httpx.Response, which: str) -> tuple[str, int] | None:
    stream = response.extensions.get("network_stream")
    try:
        addr = stream.get_extra_info(which) if stream is not None else None
    except Exception:  # noqa: BLE001 - transport without that information
        return None
    return (str(addr[0]), int(addr[1])) if isinstance(addr, (tuple, list)) and len(addr) >= 2 else None


async def own_source_address(host: OpnsenseHost) -> str | None:
    """The address this firewall sees OPNexus's API requests come from (B10): behind NAT or the management tunnel it is
    not what OPNexus knows about itself, so the firewall is asked. Two requests on one keep-alive connection: the first
    tells the firewall address and the local port of this connection, the second lists pf's states for that address
    (`diagnostics/firewall/query_states`, POST, read-only; Diagnostics/Api/FirewallController, core stable/26.7) and the
    state whose source port is our local port is this very connection. Live 2026-10-23 on test-fw-1/3: one ESTABLISHED
    state, src 192.0.2.10 = the OPNexus server, port preserved by Docker's NAT.

    None when it cannot be told for sure: through the overlay's CONNECT proxy (the local port belongs to the proxy
    connection), a NAT that rewrote the port, or an API key without the states privilege (that raises
    OpnsenseAPIError)."""
    async with _client(host) as http:
        try:
            first = await http.get("/diagnostics/firewall/pf_states")
            first.raise_for_status()
            server = _stream_addr(first, "server_addr")
            if server is None:
                return None
            second = await http.post("/diagnostics/firewall/query_states",
                                     json={"searchPhrase": server[0], "rowCount": 500, "current": 1})
            second.raise_for_status()
            local, server = _stream_addr(second, "client_addr"), _stream_addr(second, "server_addr")
            rows = second.json().get("rows") or []
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise OpnsenseAPIError(f"Zustandstabelle von {host.name} nicht lesbar: {str(exc)[:200]}") from exc
    if local is None or server is None:
        return None

    def same_ip(a, b) -> bool:
        try:
            return ipaddress.ip_address(str(a)) == ipaddress.ip_address(str(b))
        except ValueError:
            return False

    for row in rows if isinstance(rows, list) else []:
        if (isinstance(row, dict) and same_ip(row.get("dst_addr"), server[0]) and str(row.get("dst_port")) == str(server[1])
                and str(row.get("src_port")) == str(local[1]) and str(row.get("state", "")).startswith("ESTABLISHED")):
            try:
                return str(ipaddress.ip_address(str(row.get("src_addr"))))
            except ValueError:
                return None
    return None


async def get_rule_stats(host: OpnsenseHost) -> dict:
    """Per-label counters, already summed over every pf line of a rule. Used for the rule usage sampling
    (rule_hit_service). Live check 2026-10-22 on all three test firewalls (26.7, one with unmigrated legacy
    rules): HTTP 200, ``stats`` has exactly one key per label of ``pfStatistics/rules`` (30/30/25 labels,
    including the 32-hex label of the legacy user rule on the third), and each ``packets`` equals the sum over
    that label's pf lines (differences only by traffic between the two reads)."""
    return await _request(host, "GET", "/firewall/filter_util/ruleStats")


async def find_alias_references(host: OpnsenseHost, ip: str) -> list[str] | None:
    """Names of the aliases (pf tables) that contain ``ip`` right now; None when the firewall
    could not answer (callers then treat table membership as unknown)."""
    data = await _request(host, "POST", "/firewall/alias_util/findReferences", json={"ip": ip})
    if not isinstance(data, dict) or data.get("status") != "ok" or not isinstance(data.get("matches"), list):
        return None
    return [str(name) for name in data["matches"]]


async def get_filter_interface_list(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/firewall/filter/get_interface_list")


async def search_interface_groups(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/firewall/group/searchItem", params={"rowCount": -1})


async def get_firmware_status(host: OpnsenseHost) -> dict:
    # GET reads cached results. POST would trigger a synchronous probe.
    return await _request(host, "GET", "/core/firmware/status")


async def get_firmware_running(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/core/firmware/running")


async def get_firmware_update_progress(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/core/firmware/upgradestatus")


async def check_firmware_updates(host: OpnsenseHost) -> dict:
    return _mutation_result(await _request(host, "POST", "/core/firmware/check"), "Update-Suche starten", set())


async def reboot_host(host: OpnsenseHost) -> dict:
    return _mutation_result(await _request(host, "POST", "/core/system/reboot"), "Neustart anfordern", set())


async def update_firmware(host: OpnsenseHost) -> dict:
    # Stable package updates within the current major release.
    return _mutation_result(await _request(host, "POST", "/core/firmware/update"), "Firmware-Update anfordern", set())


async def upgrade_firmware(host: OpnsenseHost) -> dict:
    # Major release upgrade. FirmwareController::upgradeAction() is otherwise
    # identical to updateAction() (same `firmware flush` + configd job +
    # {"status": "ok"} shape, same /core/firmware/upgradestatus progress
    # endpoint, same /core/firmware/running gate) - verified against core
    # source github.com/opnsense/core master, FirmwareController.php.
    return _mutation_result(await _request(host, "POST", "/core/firmware/upgrade"), "Major-Upgrade anfordern", set())


# ── Generic service control ─────────────────────────────────────────────
# Core\Api\ServiceController, verified against core source
# (github.com/opnsense/core, master):
# https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/controllers/OPNsense/Core/Api/ServiceController.php
# searchAction() is GET, lists every OPNsense-known service (id/name/
# description/locked/running). start/stop/restartAction($name, $id='')
# are POST-only, `{"result": "ok"}` on success or `{"result": "failed"}`
# (same shape as OpenVPN/WireGuard's reconfigure, not IPsec's `status`).
# search_services() has been live-verified (read-only GET) against
# fw-primary/fw-secondary; start/stop/restart remain mock-only until the
# user explicitly approves a first real call, per this project's
# verification discipline.
async def search_services(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/core/service/search")


def _service_result(data: Any, operation: str) -> dict:
    return _mutation_result(data, operation, {"ok"})


async def start_service(host: OpnsenseHost, name: str, id_: str = "") -> dict:
    path = f"/core/service/start/{name}/{id_}" if id_ else f"/core/service/start/{name}"
    return _service_result(await _request(host, "POST", path), f"Dienst '{name}' starten")


async def stop_service(host: OpnsenseHost, name: str, id_: str = "") -> dict:
    path = f"/core/service/stop/{name}/{id_}" if id_ else f"/core/service/stop/{name}"
    return _service_result(await _request(host, "POST", path), f"Dienst '{name}' stoppen")


async def restart_service(host: OpnsenseHost, name: str, id_: str = "") -> dict:
    path = f"/core/service/restart/{name}/{id_}" if id_ else f"/core/service/restart/{name}"
    return _service_result(await _request(host, "POST", path), f"Dienst '{name}' neustarten")


# ── IPsec (site-to-site, PSK) ────────────────────────────────────────────
# Verified directly against OPNsense core source (github.com/opnsense/core,
# master, fetched 2026-09-08) — see ipsec_deploy_service.py's module
# docstring for the full field-mapping writeup this endpoint list feeds.
# Endpoint-path convention confirmed against
# src/opnsense/mvc/app/library/OPNsense/Mvc/Router.php::parsePath(), the
# exact code that turns "/api/{module}/{controller}/{action}/{params...}"
# into a namespace+class+method call: each segment goes through
# str_replace('_', '', ucwords($segment, '_')), so both "camelCase" and
# "snake_case" URL segments can resolve to the same PHP class/method name
# when the segment has no internal underscores (ucwords only touches the
# very first character in that case) — this is also why the existing
# DNatController mapping ("d_nat") and a hypothetical "dNat" would both
# work. Below uses the camelCase form, matching OPNsense's own published
# API doc examples for these controllers.
#   - ConnectionsController (module "ipsec", controller "connections"):
#     searchConnection/getConnection/addConnection/setConnection/
#     delConnection/toggleConnection, and — same controller, different
#     model paths — searchLocal/getLocal/addLocal/setLocal/delLocal,
#     searchRemote/.../delRemote, searchChild/.../delChild. All confirmed
#     as literal method names in ConnectionsController.php (no separate
#     Local/Remote/Child controller files exist).
#   - PreSharedKeysController (module "ipsec", controller "preSharedKeys"):
#     searchItem/getItem/addItem/setItem/delItem — separate model
#     (OPNsense\IPsec\IPsec), NOT part of Swanctl.
#   - ServiceController (module "ipsec", controller "service"):
#     reconfigureAction, inherited from ApiMutableServiceControllerBase —
#     confirmed it returns {"status": "ok"} on success (no "result" key),
#     dispatched via _mutation_result with an empty results set exactly
#     like apply()/reconfigure_aliases() above.
#
# Field names verified against Swanctl.xml (Connections.Connection,
# locals.local, remotes.remote, children.child) and IPsec.xml
# (preSharedKeys.preSharedKey) — see ipsec_deploy_service.py for the exact
# per-field payload this app sends and why some model fields are
# deliberately omitted (OPNsense's own Add()-populated defaults cover
# them; ApiMutableModelControllerBase::addBase() only overlays the keys we
# actually post).
#
# IPsecProposalField (proposals/esp_proposals) verified against
# FieldTypes/IPsecProposalField.php: it is a BaseListField (Multiple=Y,
# i.e. comma-separated on the wire) whose accepted values are cipher-suite
# key strings such as "aes256-sha256-modp2048", OR the literal "default"
# (OPNsense's own <Default>default</Default> for both proposals and
# esp_proposals) — this app always sends "default" unless the user
# explicitly changes it, which is the same conservative, source-confirmed
# default OPNsense's own UI ships with. NOT verified against a live
# getConnection response (no write/read call was made against a real
# host for this feature — see README/plan), only against the model +
# field-type source.
def _ipsec_result(data: Any, operation: str) -> dict:
    return _mutation_result(data, operation, {"saved"})


async def search_ipsec_connections(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/ipsec/connections/searchConnection")


async def add_ipsec_connection(host: OpnsenseHost, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", "/ipsec/connections/addConnection", json={"connection": payload}), "IPsec-Verbindung anlegen")


async def set_ipsec_connection(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", f"/ipsec/connections/setConnection/{uuid_}", json={"connection": payload}), "IPsec-Verbindung ändern")


async def del_ipsec_connection(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/ipsec/connections/delConnection/{uuid_}"), "IPsec-Verbindung entfernen", {"deleted", "not found"})


async def add_ipsec_local(host: OpnsenseHost, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", "/ipsec/connections/addLocal", json={"local": payload}), "IPsec-Identität (lokal) anlegen")


async def set_ipsec_local(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", f"/ipsec/connections/setLocal/{uuid_}", json={"local": payload}), "IPsec-Identität (lokal) ändern")


async def del_ipsec_local(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/ipsec/connections/delLocal/{uuid_}"), "IPsec-Identität (lokal) entfernen", {"deleted", "not found"})


async def add_ipsec_remote(host: OpnsenseHost, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", "/ipsec/connections/addRemote", json={"remote": payload}), "IPsec-Identität (remote) anlegen")


async def set_ipsec_remote(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", f"/ipsec/connections/setRemote/{uuid_}", json={"remote": payload}), "IPsec-Identität (remote) ändern")


async def del_ipsec_remote(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/ipsec/connections/delRemote/{uuid_}"), "IPsec-Identität (remote) entfernen", {"deleted", "not found"})


async def add_ipsec_child(host: OpnsenseHost, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", "/ipsec/connections/addChild", json={"child": payload}), "IPsec Phase-2 (Child) anlegen")


async def set_ipsec_child(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", f"/ipsec/connections/setChild/{uuid_}", json={"child": payload}), "IPsec Phase-2 (Child) ändern")


async def del_ipsec_child(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/ipsec/connections/delChild/{uuid_}"), "IPsec Phase-2 (Child) entfernen", {"deleted", "not found"})


async def add_ipsec_psk(host: OpnsenseHost, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", "/ipsec/preSharedKeys/addItem", json={"preSharedKey": payload}), "IPsec Pre-Shared-Key anlegen")


async def set_ipsec_psk(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _ipsec_result(await _request(host, "POST", f"/ipsec/preSharedKeys/setItem/{uuid_}", json={"preSharedKey": payload}), "IPsec Pre-Shared-Key ändern")


async def del_ipsec_psk(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/ipsec/preSharedKeys/delItem/{uuid_}"), "IPsec Pre-Shared-Key entfernen", {"deleted", "not found"})


async def ensure_ipsec_enabled(host: OpnsenseHost) -> bool:
    """Switch the service-wide IPsec flag on (`ipsec/connections/isEnabled` /
    `toggle/1`, ConnectionsController — it is NOT part of the service
    controller). Without it the connections are stored but no daemon runs
    (live 26.7.6: is_enabled=false, service status "disabled", yet
    reconfigure answered ok). Returns True when it had to be switched on."""
    data = await _request(host, "GET", "/ipsec/connections/is_enabled")
    if isinstance(data, dict) and data.get("enabled") is True:
        return False
    _mutation_result(await _request(host, "POST", "/ipsec/connections/toggle/1"), "IPsec aktivieren", {"ok"})
    return True


async def reconfigure_ipsec(host: OpnsenseHost) -> dict:
    """`ipsec/service/reconfigure` (ServiceController::reconfigureAction,
    inherited from ApiMutableServiceControllerBase) — soft-reloads a
    running IPsec daemon instead of a full stop/start when possible.
    Deliberately its own call, separate from the 5 config-write calls
    above — see ipsec_deploy_service.py for why this app never bundles it
    silently."""
    await _before_reload(host)
    return _mutation_result(await _request(host, "POST", "/ipsec/service/reconfigure"), "IPsec-Dienst neu laden", set())


# ── OpenVPN (instance manager, 24.1+ unified model) ─────────────────────
# Verified directly against OPNsense core source (github.com/opnsense/core,
# master, fetched 2026-09-09 by downloading the raw files directly — see
# openvpn_deploy_service.py's module docstring for the full field-mapping
# writeup and scope-decision reasoning this endpoint list feeds.
#   - InstancesController (module "openvpn", controller "instances"),
#     confirmed as literal PHP method names in InstancesController.php
#     (`class InstancesController extends ApiMutableModelControllerBase`,
#     internalModelName = 'instance', internalModelClass =
#     'OPNsense\\OpenVPN\\OpenVPN'):
#       searchAction / getAction / addAction / setAction / delAction /
#       toggleAction (Instances.Instance) — same
#       ApiMutableModelControllerBase base class already relied on for
#       IPsec's Connection, so the same {"result": "saved"/"deleted"/
#       "not found", "uuid": ...} response shape is assumed here too (NOT
#       re-verified against a live response — no call was ever made
#       against a real host for this feature either). toggleAction is
#       unused, same decision IPsec made to skip toggleConnection.
#       searchStaticKeyAction / getStaticKeyAction / addStaticKeyAction /
#       setStaticKeyAction / delStaticKeyAction (StaticKeys.StaticKey,
#       internalModelName 'statickey') — same controller class, same base.
#       genKeyAction (server-side key generation via
#       `configdpRun("openvpn genkey", [$type])`) exists but is
#       deliberately NOT called by this app — the operator supplies their
#       own key material by hand, the exact same philosophy already
#       applied to IPsec's PSK (OPNexus never auto-generates it either).
#   - ServiceController (module "openvpn", controller "service"):
#     reconfigureAction — a hand-rolled `ApiControllerBase` method, NOT
#     inherited from ApiMutableServiceControllerBase like IPsec's. Its
#     body (read directly from source) is
#     `(new Backend())->configdRun('openvpn configure');
#      $this->runInterfaceRegistration(); return ['result' => 'ok'];` —
#     note the response key is "result", NOT "status" like IPsec's
#     reconfigure ({"status": "ok"}) — explicitly checked rather than
#     assumed symmetric, since the plan flagged this exact risk. Dispatched
#     through _mutation_result with results={"ok"} (the "result"-based
#     branch), not the empty-set "status"-based branch IPsec's
#     reconfigure_ipsec() uses.
#
# Field names verified against OpenVPN.xml (Instances.Instance,
# StaticKeys.StaticKey) — see openvpn_deploy_service.py for the exact
# per-field payload this app sends, and the scope decision this drove:
# `role` OptionValues are ONLY "client"/"server" (no legacy
# "p2p_shared_key" mode exists in this unified model), so a working
# instance always needs `cert`/`ca` (ModelRelationFields into OPNsense's
# own certificate store) regardless of role — this app treats those as
# opaque, OPNexus-unvalidated UUID strings the operator supplies (see
# OpenVpnInstance's docstring for why). `tls_key` (also a
# ModelRelationField, -> StaticKeys.StaticKey) is an OPTIONAL
# tls-auth/tls-crypt hardening layer on top of that, which is the one
# piece of "simple secret" auth material this app CAN fully manage itself
# end-to-end (create the StaticKey, inject its UUID) — the closest
# available analogue to IPsec's PSK, even though it isn't a full
# substitute for certificates the way PSK was a full substitute for IKE
# certs/EAP.
def _openvpn_result(data: Any, operation: str) -> dict:
    return _mutation_result(data, operation, {"saved"})


async def search_openvpn_instances(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/openvpn/instances/search")


async def add_openvpn_instance(host: OpnsenseHost, payload: dict) -> dict:
    return _openvpn_result(await _request(host, "POST", "/openvpn/instances/add", json={"instance": payload}), "OpenVPN-Instanz anlegen")


async def set_openvpn_instance(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _openvpn_result(await _request(host, "POST", f"/openvpn/instances/set/{uuid_}", json={"instance": payload}), "OpenVPN-Instanz ändern")


async def del_openvpn_instance(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/openvpn/instances/del/{uuid_}"), "OpenVPN-Instanz entfernen", {"deleted", "not found"})


async def add_openvpn_static_key(host: OpnsenseHost, payload: dict) -> dict:
    return _openvpn_result(await _request(host, "POST", "/openvpn/instances/addStaticKey", json={"statickey": payload}), "OpenVPN Static-Key anlegen")


async def set_openvpn_static_key(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _openvpn_result(await _request(host, "POST", f"/openvpn/instances/setStaticKey/{uuid_}", json={"statickey": payload}), "OpenVPN Static-Key ändern")


async def del_openvpn_static_key(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/openvpn/instances/delStaticKey/{uuid_}"), "OpenVPN Static-Key entfernen", {"deleted", "not found"})


async def reconfigure_openvpn(host: OpnsenseHost) -> dict:
    """`openvpn/service/reconfigure` (ServiceController::reconfigureAction)
    — runs `configdRun('openvpn configure')` plus interface registration.
    Returns `{"result": "ok"}` (confirmed directly from source — see the
    section comment above for why this is NOT assumed symmetric with
    IPsec's `{"status": "ok"}`), dispatched via _mutation_result with the
    "result"-based branch (results={"ok"})."""
    await _before_reload(host)
    return _mutation_result(await _request(host, "POST", "/openvpn/service/reconfigure"), "OpenVPN-Dienst neu laden", {"ok"})


# ── WireGuard (core since 24.1, moved out of the os-wireguard plugin) ────
# Verified directly against OPNsense core source (github.com/opnsense/core,
# master, fetched 2026-09-09 by downloading the raw model XML / controller
# PHP files directly — see wireguard_deploy_service.py's module docstring
# for the full field-mapping writeup and scope-decision reasoning this
# endpoint list feeds. IMPORTANT CORRECTION vs. this session's own
# earlier, pre-implementation research (which assumed the same bare
# `addAction`/`setAction`/`delAction` naming OpenVPN's InstancesController
# uses): WireGuard's controllers name every CRUD action after the model it
# targets (`addServerAction`/`setServerAction`/... and
# `addClientAction`/`setClientAction`/...), matching IPsec's
# addConnection/addLocal/... naming convention instead — confirmed by
# reading ServerController.php/ClientController.php directly, not assumed
# from either sibling feature.
#   - ServerController (module "wireguard", controller "server",
#     `class ServerController extends ApiMutableModelControllerBase`,
#     internalModelName 'server', internalModelClass
#     '\OPNsense\Wireguard\Server'): searchServerAction/getServerAction/
#     addServerAction/setServerAction/delServerAction/toggleServerAction —
#     all confirmed as literal PHP method names. toggleServerAction is
#     unused, same decision IPsec/OpenVPN made to skip their own
#     toggle*Action. keyPairAction (`configdRun('wireguard gen_keypair')`)
#     exists but is deliberately NOT called by this app — see below.
#   - ClientController (module "wireguard", controller "client",
#     internalModelName 'client', internalModelClass
#     '\OPNsense\Wireguard\Client'): searchClientAction/getClientAction/
#     addClientAction/setClientAction/delClientAction/toggleClientAction —
#     confirmed as literal PHP method names. Notable, read directly from
#     source: addClientAction(uuid=null) simply calls
#     setClientAction(null) — there is no separate "add" code path.
#     setClientAction($uuid) is where the denormalized peer<->server
#     linkage is actually written: it reads a `client[servers]` POST field
#     (comma-separated Server UUIDs this peer should be attached to),
#     loads a fresh Server model instance, adds/removes this peer's UUID
#     to/from each matching Server's `peers` field accordingly, persists
#     that via `$mdl->serializeToConfig(false, true)` (bypassing normal
#     validation, since the peer's own UUID may be brand new), and ONLY
#     THEN calls the base class's own setBase() to persist the Client
#     record itself — both writes happen inside this ONE POST. On a new
#     peer (uuid was null), setClientAction generates the UUID itself
#     (`$mdl->servers->generateUUID()`, confirmed read directly from
#     source — reusing the SERVER model's UUID pool, not the Client
#     model's own, an OPNsense-internal detail this app doesn't need to
#     care about) and adds `"uuid"` to the response when
#     `result["result"] == "saved"`, same convention as every other
#     addBase()-backed endpoint already relied on elsewhere in this app.
#     delClientAction($uuid) does the mirror-image cleanup: before
#     deleting the Client record, it loops every Server and strips this
#     peer's UUID out of any `peers` list that contains it. CONCRETE
#     CONSEQUENCE for this app's deploy sequence (see
#     wireguard_deploy_service.py): this app NEVER reads or rewrites a
#     Server's `peers` field directly — it only ever needs to (a) create/
#     update the Server first (to know its UUID), then (b) create/update
#     the Client/Peer with `servers` set to that Server's UUID, letting
#     OPNsense's own controller maintain the Server's `peers` list as a
#     side effect; and on delete, deleting the Client/Peer first is
#     sufficient to unlink it from its Server automatically, before the
#     Server itself is deleted. Getting the ORDER wrong (writing the peer
#     before its server exists) would silently produce an unlinked peer
#     instead of an error — `setClientAction`'s loop simply never matches
#     a not-yet-existing server UUID, so the write "succeeds" but never
#     attaches. Also present, unused by this app (v1 doesn't build the
#     config-builder / peer-generator wizard these back):
#     getClientBuilderAction/addClientBuilderAction, listServersAction,
#     getServerInfoAction (UI helper for auto-suggesting a free tunnel
#     address). pskAction (`configdRun('wireguard gen_psk')`) exists but
#     is deliberately NOT called by this app — see below.
#   - ServiceController (module "wireguard", controller "service"):
#     reconfigureAction — a hand-rolled `ApiMutableServiceControllerBase`
#     override (NOT the inherited default), body read directly from
#     source: `$this->runInterfaceRegistration();
#     $backend->configdRun('template reload ...');
#     $backend->configdpRun('wireguard configure'); return ['result' =>
#     'ok'];` (or `['result' => 'failed']` if not a POST). Response key is
#     "result", matching OpenVPN's shape (`{"result": "ok"}`), NOT IPsec's
#     `{"status": "ok"}` — explicitly checked from source rather than
#     assumed symmetric with either sibling feature, dispatched through
#     _mutation_result with the "result"-based branch (results={"ok"}).
#     Also present, unused by this app: showAction (`wg show` live peer/
#     handshake status) — a plausible future read-only status column, out
#     of v1 scope.
#   - GeneralController (module "wireguard", controller "general") exists
#     but is a bare `ApiMutableModelControllerBase` subclass with NO
#     custom action methods of its own — meaning it only exposes whatever
#     that base class provides generically, which this app doesn't call
#     at all. General.xml (`OPNsense\Wireguard\General`, mount
#     `//OPNsense/wireguard/general`) declares exactly one field, a
#     top-level `enabled` (BooleanField, Default 0) — a SEPARATE,
#     service-wide WireGuard on/off master switch that
#     ServiceController's own `$internalServiceEnabled = 'enabled'` /
#     `$internalServiceClass = General::class` tie together for the
#     framework's generic service-status plumbing. NOT INDEPENDENTLY
#     VERIFIED LIVE (26.7.5, 2026-10-09): with General.enabled=0 a deploy
#     writes Server+Peer correctly but NO wg interface comes up and
#     `wireguard/service/show` stays empty, while OPNexus used to report
#     "success". Enabling it (wireguard/general/set + reconfigure) brings
#     wg0 up. wireguard_deploy_service therefore switches it on before the
#     reconfigure (ensure_wireguard_enabled below); undeploy never touches it.
#
# Field names verified against Server.xml/Client.xml (fetched directly,
# not summarized) — see wireguard_deploy_service.py for the exact
# per-field payload this app sends. Two source-confirmed findings that
# drove the "operator supplies all key material" scope decision (the same
# philosophy IPsec/OpenVPN already apply to their own PSK/tls_key):
# Server.pubkey has no <Required> tag AND ServerField.php's only
# actionPostLoadingEvent() override computes cnfFilename/statFilename/
# interface (`wg{instance}`) — there is no crypto derivation anywhere in
# the model/field-type layer, so a public key is never auto-computed from
# a supplied private key server-side. Both `keyPairAction`
# (`wireguard gen_keypair`) and `pskAction` (`wireguard gen_psk`) exist
# purely as convenience endpoints OPNsense's own UI uses; this app
# deliberately never calls either, mirroring the exact reasoning IPsec/
# OpenVPN already documented for skipping their own key-generation
# actions.
def _wireguard_result(data: Any, operation: str) -> dict:
    return _mutation_result(data, operation, {"saved"})


async def add_wireguard_server(host: OpnsenseHost, payload: dict) -> dict:
    return _wireguard_result(await _request(host, "POST", "/wireguard/server/addServer", json={"server": payload}), "WireGuard-Server anlegen")


async def set_wireguard_server(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _wireguard_result(await _request(host, "POST", f"/wireguard/server/setServer/{uuid_}", json={"server": payload}), "WireGuard-Server ändern")


async def del_wireguard_server(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/wireguard/server/delServer/{uuid_}"), "WireGuard-Server entfernen", {"deleted", "not found"})


async def add_wireguard_peer(host: OpnsenseHost, payload: dict) -> dict:
    # Wire payload key is "client" (ClientController's internalModelName)
    # even though this app's own naming says "peer" throughout — see the
    # section comment above. The `servers` field (comma-list of Server
    # UUIDs) is what makes OPNsense link this peer to its server(s).
    return _wireguard_result(await _request(host, "POST", "/wireguard/client/addClient", json={"client": payload}), "WireGuard-Peer anlegen")


async def set_wireguard_peer(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _wireguard_result(await _request(host, "POST", f"/wireguard/client/setClient/{uuid_}", json={"client": payload}), "WireGuard-Peer ändern")


async def del_wireguard_peer(host: OpnsenseHost, uuid_: str) -> dict:
    # delClientAction also strips this peer's UUID from every Server's
    # `peers` list server-side before deleting the Client record itself —
    # see the section comment above. This app never needs a separate call
    # to unlink the peer from its server.
    return _mutation_result(await _request(host, "POST", f"/wireguard/client/delClient/{uuid_}"), "WireGuard-Peer entfernen", {"deleted", "not found"})


async def ensure_wireguard_enabled(host: OpnsenseHost) -> bool:
    """Turn the service-wide WireGuard switch (General.enabled, default 0) on
    if it is off. Returns True when it had to be switched on."""
    data = await _request(host, "GET", "/wireguard/general/get")
    general = data.get("general", {}) if isinstance(data, dict) else {}
    if str(general.get("enabled", "0")) == "1":
        return False
    _mutation_result(
        await _request(host, "POST", "/wireguard/general/set", json={"general": {"enabled": "1"}}),
        "WireGuard-Dienst aktivieren",
        {"saved"},
    )
    return True


async def reconfigure_wireguard(host: OpnsenseHost) -> dict:
    """`wireguard/service/reconfigure` (ServiceController::reconfigureAction)
    — runs interface registration + template reload + `wireguard configure`.
    Returns `{"result": "ok"}` (confirmed directly from source — see the
    section comment above for why this is NOT assumed symmetric with
    IPsec's `{"status": "ok"}`, and matches OpenVPN's shape instead),
    dispatched via _mutation_result with the "result"-based branch
    (results={"ok"})."""
    await _before_reload(host)
    return _mutation_result(await _request(host, "POST", "/wireguard/service/reconfigure"), "WireGuard-Dienst neu laden", {"ok"})


# ── Trust-Store certificates (Core, Trust/Api/CertController.php) ───────
# Verified directly against OPNsense core source (raw.githubusercontent.com/
# opnsense/core/master/..., fetched 2026-09-09 by downloading Cert.xml and
# CertController.php directly) — see managed_certificate_service.py's
# module docstring for the full field-mapping writeup.
#
# URL SEGMENTS ARE NOT PHP METHOD NAMES: the router strips the `Action`
# suffix, so addAction() is POST /trust/cert/add (the `...Action` URL is a
# 404 — found live 2026-10-09 on 26.7.5, the respx mocks had hidden it).
#
# CertController extends ApiMutableModelControllerBase exactly like
# FilterController/ConnectionsController/etc: searchAction (already used
# via search_certificates() above) / getAction / addAction / setAction /
# delAction, all confirmed as literal PHP method names. addAction/setAction
# both invoke setBaseHook() (confirmed in
# ApiMutableModelControllerBase::addBase()/setBase() — the hook call sites
# were read directly, not assumed), which is where the actual
# internal/external/import/sign_csr/import_csr/reissue/manual branching
# happens and where a UserException (e.g. "Invalid X509 certificate
# provided") gets thrown on bad input — that surfaces as an HTTP error via
# OpnsenseAPIError exactly like every other controller here, so the
# standard {"result":"saved","uuid":...} success shape via _mutation_result
# applies. Trust-Store has NO async/external-network behavior at all (it
# never leaves the local OPNsense host, even for CA-signed certs — that's
# all local OpenSSL operations against the Trust-Store's own local CA
# store) — this is NOT one of the asymmetric ACME response shapes below.
#
# delAction calls checkAndThrowValueInUse() itself (confirmed reading
# CertController::delAction() directly) BEFORE delBase() — deleting an
# in-use cert throws a UserException carrying OPNsense's own "<module> -
# <description> {<reference>}" message, which surfaces as an HTTP error
# this app passes straight through rather than pre-checking itself (per
# the plan).
def _trust_cert_result(data: Any, operation: str) -> dict:
    return _mutation_result(data, operation, {"saved"})


async def add_trust_cert(host: OpnsenseHost, payload: dict) -> dict:
    return _trust_cert_result(await _request(host, "POST", "/trust/cert/add", json={"cert": payload}), "Zertifikat anlegen")


async def set_trust_cert(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _trust_cert_result(await _request(host, "POST", f"/trust/cert/set/{uuid_}", json={"cert": payload}), "Zertifikat ändern")


async def del_trust_cert(host: OpnsenseHost, uuid_: str) -> dict:
    # NOT dispatched through the {"saved"} results set above — delBase()'s
    # own success shape is {"status": "deleted"}/{"status": "not found"},
    # matching every other del* call in this module (add_alias_item's
    # del_alias_item sibling etc.), so this reuses the same
    # {"deleted", "not found"} results set they all share.
    return _mutation_result(await _request(host, "POST", f"/trust/cert/del/{uuid_}"), "Zertifikat entfernen", {"deleted", "not found"})


async def get_trust_cert(host: OpnsenseHost, uuid_: str) -> dict:
    return await _request(host, "GET", f"/trust/cert/get/{uuid_}")


# ── ACME (Let's Encrypt et al.), os-acme-client plugin ───────────────────
# Verified directly against opnsense/plugins source (raw.githubusercontent.
# com/opnsense/plugins/master/security/acme-client/..., fetched 2026-09-09
# by downloading AcmeClient.xml, AccountsController.php and
# CertificatesController.php directly) — see acme_service.py's module
# docstring for the full field-mapping writeup.
#
# AccountsController (module "acmeclient", controller "accounts",
# `class AccountsController extends ApiMutableModelControllerBase`,
# internalModelName 'accounts.account' path... actually internalModelName
# is 'accounts', getBase/addBase/setBase/delBase all address the
# 'accounts.account' path explicitly per-call): getAction/addAction/
# delAction all confirmed as literal PHP method names — BUT the edit
# action is named updateAction, NOT setAction (explicitly checked, per
# the plan's own warning not to assume symmetry with Trust-Store's
# setAction/IPsec/OpenVPN/WireGuard's setX naming). delAction calls
# delBase('accounts.account', $uuid) — a 2-arg form (vs. Trust-Store's
# checkAndThrowValueInUse()-then-delBase() dance) with no in-use guard of
# its own; deleting an account an AcmeCertificateRequest still references
# is this app's OWN responsibility to prevent (see acme_service.py).
#
# CertificatesController (module "acmeclient", controller "certificates"):
# same shape — getAction/addAction/updateAction/delAction, all confirmed
# as literal PHP method names.
#
# BOTH controllers additionally expose a real action-trigger endpoint
# with the SAME distinctive response shape, confirmed reading the PHP
# directly: registerAction($uuid) (accounts) and signAction($uuid)
# (certificates) both return `{"result": "failed"}` immediately if the
# uuid doesn't resolve to a model node, OR — on any POST that DOES resolve
# — `{"response": <raw configdRun() text>}`, with NO other shape possible
# (there is no third branch). `_acme_action_result()` below is the
# dedicated parser for this (NOT `_mutation_result`, which expects
# "result"/"status" values like "saved"/"ok"/"deleted", never "failed" as
# the ONLY populated key with no accompanying "response").
# registerAction's body literally runs
# `$backend->configdRun("acmeclient register-account {$uuid}")`
# synchronously (this app's request blocks until acme.sh's
# --registeraccount call against the real CA returns) — idempotent on an
# already-registered account, but still a genuine external side effect
# every time it's called, per the plan.
# signAction's body is the same shape but its underlying configd action
# (`acmeclient sign-cert {$uuid}`) is documented (per the plan, verified
# against this plugin's own configd actions in prior research) to fork a
# background daemon and return immediately — so a "response" value here
# is only proof the trigger was accepted, never proof of actual issuance
# success. The real outcome is only knowable by polling
# statusCode/statusLastUpdate/lastUpdate via get_acme_certificate() below,
# AFTER this call returns — see acme_service.py's poll_certificate_status().
def _acme_result(data: Any, operation: str) -> dict:
    return _mutation_result(data, operation, {"saved"})


async def add_acme_account(host: OpnsenseHost, payload: dict) -> dict:
    return _acme_result(await _request(host, "POST", "/acmeclient/accounts/add", json={"account": payload}), "ACME-Account anlegen")


async def update_acme_account(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _acme_result(await _request(host, "POST", f"/acmeclient/accounts/update/{uuid_}", json={"account": payload}), "ACME-Account ändern")


async def del_acme_account(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/acmeclient/accounts/del/{uuid_}"), "ACME-Account entfernen", {"deleted", "not found"})


async def get_acme_account(host: OpnsenseHost, uuid_: str) -> dict:
    return await _request(host, "GET", f"/acmeclient/accounts/get/{uuid_}")


async def add_acme_certificate(host: OpnsenseHost, payload: dict) -> dict:
    return _acme_result(await _request(host, "POST", "/acmeclient/certificates/add", json={"certificate": payload}), "ACME-Zertifikatsanfrage anlegen")


async def update_acme_certificate(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _acme_result(await _request(host, "POST", f"/acmeclient/certificates/update/{uuid_}", json={"certificate": payload}), "ACME-Zertifikatsanfrage ändern")


async def del_acme_certificate(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/acmeclient/certificates/del/{uuid_}"), "ACME-Zertifikatsanfrage entfernen", {"deleted", "not found"})


async def get_acme_certificate(host: OpnsenseHost, uuid_: str) -> dict:
    return await _request(host, "GET", f"/acmeclient/certificates/get/{uuid_}")


def _acme_action_result(data: Any, operation: str) -> dict:
    """Dedicated parser for register/sign's asymmetric response shape
    ({"result": "failed"} OR {"response": "<raw text>"}) — see the section
    comment above for why this is a THIRD distinct shape after IPsec's
    ({"status": "ok"}) and OpenVPN/WireGuard's ({"result": "ok"}), and
    why `_mutation_result` deliberately isn't reused for it."""
    if not isinstance(data, dict):
        raise OpnsenseAPIError(f"{operation}: keine auswertbare Antwort der Firewall")
    if "response" in data:
        return data
    if str(data.get("result", "")).strip().lower() == "failed":
        raise OpnsenseAPIError(f"{operation}: von der Firewall abgelehnt (Objekt nicht gefunden oder keine POST-Anfrage)")
    raise OpnsenseAPIError(f"{operation}: unerwartete Antwortform der Firewall: {redact_data(data)}")


async def register_acme_account(host: OpnsenseHost, uuid_: str) -> dict:
    """`POST /acmeclient/accounts/register/{uuid}` — REAL, blocking network
    call to the configured external CA (see section comment above). Never
    call this against anything but a respx mock / fully-mocked write path
    in tests/verification."""
    return _acme_action_result(await _request(host, "POST", f"/acmeclient/accounts/register/{uuid_}"), "ACME-Account registrieren")


async def sign_acme_certificate(host: OpnsenseHost, uuid_: str) -> dict:
    """`POST /acmeclient/certificates/sign/{uuid}` — REAL, asynchronous
    network call to the configured external CA (see section comment
    above). The trigger response is NOT proof of successful issuance —
    callers must poll get_acme_certificate() afterwards. Never call this
    against anything but a respx mock / fully-mocked write path in
    tests/verification."""
    return _acme_action_result(await _request(host, "POST", f"/acmeclient/certificates/sign/{uuid_}"), "ACME-Zertifikat ausstellen")


# ── Network inventory (read-only) ────────────────────────────────────────
# Interfaces\Api\{Overview,Assignment,VlanSettings,VipSettings}Controller,
# Routing\Api\SettingsController and Routes\Api\RoutesController, verified
# against core source (github.com/opnsense/core, master) AND live with real
# read-only GETs on OPNsense 26.7.1/26.7.6 (2026-10-09). Shapes: the search*
# calls return {"rows": [...]} (flat dicts with "%"-prefixed display
# counterparts); interfacesInfo rows carry device/identifier/status/ipv4[].
# Caution: vip_settings rows contain the CARP password in plain text — callers
# must drop it (see network_service).
async def get_interfaces_info(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/interfaces/overview/interfacesInfo", params={"rowCount": -1})


async def search_interface_assignments(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/interfaces/assignment/searchItem", params={"rowCount": -1})


async def search_vlans(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/interfaces/vlan_settings/searchItem", params={"rowCount": -1})


async def search_vips(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/interfaces/vip_settings/searchItem", params={"rowCount": -1})


async def search_gateways(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/routing/settings/searchGateway", params={"rowCount": -1})


async def search_static_routes(host: OpnsenseHost) -> dict:
    return await _request(host, "GET", "/routes/routes/searchroute", params={"rowCount": -1})


# ── Network objects (write) ──────────────────────────────────────────────
# One table for the four config objects handled by network_change_service.
# Sources (core master, read 2026-10-09): Interfaces/Api/VlanSettingsController,
# VipSettingsController, Routing/Api/SettingsController (gateways) and
# Routes/Api/RoutesController. Each add/set takes {<body_key>: {fields}}; set/del
# address the item by UUID; nothing takes effect before the object's reconfigure
# call, which answers {"status": "ok"}. The VIP form posts address+prefix as ONE
# field called "network".
# The interface assignment (Interfaces/Api/AssignmentController, model NetworkInterface) is two-step:
# add/set/del only stage the change (a todo file on the firewall); reconfigure ("interface apply") makes
# it real. Its items are addressed by the interface identifier (wan, lan, opt1), not a UUID, and
# deleting one makes OPNsense itself drop every rule/NAT entry that references it.
NETWORK_ITEM_APIS = {
    "vlan": {"body_key": "vlan", "base": "/interfaces/vlan_settings", "add": "addItem", "set": "setItem",
             "del": "delItem", "get": "getItem", "search": "searchItem",
             "reconfigure": "/interfaces/vlan_settings/reconfigure"},
    "vip": {"body_key": "vip", "base": "/interfaces/vip_settings", "add": "addItem", "set": "setItem",
            "del": "delItem", "get": "getItem", "search": "searchItem",
            "reconfigure": "/interfaces/vip_settings/reconfigure"},
    "gateway": {"body_key": "gateway_item", "base": "/routing/settings", "add": "addGateway", "set": "setGateway",
                "del": "delGateway", "get": "getGateway", "search": "searchGateway",
                "reconfigure": "/routing/settings/reconfigure"},
    "route": {"body_key": "route", "base": "/routes/routes", "add": "addroute", "set": "setroute",
              "del": "delroute", "get": "getroute", "search": "searchroute",
              "reconfigure": "/routes/routes/reconfigure"},
    "interface": {"body_key": "interface", "base": "/interfaces/assignment", "add": "addItem", "set": "setItem",
                  "del": "delItem", "get": "getItem", "search": "searchItem",
                  "reconfigure": "/interfaces/assignment/reconfigure"},
    # DNS / DHCP (Unbound/Api/SettingsController, Dnsmasq/Api/SettingsController, Kea/Api/Dhcpv4Controller;
    # reconfigure is the service controller's inherited action). Read 2026-10-09.
    "dns_host": {"body_key": "host", "base": "/unbound/settings", "add": "addHostOverride", "set": "setHostOverride",
                 "del": "delHostOverride", "get": "getHostOverride", "search": "searchHostOverride",
                 "reconfigure": "/unbound/service/reconfigure"},
    "dhcp_host": {"body_key": "host", "base": "/dnsmasq/settings", "add": "addHost", "set": "setHost",
                  "del": "delHost", "get": "getHost", "search": "searchHost",
                  "reconfigure": "/dnsmasq/service/reconfigure"},
    "kea_subnet": {"body_key": "subnet4", "base": "/kea/dhcpv4", "add": "addSubnet", "set": "setSubnet",
                   "del": "delSubnet", "get": "getSubnet", "search": "searchSubnet",
                   "reconfigure": "/kea/service/reconfigure"},
    # System / interface devices / DNS+DHCP extras (same controllers pattern; read 2026-10-09).
    "tunable": {"body_key": "sysctl", "base": "/core/tunables", "add": "addItem", "set": "setItem", "del": "delItem",
                "get": "getItem", "search": "searchItem", "reconfigure": "/core/tunables/reconfigure"},
    "cron_job": {"body_key": "job", "base": "/cron/settings", "add": "addJob", "set": "setJob", "del": "delJob",
                 "get": "getJob", "search": "searchJobs", "reconfigure": "/cron/service/reconfigure"},
    "syslog_destination": {"body_key": "destination", "base": "/syslog/settings", "add": "addDestination",
                           "set": "setDestination", "del": "delDestination", "get": "getDestination",
                           "search": "searchDestinations", "reconfigure": "/syslog/service/reconfigure"},
    "gateway_group": {"body_key": "gateway_group", "base": "/routing/group_settings", "add": "add", "set": "set",
                      "del": "del", "get": "get", "search": "search", "reconfigure": "/routing/group_settings/reconfigure"},
    "lagg": {"body_key": "lagg", "base": "/interfaces/lagg_settings", "add": "addItem", "set": "setItem",
             "del": "delItem", "get": "getItem", "search": "searchItem", "reconfigure": "/interfaces/lagg_settings/reconfigure"},
    "bridge": {"body_key": "bridge", "base": "/interfaces/bridge_settings", "add": "addItem", "set": "setItem",
               "del": "delItem", "get": "getItem", "search": "searchItem", "reconfigure": "/interfaces/bridge_settings/reconfigure"},
    "loopback": {"body_key": "loopback", "base": "/interfaces/loopback_settings", "add": "addItem", "set": "setItem",
                 "del": "delItem", "get": "getItem", "search": "searchItem", "reconfigure": "/interfaces/loopback_settings/reconfigure"},
    "gre": {"body_key": "gre", "base": "/interfaces/gre_settings", "add": "addItem", "set": "setItem",
            "del": "delItem", "get": "getItem", "search": "searchItem", "reconfigure": "/interfaces/gre_settings/reconfigure"},
    "vxlan": {"body_key": "vxlan", "base": "/interfaces/vxlan_settings", "add": "addItem", "set": "setItem",
              "del": "delItem", "get": "getItem", "search": "searchItem", "reconfigure": "/interfaces/vxlan_settings/reconfigure"},
    "gif": {"body_key": "gif", "base": "/interfaces/gif_settings", "add": "addItem", "set": "setItem",
            "del": "delItem", "get": "getItem", "search": "searchItem", "reconfigure": "/interfaces/gif_settings/reconfigure"},
    "dns_forward": {"body_key": "dot", "base": "/unbound/settings", "add": "addForward", "set": "setForward",
                    "del": "delForward", "get": "getForward", "search": "searchForward",
                    "reconfigure": "/unbound/service/reconfigure"},
    "dhcp_range": {"body_key": "range", "base": "/dnsmasq/settings", "add": "addRange", "set": "setRange",
                   "del": "delRange", "get": "getRange", "search": "searchRange",
                   "reconfigure": "/dnsmasq/service/reconfigure"},
    "kea_reservation": {"body_key": "reservation", "base": "/kea/dhcpv4", "add": "addReservation",
                        "set": "setReservation", "del": "delReservation", "get": "getReservation",
                        "search": "searchReservation", "reconfigure": "/kea/service/reconfigure"},
}


def _nest(fields: dict) -> dict:
    """{"option_data.routers": "x"} -> {"option_data": {"routers": "x"}} (the wire form of Kea's option block)."""
    out: dict = {}
    for key, value in fields.items():
        head, _, tail = key.partition(".")
        if tail:
            out.setdefault(head, {})[tail] = value
        else:
            out[key] = value
    return out


async def get_network_item(host: OpnsenseHost, kind: str, uuid: str | None) -> dict:
    api = NETWORK_ITEM_APIS[kind]
    path = f"{api['base']}/{api['get']}" + (f"/{uuid}" if uuid else "")
    data = await _request(host, "GET", path)
    item = data.get(api["body_key"]) if isinstance(data, dict) else None
    return item if isinstance(item, dict) else {}


async def write_network_item(host: OpnsenseHost, kind: str, action: str, uuid: str | None, fields: dict) -> dict:
    api = NETWORK_ITEM_APIS[kind]
    if action == "add":
        return _mutation_result(
            await _request(host, "POST", f"{api['base']}/{api['add']}", json={api["body_key"]: _nest(fields)}),
            "Objekt anlegen", {"saved"})
    if action == "set":
        return _mutation_result(
            await _request(host, "POST", f"{api['base']}/{api['set']}/{uuid}", json={api["body_key"]: _nest(fields)}),
            "Objekt ändern", {"saved"})
    return _mutation_result(
        await _request(host, "POST", f"{api['base']}/{api['del']}/{uuid}"), "Objekt entfernen", {"deleted"})


async def reconfigure_network(host: OpnsenseHost, kind: str) -> dict:
    await _before_reload(host)
    return _mutation_result(
        await _request(host, "POST", NETWORK_ITEM_APIS[kind]["reconfigure"]), "Änderung anwenden", set())


async def list_network_item_uuids(host: OpnsenseHost, kind: str) -> list[str]:
    api = NETWORK_ITEM_APIS[kind]
    data = await _request(host, "GET", f"{api['base']}/{api['search']}", params={"rowCount": -1})
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [str(r.get("uuid")) for r in rows if isinstance(r, dict) and r.get("uuid")]


async def interface_changes_pending(host: OpnsenseHost) -> bool:
    """True while staged interface changes wait for an apply (AssignmentController::pendingAction).
    Our own apply would apply those too, so a change must not start on top of them."""
    data = await _request(host, "GET", "/interfaces/assignment/pending")
    return isinstance(data, dict) and data.get("status") == "pending"


async def _search_all(host: OpnsenseHost, path: str) -> dict:
    return await _request(host, "GET", path, params={"rowCount": -1})


async def search_dns_hosts(host: OpnsenseHost) -> dict:
    return await _search_all(host, "/unbound/settings/searchHostOverride")


async def search_dhcp_hosts(host: OpnsenseHost) -> dict:
    return await _search_all(host, "/dnsmasq/settings/searchHost")


async def search_kea_subnets(host: OpnsenseHost) -> dict:
    return await _search_all(host, "/kea/dhcpv4/searchSubnet")


async def search_kea_reservations(host: OpnsenseHost) -> dict:
    return await _search_all(host, "/kea/dhcpv4/searchReservation")


async def search_network_objects(host: OpnsenseHost, kind: str) -> list[dict]:
    api = NETWORK_ITEM_APIS[kind]
    data = await _request(host, "GET", f"{api['base']}/{api['search']}", params={"rowCount": -1})
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [r for r in rows if isinstance(r, dict)]


# ── ZFS snapshots (Core/Api/SnapshotsController, read 2026-10-09) ────────
# Not a config object: flat form fields instead of {body_key: {...}}, no reconfigure, and the answers are
# free-form ("OK" strings / {"status": ...}), so callers confirm by listing again.
async def list_snapshots(host: OpnsenseHost) -> list[dict]:
    data = await _request(host, "GET", "/core/snapshots/search", params={"rowCount": -1})
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [r for r in rows if isinstance(r, dict)]


async def create_snapshot(host: OpnsenseHost, name: str, note: str = "") -> None:
    data = await _request(host, "POST", "/core/snapshots/add", json={"name": name, "note": note})
    if isinstance(data, dict) and (data.get("validations") or str(data.get("status", "")).lower() == "failed"):
        raise OpnsenseAPIError(f"Snapshot anlegen von Firewall nicht bestätigt: {redact_data(data)}")


async def activate_snapshot(host: OpnsenseHost, uuid: str) -> None:
    """Boot environment for every following start (bectl activate, not -t); takes effect at the next reboot."""
    data = await _request(host, "POST", f"/core/snapshots/activate/{uuid}")
    if isinstance(data, dict) and (data.get("validations") or str(data.get("status", "")).lower() == "failed"):
        raise OpnsenseAPIError(f"Snapshot aktivieren von Firewall nicht bestätigt: {redact_data(data)}")


async def delete_snapshot(host: OpnsenseHost, uuid: str) -> None:
    data = await _request(host, "POST", f"/core/snapshots/del/{uuid}")
    if isinstance(data, dict) and (data.get("validations") or str(data.get("status", "")).lower() == "failed"):
        raise OpnsenseAPIError(f"Snapshot löschen von Firewall nicht bestätigt: {redact_data(data)}")


# ── Diagnostics (Diagnostics/Api/{Ping,Traceroute,Portprobe,DnsDiagnostics}Controller, read + live 2026-10-09) ──
# Traceroute/portprobe/DNS answer synchronously to the same "set" call that validates the form:
#   {"result": "ok", "response": {...}} or {"result": "failed", "validations": {"<model>.settings.<field>": "..."}}.
# Ping is a job: set (returns a uuid) -> start -> searchJobs shows live counters -> stop -> remove.
# A probe to a closed/blackholed port takes the firewall 10 s, so these calls need a longer timeout.
DIAGNOSTIC_TIMEOUT = 40.0


async def diagnostic_set(host: OpnsenseHost, path: str, model: str, settings: dict) -> dict:
    data = await _request(host, "POST", path, json={model: {"settings": settings}}, timeout=DIAGNOSTIC_TIMEOUT)
    if not isinstance(data, dict):
        raise OpnsenseAPIError("Diagnose: keine auswertbare Antwort der Firewall")
    if data.get("result") != "ok":
        raise OpnsenseDiagnosticRejected(data.get("validations") or {})
    return data


class OpnsenseDiagnosticRejected(OpnsenseAPIError):
    """The firewall refused the input (its own validation); carries {field: message}."""

    def __init__(self, validations: dict):
        super().__init__("Eingabe von der Firewall abgelehnt")
        self.validations = validations


async def ping_job_start(host: OpnsenseHost, settings: dict) -> str:
    data = await diagnostic_set(host, "/diagnostics/ping/set", "ping", settings)
    job = data.get("uuid")
    if not job:
        raise OpnsenseAPIError("Ping: die Firewall hat keine Job-ID geliefert")
    started = await _request(host, "POST", f"/diagnostics/ping/start/{job}", timeout=DIAGNOSTIC_TIMEOUT)
    if not isinstance(started, dict) or started.get("status") != "ok":
        await ping_job_remove(host, job)
        raise OpnsenseAPIError("Ping: der Job konnte nicht gestartet werden")
    return job


async def ping_job_state(host: OpnsenseHost, job: str) -> dict | None:
    data = await _request(host, "GET", "/diagnostics/ping/searchJobs", params={"rowCount": -1})
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return next((r for r in rows if isinstance(r, dict) and r.get("id") == job), None)


async def ping_job_stop(host: OpnsenseHost, job: str) -> None:
    await _request(host, "POST", f"/diagnostics/ping/stop/{job}")


async def ping_job_remove(host: OpnsenseHost, job: str) -> None:
    await _request(host, "POST", f"/diagnostics/ping/remove/{job}")
