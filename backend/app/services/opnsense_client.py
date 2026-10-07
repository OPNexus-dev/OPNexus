"""Thin async client for the OPNsense REST API.

Endpoint paths verified against docs.opnsense.org and the OPNsense core
source (FilterController.php, Diagnostics/Api/InterfaceController.php,
Diagnostics/Api/FirewallController.php) — see README for the full table.
All of these are CORE endpoints; no os-firewall plugin exists or is
required.

Auth is HTTP Basic: username = API key, password = API secret.
"""
import logging
import json
from typing import Any

import httpx

from app.config import settings
from app.models.host import OpnsenseHost
from app.services import opnsense_secret_store, tls_pin_service

logger = logging.getLogger(__name__)


class OpnsenseAPIError(Exception):
    pass


def _client(host: OpnsenseHost) -> httpx.AsyncClient:
    secret = opnsense_secret_store.read_secret(host.id)
    if not secret:
        raise OpnsenseAPIError(f"Kein API-Secret für Host '{host.name}' hinterlegt")
    return httpx.AsyncClient(
        base_url=host.base_url.rstrip("/") + "/api",
        auth=(host.api_key, secret),
        verify=tls_pin_service.verify_argument(host.verify_tls, host.tls_ca_pem),
        timeout=settings.opnsense_timeout,
    )


async def _do_request(host: OpnsenseHost, method: str, path: str, **kwargs) -> httpx.Response:
    async with _client(host) as client:
        try:
            resp = await client.request(method, path, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            raise OpnsenseAPIError(f"{exc.response.status_code} {path}: {exc.response.text}") from exc
        except httpx.HTTPError as exc:
            if host.tls_ca_pem and "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise OpnsenseAPIError(
                    f"Das TLS-Zertifikat von {host.name} passt nicht zum festgesetzten Zertifikat "
                    "(Zertifikat erneuert oder Verbindung manipuliert?)"
                ) from exc
            if isinstance(exc, httpx.RemoteProtocolError):
                # The firewall answered, but the HTTP framing is broken (seen with
                # OPNsense 26.7: kernel TLS corrupts responses above ~64 KiB).
                raise OpnsenseAPIError(
                    f"{host.name} hat eine beschädigte HTTP-Antwort geliefert ({exc}). "
                    "Bei OPNsense 26.x hilft meist `kern.ipc.tls.enable=0` (System ▸ Einstellungen ▸ "
                    "Tunables), siehe Handbuch „Fehlerbehebung“."
                ) from exc
            raise OpnsenseAPIError(f"Verbindung zu {host.name} fehlgeschlagen: {exc}") from exc


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
        detail = json.dumps(data, ensure_ascii=False)[:350]
        raise OpnsenseAPIError(f"{operation} von Firewall nicht bestätigt: {detail}")
    return data


async def add_rule(host: OpnsenseHost, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", "/firewall/filter/addRule", json={"rule": payload}), "Regel anlegen", {"saved"})


async def set_rule(host: OpnsenseHost, rule_uuid: str, payload: dict) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/filter/setRule/{rule_uuid}", json={"rule": payload}), "Regel ändern", {"saved"})


async def del_rule(host: OpnsenseHost, rule_uuid: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/firewall/filter/delRule/{rule_uuid}"), "Regel entfernen", {"deleted", "not found"})


async def apply(host: OpnsenseHost) -> dict:
    return _mutation_result(await _request(host, "POST", "/firewall/filter/apply"), "Änderungen anwenden", set())


async def move_rule_before(host: OpnsenseHost, selected_uuid: str, target_uuid: str) -> dict:
    """`firewall/filter/moveRuleBefore/{selected_uuid}/{target_uuid}`
    (FilterController::moveRuleBeforeAction) — repositions `selected_uuid`
    to sit directly before `target_uuid` via gap-numbered `sequence`
    floats, scoped to rules sharing the same `prio_group` (interface/type).
    Verified against OPNsense core source: no "move to top/bottom"
    sentinel exists, and both UUIDs must resolve to existing rules on the
    SAME interface/type or the controller throws. On success it returns
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
    return _mutation_result(await _request(host, "POST", "/firewall/source_nat/apply"), "Änderungen anwenden", set())


async def get_source_nat_settings(host: OpnsenseHost) -> dict:
    """Read only. SourceNatController::getAction; shape differs by release."""
    return await _request(host, "GET", "/firewall/source_nat/get")


# Core controllers, checked against OPNsense core source:
# https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/controllers/OPNsense/Core/Api/FirmwareController.php
# https://github.com/opnsense/core/blob/master/src/opnsense/mvc/app/controllers/OPNsense/Core/Api/SystemController.php
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


async def reconfigure_ipsec(host: OpnsenseHost) -> dict:
    """`ipsec/service/reconfigure` (ServiceController::reconfigureAction,
    inherited from ApiMutableServiceControllerBase) — soft-reloads a
    running IPsec daemon instead of a full stop/start when possible.
    Deliberately its own call, separate from the 5 config-write calls
    above — see ipsec_deploy_service.py for why this app never bundles it
    silently."""
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
#     VERIFIED: whether `wireguard configure` (the configd action
#     reconfigureAction shells out to) actually brings up any wg-quick
#     interface end-to-end while General.enabled=0 — reconfigureAction's
#     OWN PHP body never reads $internalServiceEnabled at all (it always
#     runs unconditionally), so this depends on configd's own action
#     script content, which is out of the controller/model source layer
#     this app's verification standard covers (same boundary IPsec/
#     OpenVPN draw). Flagged as a real, source-confirmed structural
#     difference from IPsec/OpenVPN (neither of which have an equivalent
#     separate master switch) rather than silently ignored — see README:
#     operators should confirm "VPN > WireGuard > General" is enabled at
#     least once via the OPNsense UI before relying on OPNexus-pushed
#     tunnels.
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


async def reconfigure_wireguard(host: OpnsenseHost) -> dict:
    """`wireguard/service/reconfigure` (ServiceController::reconfigureAction)
    — runs interface registration + template reload + `wireguard configure`.
    Returns `{"result": "ok"}` (confirmed directly from source — see the
    section comment above for why this is NOT assumed symmetric with
    IPsec's `{"status": "ok"}`, and matches OpenVPN's shape instead),
    dispatched via _mutation_result with the "result"-based branch
    (results={"ok"})."""
    return _mutation_result(await _request(host, "POST", "/wireguard/service/reconfigure"), "WireGuard-Dienst neu laden", {"ok"})


# ── Trust-Store certificates (Core, Trust/Api/CertController.php) ───────
# Verified directly against OPNsense core source (raw.githubusercontent.com/
# opnsense/core/master/..., fetched 2026-09-09 by downloading Cert.xml and
# CertController.php directly) — see managed_certificate_service.py's
# module docstring for the full field-mapping writeup.
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
    return _trust_cert_result(await _request(host, "POST", "/trust/cert/addAction", json={"cert": payload}), "Zertifikat anlegen")


async def set_trust_cert(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _trust_cert_result(await _request(host, "POST", f"/trust/cert/setAction/{uuid_}", json={"cert": payload}), "Zertifikat ändern")


async def del_trust_cert(host: OpnsenseHost, uuid_: str) -> dict:
    # NOT dispatched through the {"saved"} results set above — delBase()'s
    # own success shape is {"status": "deleted"}/{"status": "not found"},
    # matching every other del* call in this module (add_alias_item's
    # del_alias_item sibling etc.), so this reuses the same
    # {"deleted", "not found"} results set they all share.
    return _mutation_result(await _request(host, "POST", f"/trust/cert/delAction/{uuid_}"), "Zertifikat entfernen", {"deleted", "not found"})


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
    return _acme_result(await _request(host, "POST", "/acmeclient/accounts/addAction", json={"account": payload}), "ACME-Account anlegen")


async def update_acme_account(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _acme_result(await _request(host, "POST", f"/acmeclient/accounts/updateAction/{uuid_}", json={"account": payload}), "ACME-Account ändern")


async def del_acme_account(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/acmeclient/accounts/delAction/{uuid_}"), "ACME-Account entfernen", {"deleted", "not found"})


async def get_acme_account(host: OpnsenseHost, uuid_: str) -> dict:
    return await _request(host, "GET", f"/acmeclient/accounts/get/{uuid_}")


async def add_acme_certificate(host: OpnsenseHost, payload: dict) -> dict:
    return _acme_result(await _request(host, "POST", "/acmeclient/certificates/addAction", json={"certificate": payload}), "ACME-Zertifikatsanfrage anlegen")


async def update_acme_certificate(host: OpnsenseHost, uuid_: str, payload: dict) -> dict:
    return _acme_result(await _request(host, "POST", f"/acmeclient/certificates/updateAction/{uuid_}", json={"certificate": payload}), "ACME-Zertifikatsanfrage ändern")


async def del_acme_certificate(host: OpnsenseHost, uuid_: str) -> dict:
    return _mutation_result(await _request(host, "POST", f"/acmeclient/certificates/delAction/{uuid_}"), "ACME-Zertifikatsanfrage entfernen", {"deleted", "not found"})


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
    raise OpnsenseAPIError(f"{operation}: unerwartete Antwortform der Firewall: {json.dumps(data, ensure_ascii=False)[:350]}")


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
