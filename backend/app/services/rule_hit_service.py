"""Rule usage from pf's counters: which rules never match anything?

``sample_host`` reads the per-rule packet counters (``diagnostics/firewall/pfStatistics/rules``) and the
configured rules (``searchRule``) and updates one row per rule: the time of the last observed hit.

* A counter that grew since the last sample is a hit "now" (at most one sampling interval late).
* A counter that is *lower* than before means pf reloaded its ruleset (an apply or a reboot), which
  zeroes everything. Whatever it shows above zero was hit since the reload, so that also counts as a hit now;
  zero tells nothing, the old ``last_hit_at`` stays.
* A rule is called unused only after OPNexus has watched it for ``UNUSED_AFTER_DAYS`` without a hit.
  A new rule, or a brand-new installation, is "still collecting data" instead of a false alarm.

Only rules with a UUID label are tied to a configured rule (rules created in the rule editor or API; OPNsense's
built-in automatic rules carry hash labels and are not user rules).
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import OpnsenseHost
from app.models.rule_hit import RuleHit
from app.services import opnsense_client as client
from app.services.opnsense_client import OpnsenseAPIError

logger = logging.getLogger(__name__)

UNUSED_AFTER_DAYS = 30
_LABEL = re.compile(r'label "([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"')


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


def configured_rules(search: dict) -> dict[str, dict]:
    rows = (search or {}).get("rows") or []
    return {
        str(r["uuid"]).lower(): {"description": r.get("description") or "", "enabled": r.get("enabled") in ("1", 1, True)}
        for r in rows if r.get("uuid") and not r.get("is_automatic")
    }


async def sample_host(db: AsyncSession, host: OpnsenseHost, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    counters = parse_counters(await client.get_pf_rule_statistics(host))
    rules = configured_rules(await client.search_rules(host, all_rows=True))
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
        row.description = meta["description"][:255]
        row.enabled = meta["enabled"]
        row.last_packets, row.last_bytes, row.last_sampled_at = counter["packets"], counter["bytes"], now
    for uuid_, row in rows.items():  # rule deleted on the firewall
        if uuid_ not in rules:
            await db.delete(row)
    await db.commit()
    return len(rules)


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


def classify(row: RuleHit, now: datetime) -> str:
    """'used' | 'unused' | 'collecting' | 'disabled'"""
    if not row.enabled:
        return "disabled"
    if row.last_hit_at is not None and now - row.last_hit_at < timedelta(days=UNUSED_AFTER_DAYS):
        return "used"
    watched = now - row.first_seen_at
    if watched < timedelta(days=UNUSED_AFTER_DAYS):
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
