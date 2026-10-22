"""Change history of live rules (1.30, backlog B4 part 1): observation loop logic, origin, idempotence, the
history endpoint, the usage sampling via ruleStats and the cleanup endpoint/MCP tool.

Database tests drive ``rule_history_service`` with searchRule answers shaped like the live ones; the HTTP tests
mock the firewall with respx.
"""
import asyncio
import json
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent
from app.models.host import OpnsenseHost
from app.models.rule_hit import RuleHit
from app.models.rule_history import LiveRuleState, RuleChangeEvent
from app.services import rule_hit_service, rule_history_service as rhs
from app.services.rule_analysis import context as ctx_mod
from tests.test_api_tokens import _bearer, _make
from tests.test_rule_analysis import FIXTURE, mvc
from tests.test_rule_analysis_api import FW, _mock_fw
from tests.test_rules import _create_host

LIVE = json.loads((Path(__file__).parent / "fixtures" / "rule_stats_live.json").read_text())
T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
R1, R2, R3 = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"


def answer(*rules, automatic=True):
    """A searchRule answer: optional automatic rows plus MVC rows (internal helper keys removed)."""
    rows = [dict(r) for r in FIXTURE["rules"]["rows"] if r.get("is_automatic")][:3] if automatic else []
    for rule in rules:
        row = dict(rule)
        row.pop("_loaded", None)
        rows.append(row)
    return {"rows": rows}


def rule(uuid, **kw):
    kw.setdefault("proto", "tcp")
    kw.setdefault("dport", "443")
    return mvc(uuid, **kw)


async def host_of(client, respx_mock, name="fw1", url="https://fw1.test"):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    return uuid_lib.UUID((await _create_host(client, name, url))["id"])


async def observe(host_id, search, at):
    async with SessionLocal() as db:
        host = await db.get(OpnsenseHost, host_id)
        return await rhs.observe_rows(db, host, search, now=at)


async def events(host_id):
    async with SessionLocal() as db:
        rows = (await db.scalars(select(RuleChangeEvent).where(RuleChangeEvent.host_id == host_id)
                                 .order_by(RuleChangeEvent.observed_at, RuleChangeEvent.seq))).all()
        return [(e.rule_uuid, e.kind, e.origin, e.seq) for e in rows]


# ── pure helpers ──────────────────────────────────────────────────────────

def test_clean_row_drops_derived_display_fields_and_the_fingerprint_ignores_them():
    row = {"uuid": R1, "enabled": "1", "description": "x", "%action": "Pass", "alias_meta_source_net": [{"value": "a"}],
           "#priority": 400000, "category_colors": []}
    other = dict(row, **{"%action": "Erlauben", "alias_meta_source_net": [{"value": "b"}]})
    assert rhs.clean_row(row) == {"uuid": R1, "enabled": "1", "description": "x"}
    assert rhs.fingerprint(rhs.clean_row(row)) == rhs.fingerprint(rhs.clean_row(other))
    assert rhs.fingerprint(rhs.clean_row(dict(row, description="y"))) != rhs.fingerprint(rhs.clean_row(row))


def test_change_kind():
    base = {"uuid": R1, "enabled": "1", "description": "x"}
    assert rhs.change_kind(None, base) == "added" and rhs.change_kind(base, None) == "removed"
    assert rhs.change_kind(base, {**base, "enabled": "0"}) == "disabled"
    assert rhs.change_kind({**base, "enabled": "0"}, base) == "enabled"
    assert rhs.change_kind(base, {**base, "enabled": "0", "description": "y"}) == "changed"
    assert rhs.change_kind(base, {**base, "description": "y"}) == "changed"


def test_diff_fields_is_short_and_exact():
    assert rhs.diff_fields({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == [
        {"field": "b", "before": 2, "after": 3}, {"field": "c", "before": None, "after": 4}]
    assert len(rhs.diff_fields({}, {str(i): i for i in range(40)})) == 12


# ── observation ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_observation_stores_the_starting_point_without_events(client, respx_mock):
    host = await host_of(client, respx_mock)
    result = await observe(host, answer(rule(R1), rule(R2, seq=1001)), T0)
    assert result == {"baseline": True, "events": 0, "touched": []}
    assert await events(host) == []
    async with SessionLocal() as db:
        keys = {s.rule_uuid for s in (await db.scalars(select(LiveRuleState).where(LiveRuleState.host_id == host))).all()}
    assert keys == {R1, R2, rhs.BASELINE_KEY}  # automatic rules are not tracked


@pytest.mark.asyncio
async def test_a_host_without_user_rules_still_gets_its_baseline(client, respx_mock):
    """The first rule created afterwards must show up as 'added', not vanish into a second baseline."""
    host = await host_of(client, respx_mock)
    await observe(host, answer(), T0)
    await observe(host, answer(rule(R1)), T0 + timedelta(minutes=15))
    assert await events(host) == [(R1, "added", "external", 1)]


@pytest.mark.asyncio
async def test_unchanged_rules_make_no_events_and_the_cycle_is_repeatable(client, respx_mock):
    host = await host_of(client, respx_mock)
    search = answer(rule(R1))
    await observe(host, search, T0)
    for n in (1, 2, 3):
        assert (await observe(host, search, T0 + timedelta(minutes=15 * n)))["events"] == 0
    assert await events(host) == []


@pytest.mark.asyncio
async def test_added_changed_disabled_enabled_removed_and_added_again(client, respx_mock):
    host = await host_of(client, respx_mock)
    await observe(host, answer(rule(R1)), T0)
    step = lambda n: T0 + timedelta(minutes=15 * n)  # noqa: E731
    await observe(host, answer(rule(R1), rule(R2, seq=1001)), step(1))                           # added
    await observe(host, answer(rule(R1, dport="8443"), rule(R2, seq=1001)), step(2))            # changed
    await observe(host, answer(rule(R1, dport="8443"), rule(R2, seq=1001, enabled="0")), step(3))  # disabled
    await observe(host, answer(rule(R1, dport="8443"), rule(R2, seq=1001)), step(4))            # enabled
    await observe(host, answer(rule(R1, dport="8443")), step(5))                                 # removed
    await observe(host, answer(rule(R1, dport="8443"), rule(R2, seq=1001)), step(6))            # added again
    assert await events(host) == [
        (R2, "added", "external", 1), (R1, "changed", "external", 2), (R2, "disabled", "external", 2),
        (R2, "enabled", "external", 3), (R2, "removed", "external", 4), (R2, "added", "external", 5)]
    async with SessionLocal() as db:
        changed = (await db.scalars(select(RuleChangeEvent).where(RuleChangeEvent.kind == "changed"))).one()
        assert changed.before["destination_port"] == "443" and changed.after["destination_port"] == "8443"
        gone = (await db.scalars(select(RuleChangeEvent).where(RuleChangeEvent.kind == "removed"))).one()
        assert gone.before["uuid"] == R2 and gone.after is None


@pytest.mark.asyncio
async def test_derived_display_changes_are_not_rule_changes(client, respx_mock):
    host = await host_of(client, respx_mock)
    await observe(host, answer(rule(R1, **{"%action": "Pass", "alias_meta_source_net": [{"value": "a"}]})), T0)
    await observe(host, answer(rule(R1, **{"%action": "Erlauben", "alias_meta_source_net": [{"value": "b"}]})), T0 + timedelta(minutes=15))
    assert await events(host) == []


@pytest.mark.asyncio
async def test_an_error_answer_does_not_remove_everything(client, respx_mock):
    host = await host_of(client, respx_mock)
    await observe(host, answer(rule(R1)), T0)
    for bad in ({"status": "failed"}, [], {"rows": "x"}, None):
        assert (await observe(host, bad, T0 + timedelta(minutes=15)))["events"] == 0
    assert await events(host) == []
    await observe(host, answer(), T0 + timedelta(minutes=30))  # all rules gone: confirmed only by a second look
    assert await events(host) == []
    await observe(host, answer(), T0 + timedelta(minutes=45))
    assert await events(host) == [(R1, "removed", "external", 2)]


@pytest.mark.asyncio
async def test_two_processes_seeing_the_same_change_record_it_once(client, respx_mock):
    """mutation: without the version claim / unique key both processes insert an event."""
    host = await host_of(client, respx_mock)
    await observe(host, answer(rule(R1)), T0)
    changed = answer(rule(R1, dport="8443"), rule(R2, seq=1001))
    await asyncio.gather(*(observe(host, changed, T0 + timedelta(minutes=15)) for _ in range(4)))
    assert sorted(await events(host)) == sorted([(R1, "changed", "external", 2), (R2, "added", "external", 1)])
    again = await observe(host, changed, T0 + timedelta(minutes=30))
    assert again["events"] == 0 and len(await events(host)) == 2


@pytest.mark.asyncio
async def test_origin_is_opnexus_when_the_audit_trail_has_a_deployment_in_the_window(client, respx_mock):
    host = await host_of(client, respx_mock)
    await observe(host, answer(rule(R1)), T0)
    async with SessionLocal() as db:
        db.add(AuditEvent(created_at=T0 + timedelta(minutes=5), correlation_id="c1", actor="alice", kind="rule", action="deploy",
                          status="pending", host_id=str(host), details={"remote_uuid": R2}))
        db.add(AuditEvent(created_at=T0 + timedelta(minutes=6), correlation_id="c2", actor="mallory", kind="rule", action="deploy",
                          status="error", host_id=str(host), details={"remote_uuid": R3}))  # failed: changed nothing
        await db.commit()
    await observe(host, answer(rule(R1), rule(R2, seq=1001), rule(R3, seq=1002)), T0 + timedelta(minutes=15))
    async with SessionLocal() as db:
        by_rule = {e.rule_uuid: e for e in (await db.scalars(select(RuleChangeEvent))).all()}
    assert (by_rule[R2].origin, by_rule[R2].actor) == ("opnexus", "alice") and by_rule[R2].audit_id is not None
    assert (by_rule[R3].origin, by_rule[R3].actor) == ("external", None)


@pytest.mark.asyncio
async def test_a_late_audit_entry_turns_an_external_event_into_an_opnexus_one(client, respx_mock):
    host = await host_of(client, respx_mock)
    await observe(host, answer(rule(R1)), T0)
    seen = T0 + timedelta(minutes=15)
    await observe(host, answer(rule(R1), rule(R2, seq=1001)), seen)
    assert await events(host) == [(R2, "added", "external", 1)]
    async with SessionLocal() as db:  # the deployment's audit row is committed a moment after the firewall changed
        db.add(AuditEvent(created_at=seen + timedelta(seconds=3), correlation_id="c", actor="alice", kind="rule",
                          action="deploy", status="success", host_id=str(host), details={"remote_uuid": R2}))
        await db.commit()
        assert await rhs.reconcile_origins(db, await db.get(OpnsenseHost, host), now=seen + timedelta(minutes=15)) == 1
    assert await events(host) == [(R2, "added", "opnexus", 1)]


async def _audit(host, rule_uuid, at, actor="alice", status="success"):
    async with SessionLocal() as db:
        db.add(AuditEvent(created_at=at, correlation_id=str(uuid_lib.uuid4()), actor=actor, kind="rule", action="deploy",
                          status=status, host_id=str(host), details={"remote_uuid": rule_uuid}))
        await db.commit()


async def _reconcile(host, now):
    async with SessionLocal() as db:
        return await rhs.reconcile_origins(db, await db.get(OpnsenseHost, host), now=now)


@pytest.mark.asyncio
async def test_a_later_external_change_is_not_blamed_on_an_older_opnexus_deployment(client, respx_mock):
    """mutation: without the window start the re-check reaches back to the old deployment (here an unused one)."""
    host = await host_of(client, respx_mock)
    step = lambda n: T0 + timedelta(minutes=15 * n)  # noqa: E731
    await observe(host, answer(rule(R1), rule(R2, seq=1001)), T0)
    await _audit(host, R2, step(1) - timedelta(minutes=5))             # a deployment that changed nothing visible
    await observe(host, answer(rule(R1), rule(R2, seq=1001)), step(1))
    await observe(host, answer(rule(R1), rule(R2, seq=1001, dport="22")), step(2))   # changed in the firewall's GUI
    assert await events(host) == [(R2, "changed", "external", 2)]
    assert await _reconcile(host, step(3)) == 0
    assert await events(host) == [(R2, "changed", "external", 2)]


@pytest.mark.asyncio
async def test_one_audit_entry_explains_only_one_event(client, respx_mock):
    """mutation: without the 'already claimed' filter both changes of the rule turn into OpNexus changes."""
    host = await host_of(client, respx_mock)
    step = lambda n: T0 + timedelta(minutes=15 * n)  # noqa: E731
    await observe(host, answer(rule(R1)), T0)
    await observe(host, answer(rule(R1, dport="1")), step(1))
    await observe(host, answer(rule(R1, dport="2")), step(2))
    await _audit(host, R1, step(1) + timedelta(seconds=3))             # arrives late; inside both windows
    assert await _reconcile(host, step(3)) == 1
    async with SessionLocal() as db:
        origins = [e.origin for e in (await db.scalars(select(RuleChangeEvent).order_by(RuleChangeEvent.observed_at))).all()]
    assert origins == ["opnexus", "external"]                         # the earlier change keeps it


@pytest.mark.asyncio
async def test_observe_host_settles_old_events_before_recording_new_ones(client, respx_mock):
    """mutation: reconciling after the observation lets the newer change of the same rule take the late entry."""
    host = await host_of(client, respx_mock, url=FW)
    step = lambda n: T0 + timedelta(minutes=15 * n)  # noqa: E731
    await observe(host, answer(rule(R1)), T0)
    await observe(host, answer(rule(R1, dport="1")), step(1))
    await _audit(host, R1, step(1) + timedelta(seconds=3))
    _mock_observe(respx_mock, FW, answer(rule(R1, dport="2")), httpx.Response(200, json={"status": "ok", "stats": {}}))
    async with SessionLocal() as db:
        await rhs.observe_host(db, await db.get(OpnsenseHost, host), now=step(2))
    assert [(e[1], e[2]) for e in await events(host)] == [("changed", "opnexus"), ("changed", "external")]


@pytest.mark.asyncio
async def test_pruning_removes_old_events_and_tombstones_only(client, respx_mock):
    host = await host_of(client, respx_mock)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=400)
    await observe(host, answer(rule(R1), rule(R2, seq=1001)), old)
    await observe(host, answer(rule(R1, dport="1"), rule(R2, seq=1001)), old + timedelta(minutes=15))
    await observe(host, answer(rule(R1, dport="1")), old + timedelta(minutes=30))      # R2 removed long ago
    await observe(host, answer(rule(R1, dport="2")), now)                              # recent change
    async with SessionLocal() as db:
        assert await rhs.prune(db) >= 3
        left = [(e.rule_uuid, e.kind) for e in (await db.scalars(select(RuleChangeEvent))).all()]
        states = {s.rule_uuid for s in (await db.scalars(select(LiveRuleState))).all()}
    assert left == [(R1, "changed")] and states == {R1, rhs.BASELINE_KEY}


# ── usage sampling via ruleStats ──────────────────────────────────────────

def test_parse_rule_stats_keeps_uuid_and_legacy_hex_labels():
    got = rule_hit_service.parse_rule_stats(LIVE["ruleStats"])
    assert got["db610dbc7c6467261cf08e8a72ade096"]["packets"] == 110719       # unmigrated legacy rule
    assert got["2ec1c6db-61e1-4476-b3f6-0599a28990a2"]["bytes"] == 99582131
    assert rule_hit_service.parse_rule_stats({"status": "failed"}) is None
    assert rule_hit_service.parse_rule_stats({"stats": []}) is None and rule_hit_service.parse_rule_stats(None) is None


def test_the_legacy_user_rule_is_a_configured_rule_but_automatic_ones_are_not():
    rows = {"rows": [LIVE["legacy_row"], {"uuid": "02f4bab031b57d1e30553ce08e0ec131", "is_automatic": True, "enabled": "1"}]}
    assert set(rule_hit_service.configured_rules(rows)) == {"db610dbc7c6467261cf08e8a72ade096"}
    assert set(rule_hit_service.configured_rules({"rows": [{"uuid": R1, "is_automatic": False}, {"uuid": R2, "is_automatic": "0"}]})) == {R1, R2}


def _mock_observe(respx_mock, base, search, stats):
    respx_mock.get(f"{base}/api/firewall/filter/searchRule").mock(return_value=httpx.Response(200, json=search))
    respx_mock.get(f"{base}/api/firewall/filter_util/ruleStats").mock(return_value=stats)


@pytest.mark.asyncio
async def test_observe_host_reads_two_endpoints_and_feeds_usage_and_history(client, respx_mock):
    host = await host_of(client, respx_mock, url=FW)
    legacy = {**LIVE["legacy_row"], "enabled": "1"}
    _mock_observe(respx_mock, FW, answer(legacy, rule(R1)), httpx.Response(200, json={
        "status": "ok", "stats": {**LIVE["ruleStats"]["stats"], R1: {"packets": 0, "bytes": 0, "evaluations": 3, "pf_rules": 1}}}))
    async with SessionLocal() as db:
        await rhs.observe_host(db, await db.get(OpnsenseHost, host), now=T0)
        hits = {h.rule_uuid: h for h in (await db.scalars(select(RuleHit))).all()}
    assert set(hits) == {"db610dbc7c6467261cf08e8a72ade096", R1}
    assert hits["db610dbc7c6467261cf08e8a72ade096"].last_hit_at == T0 and hits[R1].last_hit_at is None
    gets = [c.request.url.path for c in respx_mock.calls if "/api/firewall" in c.request.url.path]
    assert sorted(gets) == ["/api/firewall/filter/searchRule", "/api/firewall/filter_util/ruleStats"]
    assert len(await events(host)) == 0  # baseline


@pytest.mark.asyncio
async def test_usage_falls_back_to_pf_statistics_when_rule_stats_is_missing(client, respx_mock):
    host = await host_of(client, respx_mock, url=FW)
    _mock_observe(respx_mock, FW, answer(rule(R1)), httpx.Response(404, json={"message": "not found"}))
    respx_mock.get(f"{FW}/api/diagnostics/firewall/pfStatistics/rules").mock(return_value=httpx.Response(200, json={
        "rules": {"filter rules": {f'@5 pass in quick on vtnet1 inet all label "{R1}"': {"packets": 7, "bytes": 70, "evaluations": 9}}}}))
    async with SessionLocal() as db:
        await rhs.observe_host(db, await db.get(OpnsenseHost, host), now=T0)
        row = (await db.scalars(select(RuleHit))).one()
    assert row.rule_uuid == R1 and row.last_hit_at == T0 and row.last_packets == 7


@pytest.mark.asyncio
async def test_a_failing_host_does_not_stop_the_cycle_for_the_others(client, respx_mock):
    good = await host_of(client, respx_mock, "good", FW)
    await host_of(client, respx_mock, "bad", "https://bad.test")
    _mock_observe(respx_mock, FW, answer(rule(R1)), httpx.Response(200, json={"status": "ok", "stats": {}}))
    respx_mock.route(url__startswith="https://bad.test").mock(side_effect=httpx.ConnectError("down"))
    async with SessionLocal() as db:
        summary = await rhs.observe_all(db, now=T0)
    assert summary == {"hosts": 1, "failed": 1, "events": 0}
    assert await events(good) == []
    async with SessionLocal() as db:
        assert (await db.get(LiveRuleState, (good, R1))) is not None


# ── answers that are not to be trusted ────────────────────────────────────

@pytest.mark.asyncio
async def test_an_answer_without_automatic_rules_is_rejected_and_nothing_is_lost(client, respx_mock):
    """mutation: without the completeness check a partial answer removes every rule and every usage row."""
    host = await host_of(client, respx_mock, url=FW)
    stats = httpx.Response(200, json={"status": "ok", "stats": {R1: {"packets": 3, "bytes": 1, "evaluations": 1}}})
    _mock_observe(respx_mock, FW, answer(rule(R1), rule(R2, seq=1001)), stats)
    async with SessionLocal() as db:
        await rhs.observe_host(db, await db.get(OpnsenseHost, host), now=T0)
    respx_mock.clear()
    partial = answer(rule(R1), automatic=False)
    partial["rows"][0]["is_automatic"] = "0"  # a firmware that flags automatic rows, but none is in the answer
    _mock_observe(respx_mock, FW, partial, stats)                                   # R2 and all automatic rows missing
    async with SessionLocal() as db:
        result = await rhs.observe_host(db, await db.get(OpnsenseHost, host), now=T0 + timedelta(minutes=15))
        assert result["skipped"] is True
        assert {h.rule_uuid for h in (await db.scalars(select(RuleHit))).all()} == {R1, R2}
        assert await rule_hit_service.apply_sample(db, await db.get(OpnsenseHost, host), {}, {"rows": []}) == 0
        assert {h.rule_uuid for h in (await db.scalars(select(RuleHit))).all()} == {R1, R2}
    assert await events(host) == []


@pytest.mark.asyncio
async def test_losing_most_rules_at_once_needs_a_second_look(client, respx_mock):
    """mutation: acting on the first look turns one broken answer into removed + added events for everything."""
    host = await host_of(client, respx_mock)
    step = lambda n: T0 + timedelta(minutes=15 * n)  # noqa: E731
    full = answer(rule(R1), rule(R2, seq=1001), rule(R3, seq=1002))
    await observe(host, full, T0)
    thin = answer(rule(R1))                                           # two of three gone
    assert (await observe(host, thin, step(1)))["unconfirmed"] == 2
    assert (await observe(host, full, step(2)))["events"] == 0        # it was a hiccup: nothing happened
    assert await events(host) == []
    assert (await observe(host, thin, step(3)))["skipped"] is True    # the marker was cleared, so this is a first look again
    await observe(host, thin, step(4))                                # same picture twice: real
    assert sorted(await events(host)) == sorted([(R2, "removed", "external", 2), (R3, "removed", "external", 2)])


@pytest.mark.asyncio
async def test_an_unconfirmed_answer_does_not_delete_usage_data_either(client, respx_mock):
    """mutation: sampling an unconfirmed answer drops the usage rows of the 'vanished' rules."""
    host = await host_of(client, respx_mock, url=FW)
    stats = httpx.Response(200, json={"status": "ok", "stats": {}})
    for n, search in ((0, answer(rule(R1), rule(R2, seq=1001), rule(R3, seq=1002))), (1, answer(rule(R1)))):
        respx_mock.clear()
        _mock_observe(respx_mock, FW, search, stats)
        async with SessionLocal() as db:
            await rhs.observe_host(db, await db.get(OpnsenseHost, host), now=T0 + timedelta(minutes=15 * n))
    async with SessionLocal() as db:
        assert {h.rule_uuid for h in (await db.scalars(select(RuleHit))).all()} == {R1, R2, R3}


@pytest.mark.asyncio
async def test_losing_at_most_half_of_the_rules_is_recorded_at_once(client, respx_mock):
    host = await host_of(client, respx_mock)
    await observe(host, answer(rule(R1), rule(R2, seq=1001), rule(R3, seq=1002)), T0)
    await observe(host, answer(rule(R1), rule(R2, seq=1001)), T0 + timedelta(minutes=15))
    assert await events(host) == [(R3, "removed", "external", 2)]


# ── usage window ──────────────────────────────────────────────────────────

async def _hit(host, uuid=R1):
    async with SessionLocal() as db:
        return (await db.scalars(select(RuleHit).where(RuleHit.host_id == host, RuleHit.rule_uuid == uuid))).one()


async def _apply(host, packets, search, at):
    async with SessionLocal() as db:
        return await rule_hit_service.apply_sample(
            db, await db.get(OpnsenseHost, host), {R1: {"packets": packets, "bytes": 0, "evaluations": 0}}, search, at)


@pytest.mark.asyncio
async def test_a_rule_that_was_switched_on_starts_its_observation_window_again(client, respx_mock):
    """mutation: an old first_seen_at makes a rule enabled yesterday 'unused, sure' today."""
    host = await host_of(client, respx_mock)
    await _apply(host, 0, answer(rule(R1, enabled="0")), T0)
    await _apply(host, 0, answer(rule(R1, enabled="0")), T0 + timedelta(days=40))
    assert rule_hit_service.classify(await _hit(host), T0 + timedelta(days=40)) == "disabled"
    await _apply(host, 0, answer(rule(R1)), T0 + timedelta(days=41))        # switched on
    row = await _hit(host)
    assert row.first_seen_at == T0 + timedelta(days=41)
    assert rule_hit_service.classify(row, T0 + timedelta(days=42)) == "collecting"
    assert rule_hit_service.classify(row, T0 + timedelta(days=72)) == "unused"


@pytest.mark.asyncio
async def test_a_counter_reset_after_a_long_gap_restarts_the_window_but_not_after_a_short_one(client, respx_mock):
    """mutation: ignoring the gap treats hits lost to a reload during a long outage as 'no hits'."""
    host = await host_of(client, respx_mock)
    search = answer(rule(R1))
    await _apply(host, 5, search, T0)
    await _apply(host, 0, search, T0 + timedelta(minutes=15))              # reload, normal cadence
    assert (await _hit(host)).first_seen_at == T0
    await _apply(host, 4, search, T0 + timedelta(minutes=30))              # grew: a hit
    await _apply(host, 0, search, T0 + timedelta(hours=5))                 # reload after a gap of hours
    row = await _hit(host)
    assert row.first_seen_at == T0 + timedelta(hours=5) and row.last_hit_at == T0 + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_an_edited_rule_restarts_its_window_and_a_move_does_not(client, respx_mock):
    """mutation: without restart_window an edited rule keeps its old observation window."""
    host = await host_of(client, respx_mock, url=FW)
    step = lambda n: T0 + timedelta(days=n)  # noqa: E731
    stats = httpx.Response(200, json={"status": "ok", "stats": {R1: {"packets": 0, "bytes": 0, "evaluations": 0}}})
    for n, search in ((0, answer(rule(R1))), (40, answer(rule(R1, dport="8443"))), (41, answer(rule(R1, dport="8443", seq=1005)))):
        respx_mock.clear()
        _mock_observe(respx_mock, FW, search, stats)
        async with SessionLocal() as db:
            await rhs.observe_host(db, await db.get(OpnsenseHost, host), now=step(n))
        if n == 40:
            assert (await _hit(host)).first_seen_at == step(40)             # edited: window starts over
    assert (await _hit(host)).first_seen_at == step(40)                     # moved: unchanged
    assert [(e[1]) for e in await events(host)] == ["changed", "moved"]


def test_a_move_is_a_move():
    base = {"uuid": R1, "enabled": "1", "sequence": "5", "sort_order": "400000.0000005", "description": "x"}
    assert rhs.change_kind(base, {**base, "sequence": "9", "sort_order": "400000.0000009"}) == "moved"
    assert rhs.change_kind(base, {**base, "sequence": "9", "description": "y"}) == "changed"
    assert rhs.change_kind(base, {**base, "sequence": "9", "enabled": "0"}) == "changed"


# ── endpoints ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_history_endpoint_merges_observed_changes_revisions_and_audit(client, respx_mock):
    host = await host_of(client, respx_mock)
    await observe(host, answer(rule(R1)), T0)
    await observe(host, answer(rule(R1, dport="8443")), T0 + timedelta(minutes=15))
    async with SessionLocal() as db:
        db.add(AuditEvent(created_at=T0 + timedelta(minutes=2), correlation_id="c", actor="alice", kind="rule", action="deploy",
                          status="success", host_id=str(host), message="ok", details={"remote_uuid": R1}))
        await db.commit()
    body = (await client.get(f"/api/hosts/{host}/live-rules/{R1}/history")).json()
    assert body["rule_uuid"] == R1 and body["watching_since"]
    assert [(e["source"], e["kind"]) for e in body["events"]] == [("observed", "changed"), ("audit", "deploy")]
    assert body["events"][0]["changes"] == [{"field": "destination_port", "before": "443", "after": "8443"}]
    empty = (await client.get(f"/api/hosts/{host}/live-rules/{R3}/history")).json()
    assert empty["events"] == []
    missing = await client.get(f"/api/hosts/{uuid_lib.uuid4()}/live-rules/{R1}/history")
    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "HOST_NICHT_GEFUNDEN"


@pytest.mark.asyncio
async def test_history_of_a_managed_rule_includes_its_saved_revisions(client, respx_mock):
    from app.models.policy import RuleRevision
    from app.models.rule import FirewallRule
    from app.models.rule_deployment import RuleDeployment
    host = await host_of(client, respx_mock)
    async with SessionLocal() as db:
        rule_row = FirewallRule(description="Web in", interface="lan", action="pass", direction="in", protocol="tcp",
                                source_net="any", destination_net="any", destination_port="443")
        db.add(rule_row)
        await db.flush()
        db.add(RuleDeployment(rule_id=rule_row.id, host_id=host, status="success", opnsense_rule_uuid=R1))
        db.add(RuleRevision(rule_id=rule_row.id, note="angelegt", snapshot={"destination_port": "443"}, created_at=T0))
        db.add(RuleRevision(rule_id=rule_row.id, note="Port geändert", snapshot={"destination_port": "8443"}, created_at=T0 + timedelta(hours=1)))
        await db.commit()
    body = (await client.get(f"/api/hosts/{host}/live-rules/{R1}/history")).json()
    revisions = [e for e in body["events"] if e["source"] == "revision"]
    assert [e["note"] for e in revisions] == ["Port geändert", "angelegt"]
    assert revisions[0]["changes"] == [{"field": "destination_port", "before": "443", "after": "8443"}]


@pytest.fixture(autouse=True)
def _fresh_cache():
    ctx_mod.clear_cache()
    yield
    ctx_mod.clear_cache()


async def _seed_hits(host_id, **hits):
    async with SessionLocal() as db:
        for uuid, (first_days, last_hit_days) in hits.items():
            now = datetime.now(timezone.utc)
            db.add(RuleHit(host_id=host_id, rule_uuid=uuid, description=uuid, enabled=True, first_seen_at=now - timedelta(days=first_days),
                           last_sampled_at=now - timedelta(minutes=10),
                           last_hit_at=None if last_hit_days is None else now - timedelta(days=last_hit_days)))
        await db.commit()


@pytest.mark.asyncio
async def test_cleanup_endpoint_lists_candidates_with_confidence_and_removability(client, respx_mock):
    from app.models.rule import FirewallRule
    from app.models.rule_deployment import RuleDeployment
    data = {**FIXTURE["rules"], "rows": FIXTURE["rules"]["rows"] + [
        {k: v for k, v in rule(R1, seq=1000, src="10.20.30.0/24", dport="").items() if k != "_loaded"},
        {k: v for k, v in rule(R2, seq=1001, src="10.20.30.5", dport="").items() if k != "_loaded"},
        {k: v for k, v in rule(R3, seq=1002, src="10.20.30.9", dport="22", enabled="0").items() if k != "_loaded"},
    ]}
    _mock_fw(respx_mock)
    respx_mock.get(f"{FW}/api/firewall/filter/searchRule").mock(return_value=httpx.Response(200, json=data))
    # pf only loads enabled rules: add lines for R1/R2 so they count as loaded
    pf = json.loads(json.dumps(FIXTURE["pf"]))
    for u, n in ((R1, 900), (R2, 901)):
        pf["rules"]["filter rules"][f'@{n} pass in quick on vtnet1_vlan10 inet proto tcp from any to any label "{u}"'] = {}
    respx_mock.get(f"{FW}/api/diagnostics/firewall/pfStatistics/rules").mock(return_value=httpx.Response(200, json=pf))
    host = await host_of(client, respx_mock, url=FW)
    await _seed_hits(host, **{R2: (100, None)})
    async with SessionLocal() as db:
        rule_row = FirewallRule(description="Managed web", interface="lan", action="pass", direction="in", protocol="tcp",
                                source_net="any", destination_net="any", destination_port="443")
        db.add(rule_row)
        await db.flush()
        db.add(RuleDeployment(rule_id=rule_row.id, host_id=host, status="success", opnsense_rule_uuid=R2))
        db.add(LiveRuleState(host_id=host, rule_uuid=R3, fingerprint="x", row={}, present=True, version=1,
                             first_seen_at=T0, changed_at=datetime.now(timezone.utc) - timedelta(days=50),
                             last_seen_at=datetime.now(timezone.utc)))
        await db.commit()
    resp = await client.get(f"/api/hosts/{host}/rule-analysis/cleanup", params={"unused_days": 30})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["unused_days"] == 30 and body["host_name"] == "fw1"
    by_uuid = {c["uuid"]: c for c in body["candidates"]}
    assert set(by_uuid) == {R2, R3}
    r2 = {c["kind"]: c for c in by_uuid[R2]["categories"]}
    assert r2["shadowed"]["by"]["uuid"] == R1 and r2["shadowed"]["confidence"] == "sure"
    assert r2["unused"]["confidence"] == "sure"
    assert by_uuid[R2]["removable"] is False and by_uuid[R2]["not_removable_reason"] == "managed"
    assert by_uuid[R2]["managed_by"]["name"] == "Managed web"
    assert [c["kind"] for c in by_uuid[R3]["categories"]] == ["disabled_long"] and by_uuid[R3]["removable"] is True
    assert body["counts"] == {"unused": 1, "shadowed": 1, "duplicate": 0, "disabled_long": 1}
    assert all(m == "GET" for m in {c.request.method for c in respx_mock.calls})
    # a narrower window / the parameter is validated
    assert (await client.get(f"/api/hosts/{host}/rule-analysis/cleanup", params={"unused_days": 0})).status_code == 422
    short = (await client.get(f"/api/hosts/{host}/rule-analysis/cleanup", params={"unused_days": 365})).json()
    assert {k["kind"] for c in short["candidates"] for k in c["categories"]} == {"shadowed"}


@pytest.mark.asyncio
async def test_cleanup_endpoint_roles_and_errors(client, respx_mock):
    _mock_fw(respx_mock)
    host = await host_of(client, respx_mock, url=FW)
    unknown = await client.get(f"/api/hosts/{uuid_lib.uuid4()}/rule-analysis/cleanup")
    assert unknown.status_code == 404 and unknown.json()["detail"]["code"] == "HOST_NICHT_GEFUNDEN"
    token = await _make(client, "read")
    async with _bearer(token["token"]) as api:
        assert (await api.get(f"/api/hosts/{host}/rule-analysis/cleanup")).status_code == 200
        assert (await api.get(f"/api/hosts/{host}/live-rules/{R1}/history")).status_code == 200
        assert (await api.post(f"/api/hosts/{host}/rule-analysis/cleanup")).status_code in (403, 405)
    ctx_mod.clear_cache()
    respx_mock.clear()
    respx_mock.route(url__startswith=FW).mock(side_effect=httpx.ConnectError("down"))
    down = await client.get(f"/api/hosts/{host}/rule-analysis/cleanup")
    assert down.status_code == 502 and down.json()["detail"]["code"] == "ANALYSE_FIREWALL_NICHT_LESBAR"


@pytest.mark.asyncio
async def test_anonymous_callers_get_nothing(anon_client):
    hid = uuid_lib.uuid4()
    assert (await anon_client.get(f"/api/hosts/{hid}/rule-analysis/cleanup")).status_code == 401
    assert (await anon_client.get(f"/api/hosts/{hid}/live-rules/{R1}/history")).status_code == 401


@pytest.mark.asyncio
async def test_mcp_cleanup_tool_works_with_a_read_token_and_changes_nothing(client, respx_mock):
    _mock_fw(respx_mock)
    host = await host_of(client, respx_mock, url=FW)
    token = await _make(client, "read")
    async with _bearer(token["token"]) as api:
        rpc = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "rule_cleanup_candidates", "arguments": {"host_id": str(host), "unused_days": 90}}}
        result = (await api.post("/api/mcp", json=rpc)).json()["result"]
        tools = (await api.post("/api/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).json()["result"]["tools"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["unused_days"] == 90 and payload["candidates"] == [] and payload["host_name"] == "fw1"
    tool = next(t for t in tools if t["name"] == "rule_cleanup_candidates")
    assert tool["inputSchema"]["required"] == ["host_id"] and "path" not in tool
    assert {c.request.method for c in respx_mock.calls if c.request.url.host != "test"} == {"GET"}


def test_firmware_without_the_automatic_flag_is_not_rejected_as_incomplete():
    from app.services import rule_hit_service
    assert rule_hit_service.looks_complete({"rows": [{"uuid": "u1"}, {"uuid": "u2"}]})   # no row carries the flag: non-empty is enough
    assert not rule_hit_service.looks_complete({"rows": []})
    assert not rule_hit_service.looks_complete({"rows": [{"uuid": "u1", "is_automatic": "0"}]})  # flagged firmware, no automatic rule: partial
    assert rule_hit_service.looks_complete({"rows": [{"uuid": "u1", "is_automatic": "0"}, {"uuid": "a", "is_automatic": "1"}]})
