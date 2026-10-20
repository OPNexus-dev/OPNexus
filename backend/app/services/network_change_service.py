"""Changes to VLANs, virtual IPs, gateways and static routes of ONE firewall.

Unlike rules, these objects are specific to a firewall (own IPs, own VLAN
layout), so there is no central copy and no multi-host push: the firewall is the
source of truth and every change goes straight to it, but only through a
preview/execute pair:

  preview  reads the live state, computes the diff and the risks, and returns a
           token that covers exactly that state.
  execute  re-reads the live state, refuses if it no longer matches the token,
           refuses risky changes without an explicit acknowledgement, saves a
           fresh config backup, writes the object, applies it, and reads it back
           to check that the firewall really holds what was asked for.

The main risk is cutting the connection OPNexus itself uses (the interface or
address the firewall is reached on, the default route, a route that swallows the
management address). Those cases are detected from the firewall's own
interface list and need the acknowledgement. OPNexus cannot undo a change that
cuts it off, so there is deliberately NO automatic rollback here — the backup
taken before the change is the way back.

Secrets (the CARP password) are never returned, hashed into the token only, and
never written to the audit log.
"""
import asyncio
import hashlib
import ipaddress
import json
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import ErrorCode, api_error
from app.models.host import OpnsenseHost
from app.services import audit_service, backup_store, network_service, opnsense_client
from app.services.host_maintenance_service import ensure_no_maintenance
from app.services.opnsense_client import OpnsenseAPIError

# Only these fields can be sent; anything else is rejected, never forwarded.
FIELDS: dict[str, set[str]] = {
    "vlan": {"if", "tag", "pcp", "proto", "descr"},
    "vip": {"interface", "mode", "network", "vhid", "advbase", "advskew", "password", "descr", "nosync", "peer", "peer6"},
    "gateway": {"disabled", "name", "descr", "interface", "ipprotocol", "gateway", "defaultgw", "fargw",
                "monitor_disable", "monitor", "priority", "weight"},
    "route": {"network", "gateway", "descr", "enabled"},
    "dns_host": {"enabled", "hostname", "domain", "rr", "server", "mx", "mxprio", "addptr", "description"},
    "dhcp_host": {"host", "domain", "local", "ip", "hwaddr", "client_id", "lease_time", "ignore", "descr"},
    "kea_subnet": {"subnet", "pools", "valid_lifetime", "next_server", "description", "option_data.routers",
                   "option_data.domain_name_servers", "option_data.domain_name", "option_data.ntp_servers"},
    "kea_reservation": {"subnet", "ip_address", "hw_address", "client_id", "hostname", "description", "next_server"},
    "tunable": {"tunable", "value", "descr"},
    "cron_job": {"enabled", "minutes", "hours", "days", "months", "weekdays", "who", "command", "parameters", "description"},
    "syslog_destination": {"enabled", "transport", "program", "level", "facility", "hostname", "port", "rfc5424", "description"},
    "gateway_group": {"name", "item", "item2", "item3", "item4", "item5", "trigger", "poolopts", "descr"},
    "lagg": {"members", "primary_member", "proto", "lacp_fast_timeout", "use_flowid", "lagghash", "lacp_strict", "mtu", "descr"},
    "bridge": {"members", "linklocal", "enablestp", "proto", "descr"},
    "loopback": {"deviceId", "description"},
    "gre": {"local-addr", "remote-addr", "tunnel-local-addr", "tunnel-remote-addr", "tunnel-remote-net", "descr"},
    "gif": {"local-addr", "remote-addr", "tunnel-local-addr", "tunnel-remote-addr", "tunnel-remote-net", "descr"},
    "vxlan": {"deviceId", "vxlanid", "vxlanlocal", "vxlanlocalport", "vxlanremote", "vxlanremoteport", "vxlangroup", "vxlandev"},
    "dns_forward": {"enabled", "type", "domain", "server", "port", "verify", "forward_tcp_upstream", "forward_first", "description"},
    "dhcp_range": {"interface", "start_addr", "end_addr", "subnet_mask", "lease_time", "domain_type", "domain", "description"},
    "interface": {"if", "descr", "enable", "type4", "ipaddr", "gateway", "type6", "ipaddrv6", "gatewayv6", "mtu",
                  "blockpriv", "blockbogons", "lock"},
}
SECRET_FIELDS: dict[str, set[str]] = {"vip": {"password"}}
# Section of HostNetwork that must be readable for the kind to be managed on this firewall.
SECTION = {"vlan": "vlans", "vip": "vips", "gateway": "gateways", "route": "routes", "interface": "assignments",
           "dns_host": "dns_hosts", "dhcp_host": "dhcp_hosts", "kea_subnet": "kea_subnets",
           "kea_reservation": "kea_reservations"}
# Columns the generic object lists show (the firewall's own field names). Lists never carry fields outside FIELDS.
OBJECT_COLUMNS: dict[str, list[str]] = {
    "tunable": ["tunable", "value", "default_value", "descr"],
    "cron_job": ["enabled", "minutes", "hours", "days", "months", "weekdays", "command", "description"],
    "syslog_destination": ["enabled", "transport", "hostname", "port", "program", "level", "description"],
    "gateway_group": ["name", "item", "item2", "item3", "trigger", "descr"],
    "lagg": ["laggif", "members", "proto", "descr"],
    "bridge": ["bridgeif", "members", "proto", "descr"],
    "loopback": ["deviceId", "description"],
    "gre": ["greif", "local-addr", "remote-addr", "tunnel-local-addr", "tunnel-remote-addr", "descr"],
    "gif": ["gifif", "local-addr", "remote-addr", "tunnel-local-addr", "tunnel-remote-addr", "descr"],
    "vxlan": ["deviceId", "vxlanid", "vxlanlocal", "vxlanremote", "vxlangroup", "vxlandev"],
    "dns_forward": ["enabled", "type", "domain", "server", "port", "description"],
    "dhcp_range": ["interface", "start_addr", "end_addr", "subnet_mask", "lease_time", "description"],
}
# These change how the network is wired; the others (DNS/DHCP data) cannot cut the connection to the firewall.
WIRING_KINDS = {"vlan", "vip", "gateway", "route", "interface", "lagg", "bridge", "gre", "gif", "vxlan"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_fields(kind: str, fields: dict) -> dict[str, str]:
    allowed = FIELDS[kind]
    clean: dict[str, str] = {}
    for key, value in fields.items():
        if key not in allowed:
            raise api_error(422, ErrorCode.NETWORK_FIELD_INVALID, field=str(key)[:40])
        if isinstance(value, bool):
            value = "1" if value else "0"
        if value is None:
            value = ""
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise api_error(422, ErrorCode.NETWORK_FIELD_INVALID, field=key)
        text = str(value).strip()
        if len(text) > 255 or _CONTROL.search(text):
            raise api_error(422, ErrorCode.NETWORK_FIELD_INVALID, field=key)
        clean[key] = text
    return clean


def _explode(node: dict) -> dict:
    """Kea nests its DHCP options one level deep; treat them as "option_data.<name>" like the whitelist does."""
    out: dict = {}
    for key, value in node.items():
        if key == "option_data" and isinstance(value, dict):
            for child, inner in value.items():
                out[f"option_data.{child}"] = inner
        else:
            out[key] = value
    return out


def flatten(kind: str, node: dict) -> dict[str, str]:
    """OPNsense answers select fields as {option: {selected: 0|1}}; reduce to the selected value(s)."""
    out: dict[str, str] = {}
    for key, value in _explode(node).items():
        if key in SECRET_FIELDS.get(kind, set()):
            continue
        if isinstance(value, dict):
            selected = [k for k, o in value.items() if isinstance(o, dict) and o.get("selected") and k != ""]
            if value and all(isinstance(o, dict) and "selected" in o for o in value.values()):
                out[key] = ",".join(selected)
            continue
        out[key] = "" if value is None else str(value)
    return out


def _ip(text: str):
    try:
        return ipaddress.ip_interface(text.strip()).ip
    except ValueError:
        return None


def _network(text: str):
    try:
        return ipaddress.ip_network(text.strip(), strict=False)
    except ValueError:
        return None


def _risk(code: str, **params) -> dict:
    return {"code": code, "params": params}


def assess(kind: str, action: str, uuid: str | None, before: dict, after: dict,
           net, mgmt_addresses: set, peers: list[str], context: dict | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (risks needing acknowledgement, plain warnings, hard blocks)."""
    risks: list[dict] = []
    warnings: list[dict] = []
    blocks: list[dict] = []
    mgmt_device = net.management_device
    mgmt_iface = next((i for i in net.interfaces if i.device == mgmt_device), None)
    mgmt_ident = mgmt_iface.identifier if mgmt_iface else None

    if SECTION.get(kind) in net.unavailable:
        blocks.append(_risk("api_unavailable", section=SECTION[kind]))
    if mgmt_device is None and kind in WIRING_KINDS:
        # We cannot tell what this change could cut, so treat every change to existing objects as risky.
        if action in ("set", "del"):
            risks.append(_risk("management_unknown"))

    context = context or {}
    if kind == "interface":
        if not net.can_configure_interfaces:
            blocks.append(_risk("interface_api_unavailable"))
        if context.get("pending"):
            blocks.append(_risk("interface_changes_pending"))
        if action in ("set", "del") and mgmt_ident and uuid == mgmt_ident:
            risks.append(_risk("management_interface", device=mgmt_device))
        if action == "del" and before.get("lock") == "1":
            # OPNsense refuses to remove a locked interface; say so now instead of failing at apply time.
            blocks.append(_risk("interface_locked"))
        if action == "del":
            rules = context.get("rules_using") or []
            if rules:
                risks.append(_risk("interface_rules_removed", count=len(rules), names=", ".join(rules[:5])))
        if action in ("set", "del") and uuid in ("wan", "lan") and uuid != mgmt_ident:
            warnings.append(_risk("core_interface", name=uuid))

    if kind in ("lagg", "bridge"):
        members = {m for side in (before, after) for m in side.get("members", "").split(",") if m}
        if mgmt_device and mgmt_device in members:
            risks.append(_risk("management_member", device=mgmt_device))
        elif mgmt_device is None and action in ("set", "del"):
            pass  # already covered by management_unknown above
    if kind == "tunable":
        # sysctl values can change how the whole system behaves (and some only apply after a reboot).
        risks.append(_risk("tunable_change"))
        if action in ("set", "del") and uuid and "-" not in uuid:
            # A built-in default (its id is not a UUID): the firewall would create a new entry instead of
            # changing it, so overriding one is an "add", not an edit.
            blocks.append(_risk("tunable_default_row"))
    if kind == "gateway_group" and action == "del":
        warnings.append(_risk("gateway_group_in_use"))

    if kind == "vlan" and action in ("set", "del"):
        device = next((v.device for v in net.vlans if v.uuid == uuid), None)
        if device and device == mgmt_device:
            risks.append(_risk("management_interface", device=device))

    if kind == "vip":
        for side in (before, after) if action != "add" else (after,):
            ip = _ip(side.get("network", "")) if side.get("network") else None
            if ip is not None and ip in mgmt_addresses:
                risks.append(_risk("management_address", address=str(ip)))
                break
        if "carp" in (before.get("mode"), after.get("mode")):
            warnings.append(_risk("carp_change"))

    if kind == "gateway":
        current = next((g for g in net.gateways if g.uuid == uuid), None)
        if current is not None and action in ("set", "del"):
            if current.is_default:
                risks.append(_risk("default_gateway", name=current.name))
            if mgmt_ident and current.interface == mgmt_ident:
                risks.append(_risk("management_gateway", name=current.name))
        if action in ("add", "set") and after.get("defaultgw") == "1" and (current is None or not current.is_default):
            risks.append(_risk("default_gateway_change"))

    if kind == "route":
        for side in (before, after) if action != "add" else (after,):
            network = _network(side.get("network", "")) if side.get("network") else None
            if network is None:
                continue
            if network.prefixlen == 0:
                risks.append(_risk("default_route", network=str(network)))
                break
            if any(a.version == network.version and a in network for a in mgmt_addresses):
                risks.append(_risk("route_covers_management", network=str(network)))
                break

    if peers:
        warnings.append(_risk("cluster_peers", hosts=", ".join(peers)))
    # De-duplicate while keeping order.
    seen, unique = set(), []
    for r in risks:
        key = json.dumps(r, sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique, warnings, blocks


def _token(host: OpnsenseHost, kind: str, action: str, uuid: str | None, fields: dict, before: dict, mgmt_device) -> str:
    state = {
        "host": str(host.id), "url": host.base_url, "kind": kind, "action": action, "uuid": uuid,
        # hash, so a secret never sits in the token's input in the clear
        "fields": hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest(),
        "before": before, "mgmt": mgmt_device,
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def _public(kind: str, fields: dict) -> dict:
    return {k: ("(gesetzt)" if k in SECRET_FIELDS.get(kind, set()) and v else v) for k, v in fields.items()
            if not (k in SECRET_FIELDS.get(kind, set()) and not v)}


async def _peers(db: AsyncSession, host: OpnsenseHost) -> list[str]:
    if not host.cluster_name:
        return []
    rows = await db.scalars(select(OpnsenseHost.name).where(
        OpnsenseHost.cluster_name == host.cluster_name, OpnsenseHost.id != host.id).order_by(OpnsenseHost.name))
    return list(rows.all())


async def list_objects(host: OpnsenseHost, kind: str) -> dict:
    """Rows of one generic object kind, reduced to the columns above (plus uuid) — never whole rows,
    so nothing the list does not need (or must not show) leaves the server."""
    columns = OBJECT_COLUMNS[kind]
    try:
        rows = await opnsense_client.search_network_objects(host, kind)
    except OpnsenseAPIError as exc:
        if "404" in str(exc):
            return {"available": False, "columns": columns, "rows": []}   # this OPNsense version lacks the API
        raise
    out = []
    for row in rows:
        item = {"uuid": str(row.get("uuid", ""))}
        for col in columns:
            value = row.get(col)
            item[col] = "" if value is None or isinstance(value, (dict, list)) else str(value)
        out.append(item)
    return {"available": True, "columns": columns, "rows": out}


async def _rules_using(host: OpnsenseHost, identifier: str) -> list[str]:
    """Descriptions of the user-made rules that name this interface (OPNsense deletes them with it)."""
    data = await opnsense_client.search_rules(host, all_rows=True)
    rows = data.get("rows", []) if isinstance(data, dict) else []
    return [str(r.get("description") or r.get("uuid")) for r in rows
            if isinstance(r, dict) and not r.get("is_automatic")
            and identifier in str(r.get("interface") or "").split(",")]


async def preview(db: AsyncSession, host: OpnsenseHost, kind: str, action: str, uuid: str | None, fields: dict) -> dict:
    fields = validate_fields(kind, fields)
    if action in ("set", "del") and not uuid:
        raise api_error(404, ErrorCode.NETWORK_ITEM_NOT_FOUND)
    net = await network_service.get_host_network(host)
    before: dict[str, str] = {}
    if action in ("set", "del"):
        node = await opnsense_client.get_network_item(host, kind, uuid)
        if not node or not any(v not in ("", None) for v in node.values() if not isinstance(v, dict)):
            raise api_error(404, ErrorCode.NETWORK_ITEM_NOT_FOUND)
        # Only the fields that may be sent: the rest (e.g. Kea's DDNS key secret) never reaches the token or the audit log.
        before = {k: v for k, v in flatten(kind, node).items() if k in FIELDS[kind]}
    after = {} if action == "del" else ({**before, **fields} if action == "set" else dict(fields))
    mgmt_addresses = await network_service.management_addresses(host)
    context: dict = {}
    if kind == "interface":
        if net.can_configure_interfaces:
            context["pending"] = await opnsense_client.interface_changes_pending(host)
        if action == "del":
            context["rules_using"] = await _rules_using(host, uuid)
    risks, warnings, blocks = assess(kind, action, uuid, before, after, net, mgmt_addresses, await _peers(db, host), context)
    secret = SECRET_FIELDS.get(kind, set())
    changes = []
    for key in sorted(set(before) | set(fields)):
        old, new = before.get(key), fields.get(key) if action != "del" else None
        if action == "del":
            if old not in (None, "") and key in FIELDS[kind]:
                changes.append({"field": key, "before": old, "after": None})
        elif key in fields and (action == "add" or old != new):
            if key in secret:
                changes.append({"field": key, "before": None, "after": "(gesetzt)" if new else None})
            else:
                changes.append({"field": key, "before": old, "after": new})
    token = _token(host, kind, action, uuid, fields, before, net.management_device)
    return {
        "token": token, "kind": kind, "action": action, "uuid": uuid, "host_id": str(host.id), "host_name": host.name,
        "changes": changes, "risks": risks, "warnings": warnings, "blocks": blocks,
        "requires_acknowledgement": bool(risks), "management_device": net.management_device,
        "can_execute": not blocks and (action == "del" or bool(changes)),
        "_fields": fields, "_before": before,
    }


def _matches(expected: dict[str, str], actual: dict[str, str], secret: set[str]) -> list[str]:
    return [k for k, v in expected.items() if k not in secret and actual.get(k, "") != v]


async def _verify(host: OpnsenseHost, kind: str, action: str, uuid: str | None, fields: dict) -> tuple[bool, list[str]]:
    try:
        if action == "del":
            present = await opnsense_client.list_network_item_uuids(host, kind)
            return (uuid not in present), ([] if uuid not in present else ["still_present"])
        actual = flatten(kind, await opnsense_client.get_network_item(host, kind, uuid))
        mismatches = _matches(fields, actual, SECRET_FIELDS.get(kind, set()))
        return (not mismatches), mismatches
    except OpnsenseAPIError:
        return False, ["unreadable"]


async def _carp_vip_active(host: OpnsenseHost, vhid: str, attempts: int = 5) -> bool:
    """True once the firewall reports the VHID as MASTER or BACKUP, i.e. the VIP really is on the interface and
    taking part in CARP (not DISABLED/INIT). The config can look right while the live VIP is gone."""
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(2)
        data = await opnsense_client.get_vip_status(host)
        rows = data.get("rows") if isinstance(data, dict) else None
        states = {str(r.get("vhid")): str(r.get("status", "")).upper() for r in rows or [] if isinstance(r, dict)}
        if states.get(vhid) in ("MASTER", "BACKUP"):
            return True
    return False


async def execute(db: AsyncSession, host: OpnsenseHost, kind: str, action: str, uuid: str | None, fields: dict,
                  token: str, acknowledged: bool) -> dict:
    await ensure_no_maintenance(db, [host])
    try:
        plan = await preview(db, host, kind, action, uuid, fields)
    except OpnsenseAPIError as exc:
        raise api_error(502, ErrorCode.HOST_REQUEST_FAILED, host=host.name) from exc
    if plan["token"] != token:
        raise api_error(409, ErrorCode.ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN)
    if plan["blocks"]:
        raise api_error(409, ErrorCode.NETWORK_CHANGE_BLOCKED, reasons=", ".join(b["code"] for b in plan["blocks"]))
    if not plan["can_execute"]:
        raise api_error(409, ErrorCode.NETWORK_CHANGE_BLOCKED, reasons="nothing_to_change")
    if plan["requires_acknowledgement"] and not acknowledged:
        raise api_error(409, ErrorCode.NETWORK_ACK_REQUIRED, reasons=", ".join(r["code"] for r in plan["risks"]))

    fields = plan["_fields"]
    send = dict(fields)
    if kind == "vip" and action == "set" and plan["_before"].get("network"):
        # VipSettingsController::getVipOverlay blanks address and prefix whenever "network" is missing from
        # the request, so every VIP update must repeat it (found live: a partial set was rejected / could wipe it).
        send.setdefault("network", plan["_before"]["network"])
    # What the object must look like afterwards: for an edit that is everything it had, plus the change.
    expected = {**plan["_before"], **send} if action == "set" else dict(send)
    entity = fields.get("name") or fields.get("descr") or plan["_before"].get("name") or plan["_before"].get("descr") or kind
    audit = {"kind": "network", "action": f"{kind}.{action}"[:40], "entity_id": uuid, "entity_name": entity,
             "host_id": str(host.id), "host_name": host.name}
    details = {"before": plan["_before"], "after": _public(kind, fields) if action != "del" else None,
               "risks": [r["code"] for r in plan["risks"]], "acknowledged": acknowledged}

    # Way back: a config backup taken right before the change. No backup, no change.
    try:
        backup_id = backup_store.save_snapshot(host.id, await opnsense_client.download_backup(host))
    except OpnsenseAPIError as exc:
        await audit_service.persist(**audit, status="aborted", message="Backup vor der Änderung fehlgeschlagen", details=details)
        raise api_error(502, ErrorCode.NETWORK_BACKUP_FAILED, host=host.name) from exc
    details["backup_id"] = backup_id
    await audit_service.persist(**audit, status="started", message=None, details=details)

    try:
        result = await opnsense_client.write_network_item(host, kind, action, uuid, send)
    except OpnsenseAPIError as exc:
        await audit_service.persist(**audit, status="rejected", message=str(exc)[:500], details=details)
        raise api_error(502, ErrorCode.UPSTREAM_REJECTED, reason=str(exc)[:300]) from exc
    new_uuid = uuid or (result.get("uuid") if isinstance(result, dict) else None)
    # A description is not wired into anything: re-applying a CARP VIP for it only risks the live VIP (found
    # live 2026-10-20: the reconfigure after a description edit left the VIP in INIT on both nodes).
    cosmetic = kind == "vip" and action == "set" and {c["field"] for c in plan["changes"]} <= {"descr"}
    carp_live = kind == "vip" and action != "del" and expected.get("mode") == "carp" and not cosmetic
    try:
        if not cosmetic:
            await opnsense_client.reconfigure_network(host, kind)
        live_ok = True
        if carp_live:
            live_ok = await _carp_vip_active(host, str(expected.get("vhid")))
            if not live_ok:  # one more apply usually brings it back up
                await opnsense_client.reconfigure_network(host, kind)
                live_ok = await _carp_vip_active(host, str(expected.get("vhid")))
    except OpnsenseAPIError as exc:
        # Written but not (provably) applied: say so instead of reporting success.
        await audit_service.persist(**audit, status="apply_uncertain", message=str(exc)[:500], details=details)
        raise api_error(502, ErrorCode.NETWORK_APPLY_UNCERTAIN, host=host.name, backup_id=backup_id) from exc

    verified, mismatches = await _verify(host, kind, action, new_uuid, expected)
    if not live_ok:
        verified = False
        mismatches = [*mismatches, "carp_vip_inactive"]
    await audit_service.persist(**audit, status="applied" if verified else "unverified",
                                message=None if verified else ", ".join(mismatches)[:500], details=details)
    return {"status": "applied", "uuid": new_uuid, "verified": verified, "mismatches": mismatches, "backup_id": backup_id}


def _options(value: dict) -> list[dict]:
    return [{"value": k, "label": str(o.get("value", k)) if isinstance(o, dict) else str(k), "selected": bool(isinstance(o, dict) and o.get("selected"))}
            for k, o in value.items()]


async def template(host: OpnsenseHost, kind: str) -> dict:
    """The firewall's own blank form for this kind: field defaults and, for select fields, the choices it
    accepts right now (e.g. only the gateways it considers valid for a route)."""
    node = _explode(await opnsense_client.get_network_item(host, kind, None))
    out: dict[str, dict] = {}
    for key in sorted(FIELDS[kind]):
        if key not in node:
            continue
        value = node[key]
        if isinstance(value, dict) and value and all(isinstance(o, dict) and "selected" in o for o in value.values()):
            options = _options(value)
            # Free-text list fields (IPs, MACs, DNS servers) arrive in this shape too; the form decides how to show them.
            out[key] = {"value": ",".join(o["value"] for o in options if o["selected"] and o["value"] != ""), "options": options}
        elif key in SECRET_FIELDS.get(kind, set()):
            out[key] = {"value": "", "options": None}
        else:
            out[key] = {"value": "" if value is None or isinstance(value, dict) else str(value), "options": None}
    return out


async def item(host: OpnsenseHost, kind: str, uuid: str) -> dict[str, str]:
    """One object as the firewall holds it right now, reduced to the fields that may be sent (no secrets)."""
    node = await opnsense_client.get_network_item(host, kind, uuid)
    if not node or not any(v not in ("", None) for v in node.values() if not isinstance(v, dict)):
        raise api_error(404, ErrorCode.NETWORK_ITEM_NOT_FOUND)
    return {k: v for k, v in flatten(kind, node).items() if k in FIELDS[kind]}
