"""Security posture of a firewall: hardening checks and rule hygiene.

The checks read the firewall's own config.xml (one read-only download, parsed
in memory with defusedxml, never stored) plus two facts OPNexus already knows:
whether firmware updates are pending and how old the newest local backup is.
The XML layout was verified against real OPNsense 26.7 configs: rules live in
``OPNsense/Firewall/Filter/rules/rule`` (MVC) or, on installs not yet migrated,
in ``filter/rule`` (legacy). Both are normalised to the same shape below.

``evaluate`` is pure (bytes in, findings out). A finding is
``{id, severity, passed, params}``; ``passed`` is None when a check could not be
decided. The score starts at 100 and loses a penalty per failed check, so one
critical finding (an any-any pass rule on the WAN) already turns the host red.
"""
import re
from datetime import datetime, timezone
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as safe_ET
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compliance_result import ComplianceResult
from app.models.host import OpnsenseHost
from app.services import backup_store, rule_hit_service
from app.services import opnsense_client as client
from app.services.opnsense_client import OpnsenseAPIError

MAX_CONFIG_BYTES = 32 * 1024 * 1024
BACKUP_MAX_AGE_DAYS = 7
PENALTY = {"critical": 30, "high": 15, "medium": 8, "low": 3, "info": 0}
SEVERITY_ORDER = ("critical", "high", "medium", "low", "info")
MAX_EXAMPLES = 5


def grade(score: int) -> str:
    return "good" if score >= 90 else "fair" if score >= 70 else "poor"


def _text(el: Element | None, path: str) -> str | None:
    node = el.find(path) if el is not None else None
    if node is None or node.text is None or not node.text.strip():
        return None
    return node.text.strip()


# ── Rule normalisation ───────────────────────────────────────────────────

def _endpoint(node: Element | None) -> tuple[str, bool, str | None]:
    """(network, negated, port) of a legacy <source>/<destination> element."""
    if node is None:
        return "any", False, None
    negated = node.find("not") is not None
    port = _text(node, "port")
    if node.find("any") is not None:
        return "any", negated, port
    for tag in ("network", "address"):
        value = _text(node, tag)
        if value:
            return value, negated, port
    return "any", negated, port


def _mvc_rule(rule: Element) -> dict:
    return {
        "enabled": _text(rule, "enabled") != "0",
        "action": (_text(rule, "action") or "pass").lower(),
        "interfaces": [i for i in (_text(rule, "interface") or "").split(",") if i],
        "direction": _text(rule, "direction") or "in",
        "ipprotocol": _text(rule, "ipprotocol") or "inet",
        "protocol": (_text(rule, "protocol") or "any").lower(),
        "src": _text(rule, "source_net") or "any", "src_not": _text(rule, "source_not") == "1",
        "sport": _text(rule, "source_port"),
        "dst": _text(rule, "destination_net") or "any", "dst_not": _text(rule, "destination_not") == "1",
        "dport": _text(rule, "destination_port"),
        "floating": False,
        "quick": _text(rule, "quick") != "0",
        "descr": _text(rule, "description") or "",
        "order": int(_text(rule, "sequence") or 0),
    }


def _legacy_rule(rule: Element, index: int) -> dict:
    src, src_not, sport = _endpoint(rule.find("source"))
    dst, dst_not, dport = _endpoint(rule.find("destination"))
    return {
        "enabled": rule.find("disabled") is None,
        "action": (_text(rule, "type") or "pass").lower(),
        "interfaces": [i for i in (_text(rule, "interface") or "").split(",") if i],
        "direction": _text(rule, "direction") or "in",
        "ipprotocol": _text(rule, "ipprotocol") or "inet",
        "protocol": (_text(rule, "protocol") or "any").lower(),
        "src": src, "src_not": src_not, "sport": sport, "dst": dst, "dst_not": dst_not, "dport": dport,
        "floating": _text(rule, "floating") == "yes",
        "quick": _text(rule, "quick") != "0",
        "descr": _text(rule, "descr") or "",
        "order": index,
    }


def extract_rules(root: Element) -> list[list[dict]]:
    """Rule lists in evaluation order, one per source (MVC, legacy)."""
    mvc = sorted((_mvc_rule(r) for r in root.findall("OPNsense/Firewall/Filter/rules/rule")), key=lambda r: r["order"])
    legacy = [_legacy_rule(r, i) for i, r in enumerate(root.findall("filter/rule"))]
    return [group for group in (mvc, legacy) if group]


def wan_interfaces(root: Element) -> set[str]:
    """Interfaces facing the outside: the one named wan*, or any with an upstream gateway."""
    result = set()
    for node in root.findall("interfaces/*"):
        if node.tag.lower().startswith("wan") or _text(node, "gateway"):
            result.add(node.tag)
    return result


def _is_any_any(rule: dict) -> bool:
    return (
        rule["enabled"] and rule["action"] == "pass" and rule["src"] == "any" and not rule["src_not"]
        and rule["dst"] == "any" and not rule["dst_not"] and rule["protocol"] == "any"
        and not rule["sport"] and not rule["dport"]
    )



def _covers(a: dict, b: dict) -> bool:
    """Every packet matched by rule b is already matched by the earlier rule a.
    Only exact-or-any comparisons, so it never claims coverage it cannot prove."""
    if not (a["enabled"] and b["enabled"]) or a["floating"] or b["floating"] or not a["quick"]:
        return False
    if not a["interfaces"] or not set(b["interfaces"]) <= set(a["interfaces"]):
        return False
    if a["direction"] != b["direction"] or a["ipprotocol"] != b["ipprotocol"]:
        return False
    if a["protocol"] != "any" and a["protocol"] != b["protocol"]:
        return False
    for side in ("src", "dst"):
        if a[side + "_not"] or b[side + "_not"]:
            if (a[side], a[side + "_not"]) != (b[side], b[side + "_not"]):
                return False
        elif a[side] != "any" and a[side] != b[side]:
            return False
    for port in ("sport", "dport"):
        if a[port] and a[port] != b[port]:
            return False
    return True


def _label(rule: dict) -> str:
    return rule["descr"] or f"{rule['action']} {rule['src']} → {rule['dst']}"


def analyse_rules(groups: list[list[dict]]) -> dict:
    shadowed, duplicates = [], []
    for rules in groups:
        for j, later in enumerate(rules):
            for earlier in rules[:j]:
                if _covers(earlier, later):
                    (duplicates if earlier["action"] == later["action"] and _covers(later, earlier) else shadowed).append(_label(later))
                    break
    return {"shadowed": shadowed, "duplicates": duplicates}


# ── Checks ───────────────────────────────────────────────────────────────

def _finding(check_id: str, severity: str, passed: bool | None, **params) -> dict:
    return {"id": check_id, "severity": severity, "passed": passed, "params": params}


def _listing(items: list[str]) -> dict:
    return {"count": len(items), "examples": items[:MAX_EXAMPLES]}


def evaluate(xml_bytes: bytes, *, firmware_status: str | None, newest_backup_age_s: float | None,
             unused_rules: dict | None = None) -> list[dict]:
    if len(xml_bytes) > MAX_CONFIG_BYTES or not xml_bytes.strip():
        raise ValueError("config.xml missing or too large")
    root = safe_ET.fromstring(xml_bytes)
    if root.tag != "opnsense":
        raise ValueError("not an OPNsense config.xml")
    findings: list[dict] = []

    # Management access
    protocol = _text(root, "system/webgui/protocol")
    findings.append(_finding("webgui_https", "high", protocol == "https", protocol=protocol or "http"))

    ssh = root.find("system/ssh")
    ssh_enabled = ssh is not None and _text(ssh, "enabled") in ("enabled", "1")
    root_login = ssh_enabled and _text(ssh, "permitrootlogin") == "1"
    password_login = ssh_enabled and _text(ssh, "passwordauth") == "1"
    if root_login and password_login:
        findings.append(_finding("ssh_hardened", "high", False, reason="root_password"))
    elif password_login:
        findings.append(_finding("ssh_hardened", "medium", False, reason="password"))
    else:
        findings.append(_finding("ssh_hardened", "high", True))

    community = _text(root, "snmpd/rocommunity")
    if root.find("snmpd") is not None:
        findings.append(_finding("snmp_community", "high", (community or "").lower() not in ("", "public")))

    # Rules
    wans = wan_interfaces(root)
    groups = extract_rules(root)
    all_rules = [rule for group in groups for rule in group]
    wan_open = [_label(r) for r in all_rules if _is_any_any(r) and set(r["interfaces"]) & wans]
    findings.append(_finding("wan_any_any", "critical", not wan_open, **_listing(wan_open)))
    inner_open = [_label(r) for r in all_rules if _is_any_any(r) and r["interfaces"] and not set(r["interfaces"]) & wans]
    findings.append(_finding("internal_any_any", "medium", not inner_open, **_listing(inner_open)))
    analysis = analyse_rules(groups)
    findings.append(_finding("rules_shadowed", "medium", not analysis["shadowed"], **_listing(analysis["shadowed"])))
    findings.append(_finding("rules_duplicate", "low", not analysis["duplicates"], **_listing(analysis["duplicates"])))
    undocumented = [f"{r['action']} {r['src']} → {r['dst']}" for r in all_rules if r["enabled"] and not r["descr"]]
    findings.append(_finding("rules_documented", "low", not undocumented, **_listing(undocumented)))

    # Usage (from the pf counters OPNexus has been sampling; None = no data yet)
    if unused_rules is not None:
        names = unused_rules["unused"]
        findings.append(_finding("rules_unused", "low", None if (not names and unused_rules["collecting"]) else not names, **_listing(names)))

    # Operations
    findings.append(_finding("ntp_configured", "low", bool(_text(root, "system/timeservers"))))
    syslog_targets = [d for d in root.findall("OPNsense/Syslog/destinations/destination") if _text(d, "enabled") != "0"]
    findings.append(_finding("remote_logging", "low", bool(syslog_targets) or bool(_text(root, "syslog/remoteserver"))))
    findings.append(_finding("ids_enabled", "info", _text(root, "OPNsense/IDS/general/enabled") == "1"))

    # From what OPNexus already knows
    findings.append(_finding("firmware_current", "medium", None if firmware_status is None else firmware_status != "update"))
    if newest_backup_age_s is None:
        findings.append(_finding("backup_recent", "medium", False, age_days=None))
    else:
        age_days = int(newest_backup_age_s // 86400)
        findings.append(_finding("backup_recent", "medium", age_days < BACKUP_MAX_AGE_DAYS, age_days=age_days))
    return findings


def score(findings: list[dict]) -> int:
    return max(0, 100 - sum(PENALTY[f["severity"]] for f in findings if f["passed"] is False))


def counts(findings: list[dict]) -> dict:
    result = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in findings:
        if finding["passed"] is False:
            result[finding["severity"]] += 1
    return result


# ── Running it for a host ────────────────────────────────────────────────

async def check_host(db: AsyncSession, host: OpnsenseHost) -> ComplianceResult:
    """Download the config, evaluate, store (one row per host, replaced each time)."""
    row = await db.scalar(select(ComplianceResult).where(ComplianceResult.host_id == host.id))
    if row is None:
        row = ComplianceResult(host_id=host.id)
        db.add(row)
    row.checked_at = datetime.now(timezone.utc)
    try:
        xml = await client.download_backup(host)
        try:
            firmware_status = (await client.get_firmware_status(host)).get("status")
        except OpnsenseAPIError:
            firmware_status = None
        snapshots = backup_store.list_snapshots(host.id)
        age = (datetime.now(timezone.utc) - snapshots[0]["created_at"]).total_seconds() if snapshots else None
        try:
            await rule_hit_service.sample_host(db, host)
        except OpnsenseAPIError:
            pass  # usage data is a bonus; the check does not depend on it
        findings = evaluate(xml, firmware_status=firmware_status, newest_backup_age_s=age,
                            unused_rules=await rule_hit_service.unused_summary(db, host.id))
        row.findings, row.score, row.error = findings, score(findings), None
    except OpnsenseAPIError as exc:
        row.findings, row.score, row.error = [], None, "unreachable"
        row.error_detail = str(exc)[:300]
    except ValueError as exc:
        row.findings, row.score, row.error = [], None, "invalid_config"
        row.error_detail = str(exc)[:300]
    await db.commit()
    return row


async def check_all(db: AsyncSession) -> int:
    hosts = (await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)).order_by(OpnsenseHost.name))).all()
    for host in hosts:
        await check_host(db, host)
    return len(hosts)
