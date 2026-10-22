"""Rule usage from pf's counters: which rules never match anything?

``sample_host`` reads the per-rule packet counters and the configured rules (``searchRule``) and updates one
row per rule: the time of the last observed hit. The counters come from ``firewall/filter_util/ruleStats``
(already summed per label by the firewall; verified live on OPNsense 26.7 on all three test firewalls, see
the evidence block in opnsense_client.py, where its numbers equal the sum over ``pfStatistics/rules``). Older
releases without that endpoint fall back to parsing ``diagnostics/firewall/pfStatistics/rules``.

* A counter that grew since the last sample is a hit "now" (at most one sampling interval late).
* A counter that is *lower* than before means pf reloaded its ruleset (an apply or a reboot), which
  zeroes everything. Whatever it shows above zero was hit since the reload, so that also counts as a hit now;
  zero tells nothing, the old ``last_hit_at`` stays.
* A rule is called unused only after OPNexus has watched it for ``UNUSED_AFTER_DAYS`` without a hit.
  A new rule, or a brand-new installation, is "still collecting data" instead of a false alarm.

A counter belongs to a configured rule by its label: the UUID of a rule created in the rule editor or API, or the
32-hex label of a not yet migrated legacy rule (the ``searchRule`` row carries the same string as ``uuid``).
OPNsense's built-in automatic rules also have hash labels but are flagged ``is_automatic`` there, so they are
never tracked.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.rule_hit import RuleHit
from app.services import opnsense_client as client
from app.services.opnsense_client import OpnsenseAPIError

logger = logging.getLogger(__name__)

UNUSED_AFTER_DAYS = 30
_LABEL = re.compile(r'label "([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})"')
# A sample counts as fresh for the "unused" confidence when it is at most this old.
SAMPLE_FRESH = timedelta(hours=24)
# A pf reload zeroes the counters; whatever was hit between the last sample and the reload is lost. After a gap
# this long the loss is too big to call the rule unused on that evidence: the observation window starts over.
SAMPLE_GAP = timedelta(hours=2)


def parse_counters(stats: dict) -> dict[str, dict]:
    """{rule uuid: {packets, bytes, evaluations}} summed over every pf line a rule expands to."""
    lines = ((stats or {}).get("rules") or {}).get("filter rules") or {}
    result: dict[str, dict] = {}
    for text, values in lines.items():
        match = _LABEL.search(text)
        if not match or not isinstance(values, dict):
            continue
        entry = result.setdefault(match.group(1).lower(), {"packets": 0, "bytes": 0, "evaluations": 0})
        for key in entry:
            entry[key] += int(values.get(key) or 0)
    return result


def parse_rule_stats(data: dict) -> dict[str, dict] | None:
    """{label: {packets, bytes, evaluations}} from ``filter_util/ruleStats``; None when the answer is not
    that shape (older release, error page), so the caller can fall back to pfStatistics."""
    stats = (data or {}).get("stats") if isinstance(data, dict) else None
    if not isinstance(stats, dict) or (data or {}).get("status") != "ok":
        return None
    result: dict[str, dict] = {}
    for label, values in stats.items():
        if isinstance(values, dict):
            result[str(label).lower()] = {key: int(values.get(key) or 0) for key in ("packets", "bytes", "evaluations")}
    return result


async def read_counters(host: OpnsenseHost) -> dict[str, dict]:
    """Counters per rule label: ruleStats first, the pf ruleset text as fallback."""
    try:
        parsed = parse_rule_stats(await client.get_rule_stats(host))
    except OpnsenseAPIError:
        parsed = None
    if parsed is not None:
        return parsed
    return parse_counters(await client.get_pf_rule_statistics(host))


def looks_complete(search) -> bool:
    """A usable ``searchRule`` answer: a list that contains OPNsense's own automatic rules. A real firewall always
    has some, so an empty or partial answer (a firewall just restarting a service, a proxy error page) is rejected
    instead of being read as "every rule is gone"."""
    rows = search.get("rows") if isinstance(search, dict) else None
    if not isinstance(rows, list) or not rows:
        return False
    dicts = [r for r in rows if isinstance(r, dict)]
    if not any("is_automatic" in r for r in dicts):
        return bool(dicts)  # firmware that does not flag automatic rules at all: a non-empty list is all we can ask for
    return any(r.get("is_automatic") in (True, 1, "1") for r in dicts)


def configured_rules(search: dict) -> dict[str, dict]:
    rows = (search or {}).get("rows") or []
    return {
        str(r["uuid"]).lower(): {"description": r.get("description") or "", "enabled": r.get("enabled") in ("1", 1, True)}
        for r in rows if r.get("uuid") and r.get("is_automatic") not in (True, 1, "1")
    }


async def sample_host(db: AsyncSession, host: OpnsenseHost, now: datetime | None = None) -> int:
    """Read the counters and the rules from the firewall and update the hit rows."""
    counters = await read_counters(host)
    return await apply_sample(db, host, counters, await client.search_rules(host, all_rows=True), now)


async def apply_sample(db: AsyncSession, host: OpnsenseHost, counters: dict[str, dict], search: dict,
                       now: datetime | None = None) -> int:
    """Update the hit rows from counters (per label) and a ``searchRule`` answer that are already read
    (the observe loop shares one read between usage and change history)."""
    now = now or datetime.now(timezone.utc)
    if not looks_complete(search):
        return 0  # not a full rule list: an error or partial answer must not look like "every rule was deleted"
    rules = configured_rules(search)
    rows = {r.rule_uuid: r for r in (await db.scalars(select(RuleHit).where(RuleHit.host_id == host.id))).all()}
    for uuid_, meta in rules.items():
        counter = counters.get(uuid_, {"packets": 0, "bytes": 0})
        row = rows.get(uuid_)
        if row is None:
            row = RuleHit(host_id=host.id, rule_uuid=uuid_, first_seen_at=now, last_packets=0, last_bytes=0, last_sampled_at=now)
            db.add(row)
            if counter["packets"] > 0:
                row.last_hit_at = now  # already matching when first seen
        elif counter["packets"] > row.last_packets or (counter["packets"] < row.last_packets and counter["packets"] > 0):
            row.last_hit_at = now
        elif counter["packets"] < row.last_packets and now - row.last_sampled_at > SAMPLE_GAP:
            row.first_seen_at = now  # reloaded after a long gap: the hits in between are unknown
        if meta["enabled"] and not row.enabled:
            row.first_seen_at = now  # a rule that was off has not been "waiting for traffic" while it was off
        row.description = meta["description"][:255]
        row.enabled = meta["enabled"]
        row.last_packets, row.last_bytes, row.last_sampled_at = counter["packets"], counter["bytes"], now
    for uuid_, row in rows.items():  # rule deleted on the firewall
        if uuid_ not in rules:
            await db.delete(row)
    await db.commit()
    return len(rules)


async def restart_window(db: AsyncSession, host_id, rule_uuids, now: datetime | None = None) -> int:
    """The rules were edited, enabled or created: what OPNexus saw before does not describe them any more, so the
    observation window for "unused" starts over (an earlier last hit is kept)."""
    keys = [u.lower() for u in rule_uuids]
    if not keys:
        return 0
    result = await db.execute(update(RuleHit).where(RuleHit.host_id == host_id, RuleHit.rule_uuid.in_(keys))
                              .values(first_seen_at=now or datetime.now(timezone.utc)).execution_options(synchronize_session=False))
    await db.commit()
    return result.rowcount or 0


async def sample_all(db: AsyncSession) -> int:
    hosts = (await db.scalars(select(OpnsenseHost).where(OpnsenseHost.enabled.is_(True)))).all()
    sampled = 0
    for host in hosts:
        try:
            await sample_host(db, host)
            sampled += 1
        except OpnsenseAPIError as exc:
            logger.warning("Regelnutzung von %s nicht lesbar: %s", host.name, exc)
        except Exception:
            logger.exception("Regelnutzung von %s fehlgeschlagen", host.name)
    return sampled


def classify(row: RuleHit, now: datetime, days: int = UNUSED_AFTER_DAYS) -> str:
    """'used' | 'unused' | 'collecting' | 'disabled'; ``days`` is the observation window."""
    if not row.enabled:
        return "disabled"
    if row.last_hit_at is not None and now - row.last_hit_at < timedelta(days=days):
        return "used"
    watched = now - row.first_seen_at
    if watched < timedelta(days=days):
        return "used" if row.last_hit_at is not None else "collecting"
    return "unused"


async def unused_summary(db: AsyncSession, host_id) -> dict | None:
    """For the security check: None while nothing is known yet, else {"ready": bool, "rules": [names]}."""
    rows = (await db.scalars(select(RuleHit).where(RuleHit.host_id == host_id))).all()
    if not rows:
        return None
    now = datetime.now(timezone.utc)
    states = [(r, classify(r, now)) for r in rows]
    unused = [r.description or r.rule_uuid for r, s in states if s == "unused"]
    watching = [s for _, s in states if s == "collecting"]
    return {"unused": unused, "collecting": len(watching)}
