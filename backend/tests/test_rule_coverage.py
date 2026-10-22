"""Coverage (shadowed/duplicate) and cleanup candidates: pure functions on NormRule (1.30, backlog B4 part 1).

Rules are built the way the live ``searchRule`` delivers them (tests/test_rule_analysis.py: fixture of a real
26.7 firewall plus synthetic MVC rows with matching pf lines). Cases that must stay red when the logic is broken
are marked "mutation" in their docstring.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.rule_analysis import cleanup, coverage
from app.services.rule_analysis.normalize import normalize
from tests.test_rule_analysis import build, mvc

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def overlaps(rows, **kw):
    ctx = build(rows, **kw)
    rules, notes = normalize(ctx)
    found = coverage.analyse(rules, ctx, order_guessed=coverage.order_doubtful(notes))
    return {o.rule.uuid: o for o in found}


# ── coverage ──────────────────────────────────────────────────────────────

def test_a_narrower_rule_behind_a_broad_quick_rule_is_shadowed_and_sure():
    found = overlaps([
        mvc("broad", seq=1000, action="block", src="10.20.30.0/24"),
        mvc("narrow", seq=1001, src="10.20.30.0/25", dport="443", proto="tcp"),
    ])
    assert found["narrow"].kind == "shadowed" and found["narrow"].by.uuid == "broad" and found["narrow"].confidence == "sure"
    assert "broad" not in found


def test_an_identical_rule_with_the_same_action_is_a_duplicate_a_different_action_is_only_shadowed():
    found = overlaps([
        mvc("first", seq=1000, src="10.20.30.0/24", dport="443", proto="tcp"),
        mvc("second", seq=1001, src="10.20.30.0/24", dport="443", proto="tcp"),
        mvc("other", seq=1002, src="10.20.30.0/24", dport="443", proto="tcp", action="block"),
    ])
    assert (found["second"].kind, found["second"].by.uuid) == ("duplicate", "first")
    assert found["other"].kind == "shadowed"


def test_the_wider_later_rule_is_not_covered_by_the_narrower_earlier_one():
    found = overlaps([
        mvc("narrow", seq=1000, src="10.20.30.64/26"),
        mvc("wide", seq=1001, src="10.20.30.0/24"),
    ])
    assert found == {}


def test_only_a_quick_rule_covers():
    """mutation: ignoring `quick` makes the last-match-wins rule look like a cover."""
    found = overlaps([
        mvc("floaty", seq=1000, quick="0", action="block"),
        mvc("later", seq=1001, src="10.20.30.0/24"),
    ])
    assert found == {}


def test_sets_ranges_and_protocol_families():
    rows = [
        mvc("all-ports", seq=1000, proto="tcp/udp", ipprotocol="inet46", dport="1000:2000", src="10.20.30.0/24"),
        mvc("inside", seq=1001, proto="tcp", ipprotocol="inet", dport="1500", src="10.20.30.5"),
        mvc("udp-edge", seq=1002, proto="udp", ipprotocol="inet", dport="2000", src="10.20.30.0/24"),
        mvc("outside", seq=1003, proto="tcp", ipprotocol="inet", dport="2001", src="10.20.30.5"),
        mvc("v6-inside-v46", seq=1004, proto="tcp", ipprotocol="inet6", dport="1500", src="2001:db8::5"),
        mvc("no-port", seq=1005, proto="tcp", ipprotocol="inet", src="10.20.30.5"),
        mvc("icmp", seq=1006, proto="icmp", ipprotocol="inet", src="10.20.30.5"),
    ]
    found = overlaps(rows)
    assert set(found) == {"inside", "udp-edge"}  # "v6-inside-v46": an IPv6 address is outside the 10.20.30.0/24 source
    assert all(o.by.uuid == "all-ports" for o in found.values())


def test_inet_does_not_cover_inet46_but_inet46_covers_inet6():
    found = overlaps([mvc("v4", seq=1000, ipprotocol="inet"), mvc("both", seq=1001, ipprotocol="inet46")])
    assert found == {}
    found = overlaps([mvc("both", seq=1000, ipprotocol="inet46"), mvc("v6", seq=1001, ipprotocol="inet6")])
    assert found["v6"].by.uuid == "both"


def test_a_narrow_earlier_rule_never_covers_an_any_later_one():
    found = overlaps([mvc("web", seq=1000, proto="tcp", dport="443"), mvc("any", seq=1001)])
    assert found == {}


def test_interface_sets_and_floating_order():
    rows = [
        mvc("float-quick", prio=200000, seq=5, interface="lan,wan", action="block", src="10.20.30.0/24", loaded=True),
        mvc("on-lan", seq=1000, interface="lan", src="10.20.30.7"),
        mvc("on-wan", seq=1001, interface="wan", src="10.20.30.7"),
        mvc("on-all", seq=1002, interface="", src="10.20.30.7"),
    ]
    found = overlaps(rows)
    assert found["on-lan"].by.uuid == "float-quick" and found["on-wan"].by.uuid == "float-quick"
    assert "on-all" not in found  # applies to interfaces the floating rule does not name


def test_a_later_floating_rule_cannot_be_covered_by_an_interface_rule():
    """mutation: sorting by `sequence` instead of `sort_order` would put the interface rule first."""
    found = overlaps([
        mvc("interface-rule", prio=400000, seq=1, interface="lan", src="any"),
        mvc("floating-rule", prio=200000, seq=9, interface="lan,wan", src="10.20.30.7"),
    ])
    assert "floating-rule" not in found
    # and the other way round: the floating rule is evaluated first and wins
    found = overlaps([
        mvc("interface-rule", prio=400000, seq=9, interface="lan", src="10.20.30.7"),
        mvc("floating-rule", prio=200000, seq=1, interface="lan,wan", src="10.20.30.0/24"),
    ])
    assert found["interface-rule"].by.uuid == "floating-rule"


def test_a_group_rule_comes_before_the_interface_rule():
    found = overlaps([
        mvc("plain", prio=400000, seq=1, interface="lan", src="10.20.30.7"),
        mvc("grouped", prio=300005, seq=900, interface="lan", src="10.20.30.0/24"),
    ])
    assert found["plain"].by.uuid == "grouped"


def test_conditions_the_engine_cannot_compare_prevent_a_claim():
    for extra in ({"sched": "work-hours"}, {"tagged": "mark"}, {"received-on": "wan"}):
        found = overlaps([mvc("cond", seq=1000, **extra), mvc("later", seq=1001, src="10.20.30.5")])
        assert found == {}, extra


def test_disabled_and_unloaded_rules_do_not_cover():
    found = overlaps([mvc("off", seq=1000, enabled="0"), mvc("later", seq=1001, src="10.20.30.5")])
    assert found == {}
    found = overlaps([mvc("not-loaded", seq=1000, loaded=False), mvc("later", seq=1001, src="10.20.30.5")])
    assert found == {}


def test_negation_is_compared_exactly():
    rows = [
        mvc("not-a", seq=1000, source_not="1", src="10.20.30.0/24"),
        mvc("same", seq=1001, source_not="1", src="10.20.30.0/24"),
        mvc("plain", seq=1002, src="10.20.30.5"),
    ]
    found = overlaps(rows)
    assert set(found) == {"same"} and found["same"].kind == "duplicate"


def test_a_negated_source_covers_a_negated_source_that_excludes_more():
    """mutation: using plain containment for '!' lists claims the wrong direction."""
    narrow_exclusion = mvc("not-narrow", seq=1000, source_not="1", src="10.1.0.0/16")
    wide_exclusion = mvc("not-wide", seq=1001, source_not="1", src="10.0.0.0/8")
    assert overlaps([narrow_exclusion, wide_exclusion])["not-wide"].by.uuid == "not-narrow"
    assert overlaps([mvc("not-wide", seq=1000, source_not="1", src="10.0.0.0/8"),
                     mvc("not-narrow", seq=1001, source_not="1", src="10.1.0.0/16")]) == {}


def test_a_negated_rule_does_not_cover_a_plain_one_and_the_reverse():
    """mutation: dropping the negation guard lets '!host' appear to cover a network that contains the host."""
    assert overlaps([mvc("not-host", seq=1000, source_not="1", src="10.20.30.5"), mvc("net", seq=1001, src="10.20.30.0/24")]) == {}
    assert overlaps([mvc("net", seq=1000, src="10.20.30.0/24"), mvc("not-host", seq=1001, source_not="1", src="10.20.30.5")]) == {}


def test_order_comes_from_sort_order_with_and_without_the_pf_ruleset():
    """mutation: sorting by sequence alone puts the interface rule (sequence 1) before the group rule."""
    for pf in (True, False):
        found = overlaps([
            mvc("plain", prio=400000, seq=1, interface="lan", src="10.20.30.7"),
            mvc("grouped", prio=300005, seq=900, interface="lan", src="10.20.30.0/24"),
        ], pf=pf)
        assert found["plain"].by.uuid == "grouped", pf


def test_equal_sort_order_is_decided_by_the_pf_line_numbers_not_the_uuid():
    """mutation: sorting ties by uuid turns the effective block rule into a 'sure' shadow of the pass rule."""
    rows = [mvc("zzz-narrow", seq=1000, src="10.20.30.0/24", dport="443", proto="tcp"),
            mvc("aaa-broad", seq=1000, action="block")]
    assert overlaps(rows) == {}   # pf evaluates zzz-narrow first; the broad rule is not covered by the narrow one
    rows.reverse()                # pf now evaluates the broad quick block first
    found = overlaps(rows)
    assert found["zzz-narrow"].by.uuid == "aaa-broad" and found["zzz-narrow"].confidence == "sure"


def test_equal_sort_order_without_pf_data_is_at_most_probable():
    rows = [mvc("aaa-broad", seq=1000, action="block"), mvc("zzz-narrow", seq=1000, src="10.20.30.0/24")]
    assert overlaps(rows, pf=False)["zzz-narrow"].confidence == "probable"


def test_a_pf_order_mismatch_or_guessed_order_caps_confidence():
    """mutation: removing ORDER_MISMATCH from the doubt set."""
    assert coverage.order_doubtful(["ORDER_MISMATCH"]) and coverage.order_doubtful(["ORDER_GUESSED", "X"])
    assert not coverage.order_doubtful(["PF_RULESET_UNAVAILABLE"]) and not coverage.order_doubtful([])
    ctx = build([mvc("a", seq=1000, src="10.20.30.0/24"), mvc("b", seq=1001, src="10.20.30.5")])
    rules, _ = normalize(ctx)
    assert coverage.analyse(rules, ctx, order_guessed=True)[0].confidence == "probable"


def test_an_unlabeled_pf_line_with_conditions_is_never_a_cover():
    """mutation: ignoring proto/port/... in the unlabeled line lets a 'tcp only' block shadow every protocol."""
    from app.services.rule_analysis import pf_ruleset
    from app.services.rule_analysis.normalize import antispoof_rule
    plain = antispoof_rule(pf_ruleset.parse_line(5, "block drop in quick inet from 10.20.30.0/24 to any"), (400000, 0))
    assert plain.extra_notes == []
    for text in ("block drop in quick inet proto tcp from 10.20.30.0/24 to any",
                 "block drop in quick inet from 10.20.30.0/24 port 53 to any",
                 "block drop in quick inet from 10.20.30.0/24 to any flags S/SA",
                 "block drop in quick inet from 10.20.30.0/24 to any user 0",
                 "block drop in quick inet from 10.20.30.0/24 to any tagged x",
                 "block drop in quick inet from 10.20.30.0/24 to any received-on vtnet0"):
        assert antispoof_rule(pf_ruleset.parse_line(6, text), (400000, 0)).extra_notes == ["UNMODELLED_CONDITION"], text
    victim = antispoof_rule(pf_ruleset.parse_line(7, "block drop in quick inet from 10.20.30.5 to any"), (400000, 1))
    victim.extra_notes, victim.interfaces = [], set()
    assert coverage.covers_fields(plain, victim) == "sure"
    conditional = antispoof_rule(pf_ruleset.parse_line(8, "block drop in quick inet proto tcp from 10.20.30.0/24 to any"), (400000, 0))
    assert coverage.covers_fields(conditional, victim) is None


def test_a_disabled_alias_is_an_empty_table_and_never_evaluated():
    """mutation: using the content of a disabled alias claims coverage by something pf does not have."""
    aliases = {"OFF": {"type": "host", "content": "10.20.30.0/24", "enabled": "0"},
               "ON": {"type": "host", "content": "10.20.30.0/24", "enabled": "1"}}
    rows = [mvc("a", seq=1000, src="OFF", dport="80", proto="tcp"), mvc("b", seq=1001, src="10.20.30.5", dport="80", proto="tcp")]
    assert overlaps(rows, aliases=aliases) == {}
    rows[0] = mvc("a", seq=1000, src="ON", dport="80", proto="tcp")
    assert overlaps(rows, aliases=aliases)["b"].confidence == "probable"


def test_aliases_same_name_sure_other_name_probable_unresolvable_never():
    aliases = {
        "WEB": {"type": "host", "content": "10.20.30.5\n10.20.30.6", "enabled": "1"},
        "WEB_ONE": {"type": "host", "content": "10.20.30.5", "enabled": "1"},
        "NESTED": {"type": "network", "content": "WEB_ONE", "enabled": "1"},
        "FEED": {"type": "urltable", "content": "https://example.invalid/list", "enabled": "1"},
    }
    rows = [
        mvc("by-name", seq=1000, src="WEB", dport="80", proto="tcp"),
        mvc("same", seq=1001, src="WEB", dport="80", proto="tcp"),
        mvc("static", seq=1002, src="WEB_ONE", dport="80", proto="tcp"),
        mvc("nested", seq=1003, src="NESTED", dport="80", proto="tcp"),
        mvc("table-a", seq=1004, src="FEED", dport="81", proto="tcp"),
        mvc("table-b", seq=1005, src="WEB_ONE", dport="81", proto="tcp"),
    ]
    found = overlaps(rows, aliases=aliases)
    assert (found["same"].kind, found["same"].confidence) == ("duplicate", "sure")
    assert (found["static"].kind, found["static"].confidence) == ("shadowed", "probable")
    assert found["nested"].confidence == "probable"
    assert "table-b" not in found  # FEED has no static content: nothing is claimed


def test_port_alias_comparison():
    aliases = {"P1": {"type": "port", "content": "80\n443", "enabled": "1"}, "P2": {"type": "port", "content": "443", "enabled": "1"}}
    rows = [mvc("a", seq=1000, proto="tcp", dport="P1"), mvc("b", seq=1001, proto="tcp", dport="P2"),
            mvc("c", seq=1002, proto="tcp", dport="P1")]
    found = overlaps(rows, aliases=aliases)
    assert (found["b"].kind, found["b"].confidence) == ("shadowed", "probable")
    assert (found["c"].kind, found["c"].confidence) == ("duplicate", "sure")


def test_without_the_loaded_pf_ruleset_nothing_is_sure():
    """What is not proven is never sure."""
    rows = [mvc("a", seq=1000, src="10.20.30.0/24"), mvc("b", seq=1001, src="10.20.30.5")]
    assert overlaps(rows, pf=False)["b"].confidence == "probable"
    assert overlaps(rows, pf=True)["b"].confidence == "sure"


def test_a_guessed_order_lowers_the_confidence():
    rows = [mvc("a", seq=1000, src="10.20.30.0/24"), mvc("b", seq=1001, src="10.20.30.5")]
    for row in rows:
        row.pop("sort_order")
    found = overlaps(rows)
    assert found["b"].confidence == "probable"


def test_the_unmodified_live_fixture_has_no_overlap_among_user_rules():
    ctx = build([])
    rules, _ = normalize(ctx)
    assert [o.rule.description for o in coverage.analyse(rules, ctx) if not o.rule.automatic] == []


# ── candidates ────────────────────────────────────────────────────────────

def hit(*, enabled=True, first_days=100, last_hit_days=None, sampled_hours=1):
    return SimpleNamespace(
        enabled=enabled, first_seen_at=NOW - timedelta(days=first_days),
        last_hit_at=None if last_hit_days is None else NOW - timedelta(days=last_hit_days),
        last_sampled_at=NOW - timedelta(hours=sampled_hours), last_packets=0, last_bytes=0)


def candidates(rows, *, hits=None, states=None, managed=None, days=30, **kw):
    ctx = build(rows, **kw)
    rules, notes = normalize(ctx)
    out = cleanup.build(rules, ctx, hits=hits or {}, states=states or {}, managed=managed or {}, now=NOW,
                        unused_days=days, order_guessed=coverage.order_doubtful(notes))
    return {c["uuid"]: c for c in out}


def kinds(candidate):
    return {c["kind"]: c["confidence"] for c in candidate["categories"]}


def test_unused_needs_the_whole_window_without_a_hit():
    rows = [mvc("quiet", seq=1000, proto="tcp", dport="1"), mvc("busy", seq=1001, proto="tcp", dport="2"),
            mvc("young", seq=1002, proto="tcp", dport="3"), mvc("never", seq=1003, proto="tcp", dport="4")]
    hits = {"quiet": hit(last_hit_days=60), "busy": hit(last_hit_days=3), "young": hit(first_days=5), "never": hit()}
    got = candidates(rows, hits=hits)
    assert set(got) == {"quiet", "never"}
    assert kinds(got["quiet"]) == {"unused": "sure"}


def test_the_threshold_is_a_parameter():
    rows = [mvc("r", seq=1000)]
    hits = {"r": hit(first_days=100, last_hit_days=45)}
    assert "r" not in candidates(rows, hits=hits, days=90)
    assert "r" in candidates(rows, hits=hits, days=30)
    assert "r" not in candidates(rows, hits={"r": hit(first_days=20)}, days=30)  # watched less than the window


def test_unused_is_never_sure_without_evidence_the_rule_can_be_hit():
    """mutation: dropping the loaded/schedule/freshness checks lets every unused rule through as sure."""
    rows = [mvc("plain", seq=1000, proto="tcp", dport="1"), mvc("timed", seq=1001, sched="work-hours", proto="tcp", dport="2"),
            mvc("unloaded", seq=1002, loaded=False, proto="tcp", dport="3"), mvc("stale", seq=1003, proto="tcp", dport="4")]
    hits = {u: hit() for u in ("plain", "timed", "unloaded")} | {"stale": hit(sampled_hours=72)}
    got = candidates(rows, hits=hits)
    assert kinds(got["plain"])["unused"] == "sure"
    assert kinds(got["timed"])["unused"] == "probable"
    assert kinds(got["unloaded"])["unused"] == "unknown"
    assert kinds(got["stale"])["unused"] == "unknown"
    without_pf = candidates(rows, hits=hits, pf=False)
    assert kinds(without_pf["plain"])["unused"] == "probable"


def test_a_rule_can_be_in_several_categories():
    rows = [mvc("broad", seq=1000, src="10.20.30.0/24"), mvc("narrow", seq=1001, src="10.20.30.5")]
    got = candidates(rows, hits={"narrow": hit()})
    assert kinds(got["narrow"]) == {"unused": "sure", "shadowed": "sure"}
    assert got["narrow"]["categories"][1]["by"]["uuid"] == "broad"


def test_disabled_for_a_long_time_comes_from_the_observed_history():
    rows = [mvc("old-off", seq=1000, enabled="0"), mvc("new-off", seq=1001, enabled="0"), mvc("never-seen", seq=1002, enabled="0")]
    states = {
        "old-off": SimpleNamespace(present=True, changed_at=NOW - timedelta(days=40)),
        "new-off": SimpleNamespace(present=True, changed_at=NOW - timedelta(days=3)),
    }
    got = candidates(rows, states=states)
    assert set(got) == {"old-off"} and kinds(got["old-off"]) == {"disabled_long": "sure"}


def test_disabled_rules_are_neither_unused_nor_shadowing_candidates_of_the_unused_kind():
    got = candidates([mvc("off", seq=1000, enabled="0")], hits={"off": hit(enabled=False)})
    assert got == {}


def test_not_removable_reasons():
    rows = [mvc("plain", seq=1000, proto="tcp", dport="1"), mvc("managed-one", seq=1001, proto="tcp", dport="2"),
            mvc("legacy-one", seq=1002, legacy=True, proto="tcp", dport="3")]
    hits = {u: hit() for u in ("plain", "managed-one", "legacy-one")}
    got = candidates(rows, hits=hits, managed={"managed-one": {"kind": "rule", "id": "x", "name": "Managed"}})
    assert (got["plain"]["removable"], got["plain"]["not_removable_reason"]) == (True, None)
    assert (got["managed-one"]["removable"], got["managed-one"]["not_removable_reason"]) == (False, "managed")
    assert got["managed-one"]["managed_by"]["name"] == "Managed"
    assert (got["legacy-one"]["removable"], got["legacy-one"]["not_removable_reason"]) == (False, "legacy")


def test_removal_block_for_automatic_rules():
    ctx = build([])
    rules, _ = normalize(ctx)
    automatic = next(r for r in rules if r.automatic and r.section != "antispoof")
    assert cleanup.removal_block(automatic, None) == "automatic"
    assert cleanup.removal_block(automatic, {"kind": "rule"}) == "automatic"  # automatic wins


def test_automatic_rules_never_appear_as_candidates():
    got = candidates([], hits={})
    assert got == {}


def test_candidates_are_in_evaluation_order_and_counts_add_up():
    rows = [mvc("b", seq=1001, proto="tcp", dport="2"), mvc("a", seq=1000, proto="tcp", dport="1")]
    hits = {"a": hit(), "b": hit()}
    ctx = build(rows)
    rules, _ = normalize(ctx)
    out = cleanup.build(rules, ctx, hits=hits, states={}, managed={}, now=NOW)
    assert [c["uuid"] for c in out] == ["a", "b"]
    assert cleanup.summarize(out)["unused"] == 2


def test_the_threshold_is_clamped():
    ctx = build([mvc("r", seq=1000)])
    rules, _ = normalize(ctx)
    assert cleanup.build(rules, ctx, hits={"r": hit(first_days=100)}, states={}, managed={}, now=NOW, unused_days=0)  # 0 -> 1 day
