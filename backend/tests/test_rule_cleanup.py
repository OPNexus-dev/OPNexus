"""Rule cleanup (1.31, B4 part 2): preview, deploy, confirm, revert/sweeper, delete and restore against a stateful
fake firewall (tests/rule_cleanup_fake.py) and the test database. Numbers refer to DESIGN-1.31 §6. Roles and API
tokens are checked in the router (package 6), not here."""
import asyncio
import json
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select, update

from app.config import settings
from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.governance import GovernanceSettings
from app.models.host import OpnsenseHost
from app.models.host_operation import HostOperation
from app.models.rule_cleanup import RuleCleanupItem, RuleCleanupOperation
from app.models.rule_hit import RuleHit
from app.models.rule_history import LiveRuleState, RuleChangeEvent
from app.services import rule_cleanup_service as svc
from app.services import rule_history_service as rhs
from app.services.rule_analysis import context as ctx_mod
from tests.rule_cleanup_fake import (
    AUTO, MGMT_SOURCE, R_ALIAS, R_BASE, R_BLOCK, R_CARP, R_DUP, R_LEGACY, R_MGMT, R_TAIL, R_UNUSED, FakeFirewall,
    default_rules, rule,
)
from tests.test_rules import _create_host

FW1, FW2 = "https://203.0.113.57", "https://203.0.113.58"
UNUSED_SEEDED = (R_UNUSED, R_BLOCK, R_MGMT, R_CARP, R_ALIAS, R_TAIL)


@pytest.fixture(autouse=True)
def _fresh_cache():
    ctx_mod.clear_cache()
    yield
    ctx_mod.clear_cache()


def _now():
    return datetime.now(timezone.utc)


async def _settings(sources=(MGMT_SOURCE,), max_rules=10, min_days=7, ports=()):
    async with SessionLocal() as db:
        row = await db.get(GovernanceSettings, 1)
        if row is None:
            row = GovernanceSettings(id=1, operators_may_deploy=True)
            db.add(row)
        row.cleanup_management_sources = list(sources)
        row.cleanup_management_ports = list(ports)
        row.cleanup_max_rules, row.cleanup_delete_min_days = max_rules, min_days
        await db.commit()


async def _seed_hits(host_id, uuids, *, days=100, hit_days=None, sampled_minutes=10):
    async with SessionLocal() as db:
        now = _now()
        for u in uuids:
            db.add(RuleHit(host_id=host_id, rule_uuid=u.lower(), description=u, enabled=True,
                           first_seen_at=now - timedelta(days=days), last_packets=0, last_bytes=0,
                           last_sampled_at=now - timedelta(minutes=sampled_minutes),
                           last_hit_at=None if hit_days is None else now - timedelta(days=hit_days)))
        await db.commit()


async def call(host_id, fn, *args, **kw):
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, host_id)
        return await fn(db, host, *args, **kw)


async def refused(host_id, fn, *args, **kw) -> dict:
    with pytest.raises(HTTPException) as info:
        await call(host_id, fn, *args, **kw)
    return {"status": info.value.status_code, **info.value.detail}


async def preview(host_id, uuids, actor="admin", **kw):
    kw.setdefault("kind", "disable")
    return await call(host_id, svc.preview, actor=actor, uuids=uuids, **kw)


async def deploy(host_id, preview_id, actor="admin", name=None):
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, host_id)
        return await svc.deploy(db, host, uuid_lib.UUID(preview_id), actor=actor, confirm_name=name or host.name)


async def _items(op_id):
    async with SessionLocal() as db:
        return {i.rule_uuid: i for i in (await db.scalars(select(RuleCleanupItem).where(
            RuleCleanupItem.operation_id == uuid_lib.UUID(str(op_id))))).all()}


async def _op(op_id):
    async with SessionLocal() as db:
        return await db.get(RuleCleanupOperation, uuid_lib.UUID(str(op_id)))


async def _backdate(op_id, **fields):
    async with SessionLocal() as db:
        await db.execute(update(RuleCleanupOperation).where(RuleCleanupOperation.id == uuid_lib.UUID(str(op_id))).values(**fields))
        await db.commit()


async def _sweep():
    async with SessionLocal() as db:
        return await svc.sweep_expired_cleanups(db)


@pytest_asyncio.fixture
async def env(client, respx_mock):
    log: list = []
    fw = FakeFirewall(FW1, log=log).mount(respx_mock)
    host_id = uuid_lib.UUID((await _create_host(client, "fw-a", FW1))["id"])
    await _settings()
    await _seed_hits(host_id, UNUSED_SEEDED)
    fw.calls.clear()
    log.clear()
    return SimpleNamespace(fw=fw, host_id=host_id, log=log)


@pytest_asyncio.fixture
async def cluster(client, respx_mock):
    log: list = []
    backup = FakeFirewall(FW2, role="BACKUP", pfsync="lan", peer="10.20.30.2", lan_addr="10.20.30.3",
                          wan_addr="203.0.113.58", log=log).mount(respx_mock)
    master = FakeFirewall(FW1, role="MASTER", pfsync="lan", peer="10.20.30.3", log=log).mount(respx_mock)
    a = uuid_lib.UUID((await _create_host(client, "fw-a", FW1))["id"])
    b = uuid_lib.UUID((await _create_host(client, "fw-b", FW2))["id"])
    async with SessionLocal() as db:
        await db.execute(update(OpnsenseHost).where(OpnsenseHost.id.in_([a, b])).values(cluster_name="ha-test"))
        await db.commit()
    await _settings()
    await _seed_hits(a, UNUSED_SEEDED)
    await _seed_hits(b, UNUSED_SEEDED)
    for f in (master, backup):
        f.calls.clear()
    log.clear()
    return SimpleNamespace(master=master, backup=backup, a=a, b=b, log=log)


def _writes(log):
    return [(base, path) for base, method, path in log if method == "POST"]


# ── Preview ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preview_recomputes_from_the_live_firewall_and_stores_a_bound_token(env):
    result = await preview(env.host_id, [R_UNUSED, R_DUP])
    plan = result["plan"]
    items = {i["uuid"]: i for i in plan["nodes"][0]["items"]}
    assert set(items) == {R_UNUSED, R_DUP}
    assert items[R_UNUSED]["level"] == "sure" and items[R_UNUSED]["protection"]["result"] == "false"
    assert items[R_DUP]["cover_uuid"] == R_BASE and items[R_DUP]["cover_fp"]
    assert env.fw.writes() == []  # nothing is written by a preview
    assert ("GET", "/firewall/filter/searchRule") in env.fw.calls  # read live, not from a cache or the request
    op = await _op(result["preview_id"])
    assert op.status == "preview" and op.actor == "admin" and len(op.preview_fingerprint) == 64
    assert timedelta(minutes=4) < op.expires_at - _now() <= timedelta(minutes=5)
    async with SessionLocal() as db:
        audit = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "rule_cleanup"))).all()
    assert [(a.action, a.status) for a in audit] == [("preview", "requested")]
    assert "remote_uuid" not in audit[0].details  # a preview never appears as a change in the rule history
    # a second preview of the same actor replaces the first
    again = await preview(env.host_id, [R_UNUSED])
    assert (await _op(result["preview_id"])).status == "cancelled" and (await _op(again["preview_id"])).status == "preview"


@pytest.mark.asyncio
async def test_without_management_sources_everything_is_locked(env):
    await _settings(sources=())
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_VERWALTUNGSQUELLEN_FEHLEN"
    await _settings(sources=("not-an-address",))
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_VERWALTUNGSQUELLEN_FEHLEN"


@pytest.mark.asyncio
@pytest.mark.parametrize("uuid,reason", [
    (R_MGMT, "management"),          # allows the management source to the firewall's 443
    (R_ALIAS, "management"),         # source is a URL table: match unknown = protected (case 18)
    (R_CARP, "infrastructure"),      # CARP protocol
    (R_BASE, "not_candidate"),       # nothing says it is removable
])
async def test_rules_that_must_not_be_switched_off_are_refused(env, uuid, reason):
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[uuid], acknowledge=[uuid])
    assert got["status"] == 409 and got["code"] == "BEREINIGUNG_REGEL_NICHT_AENDERBAR"
    assert got["params"]["reason"] == reason and got["params"]["uuid"] == uuid
    assert env.fw.writes() == []


@pytest.mark.asyncio
async def test_managed_legacy_and_unknown_confidence_are_refused(env):
    from app.models.rule import FirewallRule
    from app.models.rule_deployment import RuleDeployment
    env.fw.rules.append(rule(R_LEGACY, 900, src="10.20.30.0/24", dport="6000", legacy=True))
    await _seed_hits(env.host_id, [R_LEGACY])
    async with SessionLocal() as db:
        managed = FirewallRule(description="Managed", interface="lan", action="pass", direction="in", protocol="tcp",
                               source_net="any", destination_net="any", destination_port="8081")
        db.add(managed)
        await db.flush()
        db.add(RuleDeployment(rule_id=managed.id, host_id=env.host_id, status="success", opnsense_rule_uuid=R_UNUSED))
        # stale counters (sampled two days ago): "unused" is unknown, never selectable
        await db.execute(update(RuleHit).where(RuleHit.rule_uuid == R_TAIL).values(last_sampled_at=_now() - timedelta(days=2)))
        await db.commit()
    for uuid, reason in ((R_UNUSED, "managed"), (R_LEGACY, "legacy"), (R_TAIL, "confidence")):
        got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[uuid], acknowledge=[uuid])
        assert got["params"]["reason"] == reason, (uuid, got)


@pytest.mark.asyncio
async def test_probable_needs_an_explicit_acknowledgement_per_rule(env):
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_BLOCK, R_UNUSED])
    assert got["code"] == "BEREINIGUNG_BESTAETIGUNG_FEHLT" and got["params"]["uuids"] == R_BLOCK
    ok = await preview(env.host_id, [R_BLOCK, R_UNUSED], acknowledge=[R_BLOCK], ack_text_version="1")
    item = next(i for i in ok["plan"]["nodes"][0]["items"] if i["uuid"] == R_BLOCK)
    assert item["level"] == "probable" and item["needs_ack"] and "UNUSED_BLOCK_RULE" in item["notes"]
    op = await _op(ok["preview_id"])
    assert op.options["acknowledged"] == [{"uuid": R_BLOCK, "text_version": "1"}]


@pytest.mark.asyncio
async def test_short_window_and_external_change_cap_at_probable(env):
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED], unused_days=14)
    assert got["code"] == "BEREINIGUNG_BESTAETIGUNG_FEHLT"
    async with SessionLocal() as db:
        db.add(RuleChangeEvent(host_id=env.host_id, rule_uuid=R_UNUSED, seq=1, observed_at=_now() - timedelta(days=2),
                               kind="changed", before={}, after={}, origin="external"))
        await db.commit()
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_BESTAETIGUNG_FEHLT"
    ok = await preview(env.host_id, [R_UNUSED], acknowledge=[R_UNUSED])
    assert "EXTERNAL_CHANGE" in ok["plan"]["nodes"][0]["items"][0]["notes"]


@pytest.mark.asyncio
async def test_rule_limit_and_selection_validation(env):
    await _settings(max_rules=2)
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED, R_DUP, R_TAIL])
    assert got["status"] == 422 and got["code"] == "BEREINIGUNG_ZU_VIELE_REGELN" and got["params"]["max"] == 2
    for uuids in ([], ["../apply"]):
        got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=uuids)
        assert got["code"] == "BEREINIGUNG_AUSWAHL_UNGUELTIG"
    got = await refused(env.host_id, svc.preview, actor="admin", kind="wipe", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_AUSWAHL_UNGUELTIG"


@pytest.mark.asyncio
async def test_maintenance_open_deployments_and_open_cleanups_block(env):
    from app.models.rule import FirewallRule
    from app.models.rule_deployment import RuleDeployment
    async with SessionLocal() as db:
        op = HostOperation(host_id=env.host_id, host_name="fw-a", actor="admin", action="reboot", status="submitted",
                           expires_at=_now(), fingerprint="x", snapshot={})
        db.add(op)
        await db.commit()
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["code"] == "OPEN_HOST_MAINTENANCE_BLOCKS_DEPLOY"
    async with SessionLocal() as db:
        await db.execute(update(HostOperation).values(status="done"))
        managed = FirewallRule(description="x", interface="lan", action="pass", direction="in", protocol="tcp",
                               source_net="any", destination_net="any")
        db.add(managed)
        await db.flush()
        dep = RuleDeployment(rule_id=managed.id, host_id=env.host_id, status="pending", opnsense_rule_uuid=R_TAIL)
        db.add(dep)
        await db.commit()
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_OFFENE_OPERATION" and got["params"]["reason"] == "deployment"
    async with SessionLocal() as db:
        await db.execute(update(RuleDeployment).values(status="success"))
        await db.commit()
    first = await preview(env.host_id, [R_UNUSED])
    await deploy(env.host_id, first["preview_id"])
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_DUP])
    assert got["code"] == "BEREINIGUNG_OFFENE_OPERATION" and got["params"]["reason"] == "cleanup"
    async with SessionLocal() as db:
        assert await svc.has_open_cleanup(db, [env.host_id])


# ── Deploy ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deploy_switches_off_samples_before_apply_and_waits_for_confirmation(env):
    p = await preview(env.host_id, [R_UNUSED, R_DUP])
    env.fw.calls.clear()
    result = await deploy(env.host_id, p["preview_id"])
    assert result["status"] == "awaiting_confirm" and result["can_confirm"]
    writes = env.fw.writes()
    assert sorted(writes[:2]) == sorted([("POST", f"/firewall/filter/toggleRule/{R_DUP}/0"),
                                         ("POST", f"/firewall/filter/toggleRule/{R_UNUSED}/0")])
    assert writes[2:] == [("POST", "/firewall/filter/apply")]  # one apply per node, after the toggles
    apply_at = env.fw.calls.index(("POST", "/firewall/filter/apply"))
    last_toggle = max(i for i, c in enumerate(env.fw.calls) if "toggleRule" in c[1])
    assert ("GET", "/firewall/filter_util/ruleStats") in env.fw.calls[last_toggle:apply_at]  # sampled before the apply
    assert env.fw.get(R_UNUSED)["enabled"] == "0" and env.fw.get(R_DUP)["enabled"] == "0"
    op = await _op(p["preview_id"])
    window = timedelta(seconds=settings.deploy_confirm_window_seconds)
    assert window - timedelta(seconds=10) < op.revert_deadline - _now() <= window
    items = await _items(p["preview_id"])
    assert {i.state for i in items.values()} == {"disabled"}
    assert items[R_UNUSED].fp_before and items[R_UNUSED].row_before["enabled"] == "1"
    async with SessionLocal() as db:
        audits = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "rule_cleanup", AuditEvent.action == "disable"))).all()
    assert {(a.details["remote_uuid"], a.status) for a in audits} == {(R_UNUSED, "accepted"), (R_DUP, "accepted")}
    assert all(a.host_id == str(env.host_id) and a.details["preview_fingerprint"] == op.preview_fingerprint for a in audits)


@pytest.mark.asyncio
async def test_rule_history_shows_the_cleanup_as_opnexus(env):
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, env.host_id)
        await rhs.observe_rows(db, host, {"rows": env.fw.rules})  # baseline
    p = await preview(env.host_id, [R_UNUSED])
    await deploy(env.host_id, p["preview_id"])
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, env.host_id)
        await rhs.observe_rows(db, host, {"rows": env.fw.rules})
        event = (await db.scalars(select(RuleChangeEvent).where(RuleChangeEvent.rule_uuid == R_UNUSED))).one()
    assert event.kind == "disabled" and event.origin == "opnexus" and event.audit_id is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edited", "moved", "cover_edited", "hit"])
async def test_deploy_refuses_a_stale_preview_without_writing(env, change):
    p = await preview(env.host_id, [R_UNUSED, R_DUP])
    if change == "edited":
        env.fw.get(R_UNUSED)["destination_port"] = "8082"
    elif change == "moved":
        env.fw.get(R_UNUSED)["sort_order"] = "400000.0000301"
    elif change == "cover_edited":
        env.fw.get(R_BASE)["description"] = "base changed"
    else:  # a hit since the preview: the fresh sample at deploy sees the counter grow
        async with SessionLocal() as db:
            await db.execute(update(RuleHit).where(RuleHit.rule_uuid == R_UNUSED).values(last_hit_at=_now()))
            await db.commit()
    env.fw.calls.clear()
    got = await refused(env.host_id, svc.deploy, uuid_lib.UUID(p["preview_id"]), actor="admin", confirm_name="fw-a")
    expected = R_DUP if change == "cover_edited" else R_UNUSED
    if change == "hit":
        assert got["code"] == "BEREINIGUNG_REGEL_NICHT_AENDERBAR" and got["params"]["uuid"] == R_UNUSED
    else:
        assert got["code"] == "BEREINIGUNG_VORSCHAU_VERALTET" and got["params"]["uuids"] == expected
    assert env.fw.writes() == []
    assert (await _op(p["preview_id"])).status == "expired"


@pytest.mark.asyncio
async def test_preview_token_is_single_use_bound_to_actor_and_name(env):
    p = await preview(env.host_id, [R_UNUSED])
    pid = uuid_lib.UUID(p["preview_id"])
    got = await refused(env.host_id, svc.deploy, pid, actor="someone-else", confirm_name="fw-a")
    assert got["status"] == 404
    got = await refused(env.host_id, svc.deploy, pid, actor="admin", confirm_name="FW-A")
    assert got["code"] == "HOSTNAMEN_EXAKT_EINGEBEN"
    await deploy(env.host_id, p["preview_id"])
    got = await refused(env.host_id, svc.deploy, pid, actor="admin", confirm_name="fw-a")
    assert got["code"] == "VORSCHAU_ABGELAUFEN_ODER_BEREITS_VERWENDET"
    # an expired preview
    await call(env.host_id, svc.revert_now, pid)
    q = await preview(env.host_id, [R_DUP])
    await _backdate(q["preview_id"], expires_at=_now() - timedelta(seconds=1))
    got = await refused(env.host_id, svc.deploy, uuid_lib.UUID(q["preview_id"]), actor="admin", confirm_name="fw-a")
    assert got["code"] == "VORSCHAU_ABGELAUFEN_ODER_BEREITS_VERWENDET"


@pytest.mark.asyncio
async def test_refused_toggle_stops_writing_but_applies_what_is_switched(env):
    uuids = [R_DUP, R_UNUSED, R_BLOCK, R_TAIL]
    p = await preview(env.host_id, uuids, acknowledge=[R_BLOCK])
    env.fw.fail[("toggle", R_BLOCK)] = httpx.Response(200, json={"result": "failed"})
    env.fw.calls.clear()
    result = await deploy(env.host_id, p["preview_id"])
    states = {i.rule_uuid: i.state for i in (await _items(p["preview_id"])).values()}
    assert states == {R_DUP: "disabled", R_UNUSED: "disabled", R_BLOCK: "failed", R_TAIL: "queued"}
    assert ("POST", f"/firewall/filter/toggleRule/{R_TAIL}/0") not in env.fw.calls  # nothing after the error
    assert env.fw.writes()[-1] == ("POST", "/firewall/filter/apply")  # but the switched ones are applied
    assert result["status"] == "partial" and not result["can_confirm"] and result["can_revert"]
    assert (await _op(p["preview_id"])).revert_deadline > _now()
    got = await refused(env.host_id, svc.confirm, uuid_lib.UUID(p["preview_id"]))
    assert got["code"] == "BEREINIGUNG_NICHT_BESTAETIGBAR"


@pytest.mark.asyncio
@pytest.mark.parametrize("applied,readable,state", [(True, True, "disabled"), (False, True, "failed"), (False, False, "unknown")])
async def test_unclear_toggle_is_settled_by_reading(env, applied, readable, state):
    p = await preview(env.host_id, [R_UNUSED, R_DUP])
    items_order = [i["uuid"] for i in p["plan"]["nodes"][0]["items"]]
    first = items_order[0]
    env.fw.fail[("toggle", first)] = httpx.ReadTimeout
    env.fw.fail[("toggle_applies", first)] = applied
    if not readable:
        env.fw.fail[("get", first)] = httpx.ConnectError
    result = await deploy(env.host_id, p["preview_id"])
    items = await _items(p["preview_id"])
    assert items[first].state == state
    # no further write after an unclear answer (never attempted: "skipped" once nothing changed at all)
    assert items[items_order[1]].state == ("skipped" if state == "failed" else "queued")
    expect_apply = state in ("disabled", "unknown")
    assert (("POST", "/firewall/filter/apply") in env.fw.calls) is expect_apply
    assert result["status"] == {"disabled": "partial", "failed": "failed", "unknown": "partial"}[state]
    assert not result["can_confirm"]


@pytest.mark.asyncio
async def test_failed_apply_keeps_the_deadline_and_the_sweeper_reverts(env):
    p = await preview(env.host_id, [R_UNUSED])
    env.fw.fail[("apply", "")] = [httpx.Response(500, json={}), None]
    result = await deploy(env.host_id, p["preview_id"])
    assert result["status"] == "apply_failed" and result["revert_deadline"]
    await _sweep()
    assert (await _op(p["preview_id"])).status == "apply_failed"  # deadline not reached yet
    await _backdate(p["preview_id"], revert_deadline=_now() - timedelta(seconds=1))
    env.fw.calls.clear()
    await _sweep()
    assert (await _op(p["preview_id"])).status == "reverted"
    assert env.fw.writes() == [("POST", f"/firewall/filter/toggleRule/{R_UNUSED}/1"), ("POST", "/firewall/filter/apply")]
    assert env.fw.get(R_UNUSED)["enabled"] == "1"


# ── Confirm ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_reads_the_firewall_and_refuses_when_unreachable_or_different(env):
    p = await preview(env.host_id, [R_UNUSED, R_DUP])
    await deploy(env.host_id, p["preview_id"])
    pid = uuid_lib.UUID(p["preview_id"])
    env.fw.down = True
    got = await refused(env.host_id, svc.confirm, pid)
    assert got["status"] == 502 and got["code"] == "BEREINIGUNG_FIREWALL_NICHT_ERREICHBAR"
    assert (await _op(pid)).status == "awaiting_confirm"  # stays open: the sweeper reverts it
    env.fw.down = False
    env.fw.get(R_DUP)["enabled"] = "1"  # someone switched it back on in the GUI
    got = await refused(env.host_id, svc.confirm, pid)
    assert got["code"] == "BEREINIGUNG_LIVESTAND_ABWEICHEND" and got["params"]["uuids"] == R_DUP
    env.fw.get(R_DUP)["enabled"] = "0"
    env.fw.calls.clear()
    result = await call(env.host_id, svc.confirm, pid)
    assert result["status"] == "confirmed" and {i["state"] for i in result["items"]} == {"confirmed"}
    assert env.fw.writes() == []  # confirming only reads
    op = await _op(pid)
    assert op.revert_deadline is None and op.confirmed_at is not None
    assert await _sweep() == []


@pytest.mark.asyncio
async def test_confirm_after_the_deadline_is_refused_and_the_sweeper_reverts(env):
    p = await preview(env.host_id, [R_UNUSED])
    await deploy(env.host_id, p["preview_id"])
    await _backdate(p["preview_id"], revert_deadline=_now() - timedelta(seconds=1))
    got = await refused(env.host_id, svc.confirm, uuid_lib.UUID(p["preview_id"]))
    assert got["code"] == "BEREINIGUNG_FRIST_ABGELAUFEN"
    assert len(await _sweep()) == 1
    assert env.fw.get(R_UNUSED)["enabled"] == "1"
    assert {i.state for i in (await _items(p["preview_id"])).values()} == {"reverted"}
    async with SessionLocal() as db:
        audit = (await db.scalars(select(AuditEvent).where(AuditEvent.action == "revert"))).one()
    assert audit.actor == "system:rollback" and audit.details["remote_uuid"] == R_UNUSED


@pytest.mark.asyncio
async def test_revert_now_and_failed_revert_is_not_retried_automatically(env):
    p = await preview(env.host_id, [R_UNUSED, R_DUP])
    await deploy(env.host_id, p["preview_id"])
    env.fw.fail[("toggle", R_DUP)] = httpx.Response(200, json={"result": "failed"})
    result = await call(env.host_id, svc.revert_now, uuid_lib.UUID(p["preview_id"]))
    assert result["status"] == "revert_failed" and result["can_retry"]
    states = {i["rule_uuid"]: i["state"] for i in result["items"]}
    assert states == {R_UNUSED: "reverted", R_DUP: "revert_failed"}  # the others are still reverted
    assert env.fw.get(R_UNUSED)["enabled"] == "1" and env.fw.writes()[-1] == ("POST", "/firewall/filter/apply")
    env.fw.calls.clear()
    await _backdate(p["preview_id"], revert_deadline=_now() - timedelta(minutes=5))
    await _sweep()
    assert env.fw.writes() == []  # never retried by the sweeper
    del env.fw.fail[("toggle", R_DUP)]
    result = await call(env.host_id, svc.retry, uuid_lib.UUID(p["preview_id"]))
    assert result["status"] == "reverted" and env.fw.get(R_DUP)["enabled"] == "1"


# ── Sweeper and two backends ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sweeper_skips_a_live_run_and_takes_over_an_abandoned_one(env):
    p = await preview(env.host_id, [R_UNUSED])
    await deploy(env.host_id, p["preview_id"])
    # simulate a backend that died mid-run: running, item half-way
    async with SessionLocal() as db:
        await db.execute(update(RuleCleanupOperation).values(status="running", heartbeat_at=_now(),
                                                             revert_deadline=_now() + timedelta(minutes=10)))
        await db.execute(update(RuleCleanupItem).values(state="disabling"))
        await db.commit()
    env.fw.calls.clear()
    assert await _sweep() == []          # (b) fresh heartbeat: someone is still working on it
    assert env.fw.writes() == []
    await _backdate(p["preview_id"], heartbeat_at=_now() - timedelta(hours=1))
    assert await _sweep() == [p["preview_id"]]  # (c) stale heartbeat: taken over and reverted
    assert env.fw.writes() == [("POST", f"/firewall/filter/toggleRule/{R_UNUSED}/1"), ("POST", "/firewall/filter/apply")]
    assert (await _op(p["preview_id"])).status == "reverted"


@pytest.mark.asyncio
async def test_two_deploys_of_one_preview_write_once(env):
    p = await preview(env.host_id, [R_UNUSED, R_DUP])
    outcomes = await asyncio.gather(deploy(env.host_id, p["preview_id"]), deploy(env.host_id, p["preview_id"]),
                                    return_exceptions=True)
    errors = [o for o in outcomes if isinstance(o, HTTPException)]
    assert len(errors) == 1 and errors[0].detail["code"] == "VORSCHAU_ABGELAUFEN_ODER_BEREITS_VERWENDET"
    assert sum(1 for c in env.fw.calls if "toggleRule" in c[1]) == 2  # (a) each rule exactly once


@pytest.mark.asyncio
async def test_second_operation_on_the_same_rule_is_refused(env):
    a = await preview(env.host_id, [R_UNUSED], actor="admin")
    b = await preview(env.host_id, [R_UNUSED], actor="admin2")
    await deploy(env.host_id, a["preview_id"])
    got = await refused(env.host_id, svc.deploy, uuid_lib.UUID(b["preview_id"]), actor="admin2", confirm_name="fw-a")
    assert got["code"] == "BEREINIGUNG_OFFENE_OPERATION"  # (d)
    # and the partial unique index holds even without the service's checks
    async with SessionLocal() as db:
        op = await db.get(RuleCleanupOperation, uuid_lib.UUID(b["preview_id"]))
        db.add(RuleCleanupItem(operation_id=op.id, host_id=env.host_id, rule_uuid=R_UNUSED, state="queued", description=""))
        with pytest.raises(Exception):
            await db.commit()


@pytest.mark.asyncio
async def test_confirm_and_sweeper_at_the_deadline_decide_once(env):
    p = await preview(env.host_id, [R_UNUSED])
    await deploy(env.host_id, p["preview_id"])
    await _backdate(p["preview_id"], revert_deadline=_now() + timedelta(milliseconds=300))

    async def late_confirm():
        await asyncio.sleep(0.4)
        return await call(env.host_id, svc.confirm, uuid_lib.UUID(p["preview_id"]))

    async def late_sweep():
        await asyncio.sleep(0.4)
        return await _sweep()

    outcomes = await asyncio.gather(late_confirm(), late_sweep(), return_exceptions=True)
    op = await _op(p["preview_id"])
    assert op.status == "reverted"  # (e) the deadline passed: confirm refused, exactly one revert
    assert isinstance(outcomes[0], HTTPException) and outcomes[0].detail["code"] == "BEREINIGUNG_FRIST_ABGELAUFEN"
    assert sum(1 for c in env.fw.calls if c[1].endswith(f"{R_UNUSED}/1")) == 1


# ── Cluster ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cluster_writes_backup_first_and_reverts_master_first(cluster):
    p = await preview(cluster.a, [R_UNUSED])
    assert [n["role"] for n in p["plan"]["nodes"]] == ["BACKUP", "MASTER"]
    await deploy(cluster.a, p["preview_id"])
    writes = _writes(cluster.log)
    assert writes == [(FW2, f"/firewall/filter/toggleRule/{R_UNUSED}/0"), (FW2, "/firewall/filter/apply"),
                      (FW1, f"/firewall/filter/toggleRule/{R_UNUSED}/0"), (FW1, "/firewall/filter/apply")]
    items = await _items(p["preview_id"])
    assert len(items) == 1  # (same rule_uuid on both nodes: keyed by uuid here)
    async with SessionLocal() as db:
        roles = {i.host_id: i.carp_role for i in (await db.scalars(select(RuleCleanupItem))).all()}
    assert roles == {cluster.a: "MASTER", cluster.b: "BACKUP"}
    cluster.log.clear()
    await _backdate(p["preview_id"], revert_deadline=_now() - timedelta(seconds=1))
    await _sweep()
    assert _writes(cluster.log) == [(FW1, f"/firewall/filter/toggleRule/{R_UNUSED}/1"), (FW1, "/firewall/filter/apply"),
                                    (FW2, f"/firewall/filter/toggleRule/{R_UNUSED}/1"), (FW2, "/firewall/filter/apply")]


@pytest.mark.asyncio
async def test_cluster_master_failure_leaves_backup_switched_and_unconfirmable(cluster):
    p = await preview(cluster.a, [R_UNUSED])
    cluster.master.fail[("toggle", R_UNUSED)] = httpx.Response(200, json={"result": "failed"})
    result = await deploy(cluster.a, p["preview_id"])
    assert result["status"] == "partial" and not result["can_confirm"]
    assert cluster.backup.get(R_UNUSED)["enabled"] == "0" and cluster.master.get(R_UNUSED)["enabled"] == "1"


@pytest.mark.asyncio
async def test_cluster_refuses_missing_uuid_offline_peer_and_two_masters(cluster):
    cluster.backup.rules = [r for r in cluster.backup.rules if r["uuid"] != R_UNUSED]
    got = await refused(cluster.a, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_CLUSTER_UNVOLLSTAENDIG" and got["params"]["reason"] == "uuid_missing"
    cluster.backup.rules = default_rules()
    cluster.backup.role = "MASTER"
    got = await refused(cluster.a, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_CLUSTER_UNVOLLSTAENDIG" and got["params"]["reason"] == "roles"
    cluster.backup.role = "BACKUP"
    cluster.backup.down = True
    got = await refused(cluster.a, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["status"] == 502 and got["params"]["host"] == "fw-b"


@pytest.mark.asyncio
async def test_cluster_usage_is_judged_over_all_nodes(cluster):
    # case 16: the backup saw no hit, the master did: not unused
    async with SessionLocal() as db:
        await db.execute(update(RuleHit).where(RuleHit.host_id == cluster.a, RuleHit.rule_uuid == R_UNUSED)
                         .values(last_hit_at=_now() - timedelta(days=1)))
        await db.commit()
    got = await refused(cluster.b, svc.preview, actor="admin", kind="disable", uuids=[R_UNUSED])
    assert got["params"]["reason"] == "not_candidate"
    # no usage data on one node: at best probable
    async with SessionLocal() as db:
        await db.execute(RuleHit.__table__.delete().where(RuleHit.host_id == cluster.a, RuleHit.rule_uuid == R_TAIL))
        await db.commit()
    got = await refused(cluster.b, svc.preview, actor="admin", kind="disable", uuids=[R_TAIL])
    assert got["code"] == "BEREINIGUNG_BESTAETIGUNG_FEHLT"


@pytest.mark.asyncio
async def test_cluster_pfsync_interface_rules_are_infrastructure(cluster):
    # "any" from the LAN net on the pfsync interface (LAN) would also carry pfsync
    for fw in (cluster.master, cluster.backup):
        fw.rules.append(rule("aaaaaaaa-0000-4000-8000-000000000900", 900, proto="any", src="10.20.30.0/24", description="lan any"))
    await _seed_hits(cluster.a, ["aaaaaaaa-0000-4000-8000-000000000900"])
    await _seed_hits(cluster.b, ["aaaaaaaa-0000-4000-8000-000000000900"])
    got = await refused(cluster.a, svc.preview, actor="admin", kind="disable", uuids=["aaaaaaaa-0000-4000-8000-000000000900"])
    assert got["params"]["reason"] == "infrastructure"


# ── Delete and restore ────────────────────────────────────────────────────

async def _confirmed_disable(host_id, uuids, *, days_ago=8):
    p = await preview(host_id, uuids)
    await deploy(host_id, p["preview_id"])
    await call(host_id, svc.confirm, uuid_lib.UUID(p["preview_id"]))
    await _backdate(p["preview_id"], confirmed_at=_now() - timedelta(days=days_ago))
    return p["preview_id"]


@pytest.mark.asyncio
async def test_delete_needs_the_waiting_time_an_unchanged_rule_and_no_external_change(env):
    await _confirmed_disable(env.host_id, [R_UNUSED], days_ago=2)
    got = await refused(env.host_id, svc.preview, actor="admin", kind="delete", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_WARTEZEIT_NICHT_ERREICHT" and got["params"]["days"] == 5
    async with SessionLocal() as db:
        await db.execute(update(RuleCleanupOperation).values(confirmed_at=_now() - timedelta(days=8)))
        await db.commit()
    ok = await preview(env.host_id, [R_UNUSED], kind="delete")
    assert ok["plan"]["nodes"][0]["items"][0]["basis"]["type"] == "confirmed"
    env.fw.get(R_UNUSED)["destination_port"] = "8099"
    got = await refused(env.host_id, svc.preview, actor="admin", kind="delete", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_VORSCHAU_VERALTET"
    env.fw.get(R_UNUSED)["destination_port"] = "8081"
    async with SessionLocal() as db:
        db.add(RuleChangeEvent(host_id=env.host_id, rule_uuid=R_UNUSED, seq=5, observed_at=_now() - timedelta(days=1),
                               kind="changed", before={}, after={}, origin="external"))
        await db.commit()
    got = await refused(env.host_id, svc.preview, actor="admin", kind="delete", uuids=[R_UNUSED])
    assert got["code"] == "BEREINIGUNG_VORSCHAU_VERALTET"
    env.fw.get(R_DUP)["enabled"] = "1"
    got = await refused(env.host_id, svc.preview, actor="admin", kind="delete", uuids=[R_DUP])
    assert got["params"]["reason"] == "not_candidate"  # an enabled rule is never deleted


@pytest.mark.asyncio
async def test_delete_commits_the_snapshot_before_delrule(env, monkeypatch):
    from app.services import opnsense_client as client_mod
    await _confirmed_disable(env.host_id, [R_UNUSED])
    p = await preview(env.host_id, [R_UNUSED], kind="delete")
    seen: list = []
    real_del = client_mod.del_rule

    async def spy(host, uuid):  # what another session sees in the database at the moment delRule goes out
        async with SessionLocal() as db:
            item = (await db.scalars(select(RuleCleanupItem).where(
                RuleCleanupItem.operation_id == uuid_lib.UUID(p["preview_id"])))).one()
            seen.append((item.state, bool(item.snapshot), item.anchor_next_uuid))
        return await real_del(host, uuid)
    monkeypatch.setattr(client_mod, "del_rule", spy)
    result = await deploy(env.host_id, p["preview_id"])
    assert seen == [("deleting", True, R_BLOCK)]  # durable before the remote delete
    assert result["status"] == "done"
    item = next(iter((await _items(p["preview_id"])).values()))
    assert item.state == "deleted" and item.snapshot["rule"]["destination_port"] == "8081"
    assert item.snapshot["rule"]["action"] == "pass" and "uuid" not in item.snapshot["rule"]
    assert item.prio_group == "400000" and item.anchor_prev_uuid == R_DUP
    assert env.fw.get(R_UNUSED) is None and env.fw.writes()[-1] == ("POST", "/firewall/filter/apply")


@pytest.mark.asyncio
@pytest.mark.parametrize("applied,state", [(True, "deleted"), (False, "failed")])
async def test_unclear_delete_is_settled_with_getrule(env, applied, state):
    await _confirmed_disable(env.host_id, [R_UNUSED])
    p = await preview(env.host_id, [R_UNUSED], kind="delete")
    env.fw.fail[("delete", R_UNUSED)] = httpx.ReadTimeout
    env.fw.fail[("delete_applies", R_UNUSED)] = applied
    result = await deploy(env.host_id, p["preview_id"])
    assert next(iter((await _items(p["preview_id"])).values())).state == state
    assert result["status"] == ("done" if state == "deleted" else "failed")


@pytest.mark.asyncio
async def test_restore_adds_disabled_at_the_anchor_with_a_new_uuid(env):
    await _confirmed_disable(env.host_id, [R_UNUSED])
    p = await preview(env.host_id, [R_UNUSED], kind="delete")
    await deploy(env.host_id, p["preview_id"])
    deleted = next(iter((await _items(p["preview_id"])).values()))
    env.fw.calls.clear()
    result = await call(env.host_id, svc.restore_item, deleted.id, actor="admin", confirm_name="fw-a")
    assert result["status"] == "done" and result["warnings"] == []
    item = result["items"][0]
    new = item["restored_uuid"]
    assert new and new != R_UNUSED and item["state"] == "restored"
    restored = env.fw.get(new)
    assert restored["enabled"] == "0" and restored["destination_port"] == "8081"
    assert ("POST", f"/firewall/filter/moveRuleBefore/{new}/{R_BLOCK}") in env.fw.calls
    order = [r["uuid"] for r in sorted(env.fw.rules, key=lambda r: r["sort_order"]) if not r.get("is_automatic")]
    assert order.index(new) == order.index(R_BLOCK) - 1 and order.index(new) == order.index(R_DUP) + 1
    async with SessionLocal() as db:
        audit = (await db.scalars(select(AuditEvent).where(AuditEvent.action == "restore"))).one()
        source = await db.get(RuleCleanupItem, deleted.id)
    assert audit.details["remote_uuid"] == new and audit.details["restored_from"] == R_UNUSED
    assert source.restored_uuid == new
    got = await refused(env.host_id, svc.restore_item, deleted.id, actor="admin", confirm_name="fw-a")
    assert got["code"] == "BEREINIGUNG_AKTION_NICHT_MOEGLICH"  # restored once only
    # re-enabling the restored rule is an operation with a window
    e = await preview(env.host_id, [new], kind="enable")
    env.fw.calls.clear()
    r = await deploy(env.host_id, e["preview_id"])
    assert r["status"] == "awaiting_confirm" and ("POST", f"/firewall/filter/toggleRule/{new}/1") in env.fw.calls


@pytest.mark.asyncio
async def test_restore_without_anchor_warns_and_keeps_the_rule(env):
    await _confirmed_disable(env.host_id, [R_UNUSED])
    p = await preview(env.host_id, [R_UNUSED], kind="delete")
    await deploy(env.host_id, p["preview_id"])
    deleted = next(iter((await _items(p["preview_id"])).values()))
    env.fw.rules = [r for r in env.fw.rules if r["uuid"] != R_BLOCK]  # the anchor is gone meanwhile
    result = await call(env.host_id, svc.restore_item, deleted.id, actor="admin", confirm_name="fw-a")
    assert result["status"] == "done" and result["warnings"] == ["BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR"]
    assert result["items"][0]["message"] == "BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR"
    assert env.fw.get(result["items"][0]["restored_uuid"])["enabled"] == "0"


@pytest.mark.asyncio
async def test_restore_refused_by_the_firewall_is_failed_with_its_message(env):
    await _confirmed_disable(env.host_id, [R_UNUSED])
    p = await preview(env.host_id, [R_UNUSED], kind="delete")
    await deploy(env.host_id, p["preview_id"])
    deleted = next(iter((await _items(p["preview_id"])).values()))
    env.fw.fail[("add", "")] = httpx.Response(200, json={"result": "failed", "validations": {"rule.source_net": "alias missing"}})
    result = await call(env.host_id, svc.restore_item, deleted.id, actor="admin", confirm_name="fw-a")
    assert result["status"] == "failed" and "alias missing" in result["items"][0]["message"]
    async with SessionLocal() as db:
        assert (await db.get(RuleCleanupItem, deleted.id)).restored_uuid is None  # can be tried again


@pytest.mark.asyncio
async def test_cluster_delete_restore_and_reenable_follow_the_restore_link(cluster):
    p = await preview(cluster.a, [R_UNUSED])
    await deploy(cluster.a, p["preview_id"])
    await call(cluster.a, svc.confirm, uuid_lib.UUID(p["preview_id"]))
    await _backdate(p["preview_id"], confirmed_at=_now() - timedelta(days=8))
    d = await preview(cluster.a, [R_UNUSED], kind="delete")
    cluster.log.clear()
    assert (await deploy(cluster.a, d["preview_id"]))["status"] == "done"
    assert [w for w in _writes(cluster.log) if "delRule" in w[1]] == [(FW2, f"/firewall/filter/delRule/{R_UNUSED}"),
                                                                     (FW1, f"/firewall/filter/delRule/{R_UNUSED}")]
    async with SessionLocal() as db:
        deleted = (await db.scalars(select(RuleCleanupItem).where(
            RuleCleanupItem.operation_id == uuid_lib.UUID(d["preview_id"]), RuleCleanupItem.host_id == cluster.a))).one()
    restored = await call(cluster.a, svc.restore_item, deleted.id, actor="admin", confirm_name="fw-a")
    assert restored["status"] == "done" and len(restored["items"]) == 2  # both nodes, backup first
    new = {i["host_id"]: i["restored_uuid"] for i in restored["items"]}
    assert new[str(cluster.a)] != new[str(cluster.b)]  # addRule: a new UUID on every node
    assert cluster.backup.get(new[str(cluster.b)])["enabled"] == "0" and cluster.master.get(new[str(cluster.a)])["enabled"] == "0"
    # re-enable through the master's UUID: the backup's copy is found through the restore operation
    e = await preview(cluster.a, [new[str(cluster.a)]], kind="enable")
    assert {n["host_name"]: n["items"][0]["uuid"] for n in e["plan"]["nodes"]} == {"fw-a": new[str(cluster.a)], "fw-b": new[str(cluster.b)]}
    cluster.log.clear()
    r = await deploy(cluster.a, e["preview_id"])
    assert r["status"] == "awaiting_confirm"
    assert [w for w in _writes(cluster.log) if "toggleRule" in w[1]] == [
        (FW2, f"/firewall/filter/toggleRule/{new[str(cluster.b)]}/1"), (FW1, f"/firewall/filter/toggleRule/{new[str(cluster.a)]}/1")]


@pytest.mark.asyncio
async def test_restore_with_a_refused_move_is_verified_by_reading_and_only_warns(env):
    await _confirmed_disable(env.host_id, [R_UNUSED])
    p = await preview(env.host_id, [R_UNUSED], kind="delete")
    await deploy(env.host_id, p["preview_id"])
    deleted = next(iter((await _items(p["preview_id"])).values()))
    # moveRuleBefore answers HTTP 500 (an Ambiguous error for the client, P3): the position is read back
    env.fw.fail[("move", "*")] = httpx.Response(500, json={"errorMessage": "Unexpected error, check log for details"})
    result = await call(env.host_id, svc.restore_item, deleted.id, actor="admin", confirm_name="fw-a")
    assert result["status"] == "done" and result["warnings"] == ["BEREINIGUNG_POSITION_NICHT_WIEDERHERSTELLBAR"]


@pytest.mark.asyncio
async def test_deadline_passing_while_confirm_reads_the_firewall_refuses(env):
    import time
    p = await preview(env.host_id, [R_UNUSED])
    await deploy(env.host_id, p["preview_id"])
    await _backdate(p["preview_id"], revert_deadline=_now() + timedelta(milliseconds=300))
    env.fw.fail[("search", "")] = lambda: time.sleep(0.6)  # a slow firewall: the window closes during the read
    got = await refused(env.host_id, svc.confirm, uuid_lib.UUID(p["preview_id"]))
    assert got["code"] == "BEREINIGUNG_FRIST_ABGELAUFEN"
    assert (await _op(p["preview_id"])).status == "awaiting_confirm"  # left to the sweeper


@pytest.mark.asyncio
async def test_sweeper_leaves_a_live_run_alone_even_past_its_deadline(env, monkeypatch):  # review finding 2
    from app.services import opnsense_client as client_mod
    p = await preview(env.host_id, [R_UNUSED, R_DUP])
    real = client_mod.toggle_rule
    swept: list = []

    async def toggle(host, uuid, enabled):  # a second backend's sweeper starts while the first toggle is on the wire
        if not swept:
            await _backdate(p["preview_id"], revert_deadline=_now() - timedelta(seconds=1))
            swept.append(asyncio.create_task(_sweep()))  # it gets the lock in the deploy's next checkpoint gap
            await asyncio.sleep(0.05)
        return await real(host, uuid, enabled)
    monkeypatch.setattr(client_mod, "toggle_rule", toggle)
    result = await deploy(env.host_id, p["preview_id"])
    assert await swept[0] == []  # fresh heartbeat: not taken over although the deadline passed
    assert result["status"] == "awaiting_confirm"
    assert {i.state for i in (await _items(p["preview_id"])).values()} == {"disabled"}


@pytest.mark.asyncio
async def test_a_run_taken_over_by_another_backend_writes_nothing_more(env, monkeypatch):  # review finding 2
    from app.services import opnsense_client as client_mod
    p = await preview(env.host_id, [R_UNUSED, R_DUP])
    real = client_mod.toggle_rule
    calls: list = []

    async def toggle(host, uuid, enabled):
        calls.append(uuid)
        if len(calls) == 1:  # another backend took the operation over while this toggle was on the wire
            await _backdate(p["preview_id"], status="reverting")
        return await real(host, uuid, enabled)
    monkeypatch.setattr(client_mod, "toggle_rule", toggle)
    await deploy(env.host_id, p["preview_id"])
    assert len(calls) == 1  # the conditional heartbeat before the second toggle found the operation gone
    assert (await _op(p["preview_id"])).status == "reverting"  # left to the new owner, not overwritten
    states = sorted(i.state for i in (await _items(p["preview_id"])).values())
    assert states == ["disabled", "queued"]


@pytest.mark.asyncio
async def test_extra_management_ports_protect_rules_on_them(env):  # review finding 3
    custom = "aaaaaaaa-0000-4000-8000-000000000910"
    env.fw.rules.append(rule(custom, 910, interface="wan", src="203.0.113.0/24", dport="8443", description="gui 8443"))
    await _seed_hits(env.host_id, [custom])
    ok = await preview(env.host_id, [custom])  # 8443 is not one of the fixed ports: not about management
    assert ok["plan"]["ports"] == []
    await _settings(ports=(8443,))
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[custom])
    assert got["params"]["reason"] == "management"


@pytest.mark.asyncio
async def test_cluster_peer_may_confirm_and_read_but_strangers_may_not(cluster, client, respx_mock):  # review finding 4
    p = await preview(cluster.a, [R_UNUSED])
    await deploy(cluster.a, p["preview_id"])
    pid = uuid_lib.UUID(p["preview_id"])
    assert (await call(cluster.b, svc.get_operation, pid))["id"] == p["preview_id"]
    FakeFirewall("https://203.0.113.59").mount(respx_mock)
    other = uuid_lib.UUID((await _create_host(client, "fw-c", "https://203.0.113.59"))["id"])
    for fn in (svc.get_operation, svc.confirm, svc.revert_now, svc.retry):
        assert (await refused(other, fn, pid))["status"] == 404
    got = await refused(cluster.b, svc.cancel, pid, actor="admin")  # previews/deploys stay bound to the anchor
    assert got["status"] == 404
    result = await call(cluster.b, svc.confirm, pid)
    assert result["status"] == "confirmed"


async def _half_deleted_cluster(cluster):
    p = await preview(cluster.a, [R_UNUSED])
    await deploy(cluster.a, p["preview_id"])
    await call(cluster.a, svc.confirm, uuid_lib.UUID(p["preview_id"]))
    await _backdate(p["preview_id"], confirmed_at=_now() - timedelta(days=8))
    d = await preview(cluster.a, [R_UNUSED], kind="delete")
    cluster.master.fail[("delete", R_UNUSED)] = httpx.Response(200, json={"result": "failed"})
    result = await deploy(cluster.a, d["preview_id"])
    assert result["status"] == "failed"
    assert cluster.backup.get(R_UNUSED) is None and cluster.master.get(R_UNUSED) is not None
    del cluster.master.fail[("delete", R_UNUSED)]
    return d["preview_id"]


@pytest.mark.asyncio
async def test_half_done_cluster_delete_can_be_finished(cluster):  # review finding 5
    await _half_deleted_cluster(cluster)
    again = await preview(cluster.a, [R_UNUSED], kind="delete")
    assert [(n["host_name"], len(n["items"])) for n in again["plan"]["nodes"]] == [("fw-b", 0), ("fw-a", 1)]
    cluster.log.clear()
    assert (await deploy(cluster.a, again["preview_id"]))["status"] == "done"
    assert [w for w in _writes(cluster.log) if "delRule" in w[1]] == [(FW1, f"/firewall/filter/delRule/{R_UNUSED}")]
    assert cluster.master.get(R_UNUSED) is None
    got = await refused(cluster.a, svc.preview, actor="admin", kind="delete", uuids=[R_UNUSED])  # nothing left
    assert got["params"]["reason"] == "not_candidate"


@pytest.mark.asyncio
async def test_half_done_cluster_delete_can_be_restored_where_it_was_deleted(cluster):  # review finding 5
    delete_op = await _half_deleted_cluster(cluster)
    async with SessionLocal() as db:
        deleted = (await db.scalars(select(RuleCleanupItem).where(
            RuleCleanupItem.operation_id == uuid_lib.UUID(delete_op), RuleCleanupItem.state == "deleted"))).one()
    result = await call(cluster.a, svc.restore_item, deleted.id, actor="admin", confirm_name="fw-a")
    assert result["status"] == "done" and [i["host_id"] for i in result["items"]] == [str(cluster.b)]
    assert cluster.backup.get(result["items"][0]["restored_uuid"])["enabled"] == "0"
    assert cluster.master.get(R_UNUSED)["enabled"] == "0"  # the master's copy was never deleted: untouched


@pytest.mark.asyncio
async def test_switching_a_block_rule_back_on_needs_an_acknowledgement_unless_proven_harmless(env):  # review finding 7
    lock_out = "aaaaaaaa-0000-4000-8000-000000000920"
    env.fw.rules.append(rule(lock_out, 920, action="block", interface="wan", src="203.0.113.0/24", dport="443",
                             enabled="0", description="block mgmt"))
    unknown = "aaaaaaaa-0000-4000-8000-000000000940"  # source is a URL table: match unknown
    env.fw.rules.append(rule(unknown, 940, action="block", interface="wan", src="tbl", dport="443", enabled="0",
                             description="block table"))
    harmless = "aaaaaaaa-0000-4000-8000-000000000930"
    env.fw.rules.append(rule(harmless, 930, action="block", proto="UDP", dport="5353", enabled="0", description="block mdns"))
    async with SessionLocal() as db:  # both were switched off by a confirmed cleanup earlier
        op = RuleCleanupOperation(host_id=env.host_id, kind="disable", status="confirmed", actor="admin",
                                  expires_at=_now(), preview_fingerprint="x", options={}, confirmed_at=_now())
        db.add(op)
        await db.flush()
        for u in (lock_out, harmless, unknown):
            db.add(RuleCleanupItem(operation_id=op.id, host_id=env.host_id, rule_uuid=u, state="confirmed", description=u))
        await db.commit()
    got = await refused(env.host_id, svc.preview, actor="admin", kind="enable", uuids=[lock_out, harmless, unknown])
    assert got["code"] == "BEREINIGUNG_BESTAETIGUNG_FEHLT" and got["params"]["uuids"] == ", ".join(sorted([lock_out, unknown]))
    ok = await preview(env.host_id, [lock_out, harmless, unknown], kind="enable", acknowledge=[lock_out, unknown])
    items = {i["uuid"]: i for i in ok["plan"]["nodes"][0]["items"]}
    assert items[lock_out]["needs_ack"] and items[lock_out]["notes"] == ["ENABLE_MAY_BLOCK_MANAGEMENT"]
    assert not items[harmless]["needs_ack"] and items[harmless]["protection"]["result"] == "false"


async def _unknown_restore(env):
    """A restore whose addRule answer was lost: the item stays unknown."""
    await _confirmed_disable(env.host_id, [R_UNUSED])
    p = await preview(env.host_id, [R_UNUSED], kind="delete")
    await deploy(env.host_id, p["preview_id"])
    deleted = next(iter((await _items(p["preview_id"])).values()))
    snapshot_row = dict(deleted.snapshot["row"])
    env.fw.fail[("add", "")] = httpx.ReadTimeout
    result = await call(env.host_id, svc.restore_item, deleted.id, actor="admin", confirm_name="fw-a")
    assert result["items"][0]["state"] == "unknown"
    del env.fw.fail[("add", "")]
    return result, snapshot_row


@pytest.mark.asyncio
async def test_unclear_restore_uses_the_stored_uuid_before_searching_by_content(env):  # review finding 10
    result, row = await _unknown_restore(env)
    # on the firewall: the real copy and a content-identical twin someone made by hand
    real, twin = "cccccccc-0000-4000-8000-000000000001", "cccccccc-0000-4000-8000-000000000002"
    for u, seq in ((real, 950), (twin, 960)):
        env.fw.rules.append({**row, "uuid": u, "sequence": str(seq), "sort_order": f"400000.{seq:07d}"})
    async with SessionLocal() as db:
        await db.execute(update(RuleCleanupItem).where(RuleCleanupItem.id == uuid_lib.UUID(result["items"][0]["id"]))
                         .values(restored_uuid=real))
        await db.commit()
    done = await call(env.host_id, svc.retry, uuid_lib.UUID(result["id"]))
    assert done["items"][0]["state"] == "restored" and done["items"][0]["restored_uuid"] == real


@pytest.mark.asyncio
async def test_unclear_restore_with_twins_and_no_stored_uuid_stays_unknown(env):  # review finding 10
    result, row = await _unknown_restore(env)
    for u, seq in (("cccccccc-0000-4000-8000-000000000003", 950), ("cccccccc-0000-4000-8000-000000000004", 960)):
        env.fw.rules.append({**row, "uuid": u, "sequence": str(seq), "sort_order": f"400000.{seq:07d}"})
    done = await call(env.host_id, svc.retry, uuid_lib.UUID(result["id"]))
    assert done["items"][0]["state"] == "unknown" and done["items"][0]["message"] == "BEREINIGUNG_AUSGANG_UNKLAR"
    env.fw.rules = [r for r in env.fw.rules if not r["uuid"].startswith("cccccccc")]
    done = await call(env.host_id, svc.retry, uuid_lib.UUID(result["id"]))  # nothing there: it was never created
    assert done["items"][0]["state"] == "failed"


@pytest.mark.asyncio
async def test_an_outdated_acknowledgement_text_is_refused(env):  # review finding 11
    for version in ("0", "2", ""):
        got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[R_BLOCK],
                            acknowledge=[R_BLOCK], ack_text_version=version)
        assert got["status"] == 409 and got["code"] == "BEREINIGUNG_BESTAETIGUNGSTEXT_VERALTET", version
    assert env.fw.calls == []  # refused before anything is read
    ok = await preview(env.host_id, [R_BLOCK], acknowledge=[R_BLOCK], ack_text_version=svc.ACK_TEXT_VERSION)
    assert ok["plan"]["acknowledged"] == [R_BLOCK]


@pytest.mark.asyncio
async def test_opnexus_own_address_is_protected_even_when_only_workstations_are_configured(env, monkeypatch):
    """B10: the management sources may list only the admins' workstations. A rule that is the only way OPNexus itself
    reaches the firewall would then look harmless, and after switching it off the automatic revert could not reach the
    firewall any more. The address the firewall sees OPNexus come from is protected as well."""
    from app.services import opnsense_client
    from app.services.rule_cleanup_service import redact

    custom = "aaaaaaaa-0000-4000-8000-000000000920"
    env.fw.rules.append(rule(custom, 920, interface="wan", src="198.51.100.0/24", dport="443", description="opnexus server net"))
    await _seed_hits(env.host_id, [custom])
    ok = await preview(env.host_id, [custom])  # address unknown (respx has no socket): configured sources only
    assert ok["plan"]["nodes"][0]["opnexus_source"] == {"address": None, "status": "unknown"}

    async def seen_from(_host):
        return "198.51.100.7"

    monkeypatch.setattr(opnsense_client, "own_source_address", seen_from)
    got = await refused(env.host_id, svc.preview, actor="admin", kind="disable", uuids=[custom])
    assert got["code"] == "BEREINIGUNG_REGEL_NICHT_AENDERBAR" and got["params"]["reason"] == "management"
    assert env.fw.writes() == []

    async def configured(_host):
        return MGMT_SOURCE

    monkeypatch.setattr(opnsense_client, "own_source_address", configured)
    p = await preview(env.host_id, [R_UNUSED])
    node = p["plan"]["nodes"][0]
    assert node["opnexus_source"] == {"address": MGMT_SOURCE, "status": "configured"}
    hidden = redact(p)["plan"]["nodes"][0]  # an address is security-relevant like the sources: admins only
    assert hidden["opnexus_source"] == {"status": "configured"}


@pytest.mark.asyncio
async def test_own_source_address_matches_exactly_this_connection(monkeypatch, respx_mock):
    """The state whose source port is the local port of the request; nothing else (other clients on 443, old states)."""
    import httpx as _httpx

    from app.models.host import OpnsenseHost
    from app.services import opnsense_client, opnsense_secret_store

    host = OpnsenseHost(id=uuid_lib.uuid4(), name="fw-src", base_url="https://192.0.2.1", api_key="k", verify_tls=False)
    opnsense_secret_store.save_secret(host.id, "s")
    respx_mock.get("https://192.0.2.1/api/diagnostics/firewall/pf_states").mock(return_value=_httpx.Response(200, json={"current": 3}))
    states = respx_mock.post("https://192.0.2.1/api/diagnostics/firewall/query_states")
    rows = [
        {"src_addr": "203.0.113.50", "src_port": "40000", "dst_addr": "192.0.2.1", "dst_port": "443", "state": "ESTABLISHED:ESTABLISHED"},  # a browser
        {"src_addr": "192.0.2.10", "src_port": "55555", "dst_addr": "192.0.2.1", "dst_port": "443", "state": "TIME_WAIT:TIME_WAIT"},  # old
        {"src_addr": "192.0.2.10", "src_port": "33160", "dst_addr": "192.0.2.1", "dst_port": "443", "state": "ESTABLISHED:ESTABLISHED"},  # us
    ]
    states.mock(return_value=_httpx.Response(200, json={"rows": rows}))
    addrs = {"server_addr": ("192.0.2.1", 443), "client_addr": ("172.22.0.5", 33160)}
    monkeypatch.setattr(opnsense_client, "_stream_addr", lambda _resp, which: addrs.get(which))
    assert await opnsense_client.own_source_address(host) == "192.0.2.10"
    assert json.loads(states.calls.last.request.content)["searchPhrase"] == "192.0.2.1"

    addrs["client_addr"] = ("172.22.0.5", 40001)  # NAT rewrote the port: no guessing
    assert await opnsense_client.own_source_address(host) is None
    addrs["server_addr"] = None  # through the overlay's CONNECT proxy
    assert await opnsense_client.own_source_address(host) is None
    states.mock(return_value=_httpx.Response(403, json={"message": "forbidden"}))
    addrs["server_addr"] = ("192.0.2.1", 443)
    with pytest.raises(opnsense_client.OpnsenseAPIError):
        await opnsense_client.own_source_address(host)
    opnsense_secret_store.delete_secret(host.id)
