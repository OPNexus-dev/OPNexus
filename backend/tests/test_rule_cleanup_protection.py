"""Pure parts of the rule cleanup (1.31): engine.match_rule, the management-access protection, the infrastructure
exclusion, the confidence caps and the cluster usage merge. No database, no firewall."""
import ipaddress
from datetime import datetime, timedelta, timezone

from app.services import rule_hit_service
from app.services.rule_analysis import cleanup, protection
from app.services.rule_analysis.context import Context, IfInfo
from app.services.rule_analysis.engine import Query, match_rule
from app.services.rule_analysis.model import Endpoint, NormRule
from app.services.rule_cleanup_service import selectable_level

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
ip = ipaddress.ip_address
net = ipaddress.ip_network


def ctx(**kw) -> Context:
    interfaces = {
        "wan": IfInfo("wan", "vtnet0", "WAN", nets=[net("203.0.113.0/24")], addrs=[ip("203.0.113.57")], has_gateway=True),
        "lan": IfInfo("lan", "vtnet1", "LAN", nets=[net("10.20.30.0/24")], addrs=[ip("10.20.30.2")], vips=[ip("10.20.30.1")]),
    }
    return Context(rows=[], pf=None, interfaces=interfaces, groups={}, aliases=kw.get("aliases", {}), port_forwards=[])


def nrule(*, src="", dst="", dport="", protos=frozenset({"6"}), interfaces=frozenset({"wan"}), action="pass", negated=False,
          tagged="", uuid="u1") -> NormRule:
    items = [i for i in src.split(",") if i]
    return NormRule(uuid=uuid, label=uuid, description=uuid, order=(400000, 1), order_known=True, section="interface",
                    enabled=True, action=action, quick=True, interfaces=set(interfaces), if_not=False, direction="in",
                    af={4}, protos=None if protos is None else set(protos), src=Endpoint(raw=src, negated=negated, items=items),
                    dst=Endpoint(raw=dst, items=[i for i in dst.split(",") if i]), sport="", dport=dport, tagged=tagged)


SOURCES = [net("203.0.113.10/32")]


def check(rule, c=None, sources=SOURCES):
    c = c or ctx()
    targets, ports = protection.management_targets("https://203.0.113.57:4443", c)
    return protection.management_check(rule, c, sources, targets, ports)


def test_targets_are_the_api_address_and_port_plus_every_firewall_address():
    targets, ports = protection.management_targets("https://203.0.113.57:4443", ctx())
    assert targets[0] == ip("203.0.113.57") and ip("10.20.30.1") in targets and ip("127.0.0.1") not in targets
    assert ports == [4443, 443, 80, 22]


def test_a_rule_for_the_management_path_is_protected():
    assert check(nrule(src="203.0.113.0/24", dport="443"))["result"] == "true"
    assert check(nrule(src="", dport="4443"))["result"] == "true"           # any source, API port
    assert check(nrule(src="203.0.113.10", dport="", protos=None))["result"] == "true"


def test_only_a_proven_non_match_is_removable():
    assert check(nrule(src="10.20.30.0/24", dport="443"))["result"] == "false"   # other source
    assert check(nrule(src="203.0.113.0/24", dport="8081"))["result"] == "false"  # other port
    assert check(nrule(src="203.0.113.0/24", dport="443", protos={"17"}))["result"] == "false"  # udp


def test_unknown_is_protected():  # case 18
    unknown = check(nrule(src="tbl", dport="443"), ctx(aliases={"tbl": {"name": "tbl", "type": "urltable"}}))
    assert unknown["result"] == "unknown" and unknown["match"]["port"] in (4443, 443)
    tagged = check(nrule(src="", dport="443", tagged="mark"))
    assert tagged["result"] == "unknown" and "TAGGED" in tagged["reasons"]
    q = Query(src=ip("203.0.113.10"), dst=ip("203.0.113.57"), proto="6", proto_label="tcp", dport=443)
    assert match_rule(nrule(interfaces={"nosuchgroup"}), q, ctx(), iface="wan").value is None


def test_network_sources_are_judged_for_every_address():
    small = [net("203.0.113.0/28")]
    assert check(nrule(src="203.0.113.14", dport="443"), sources=small)["result"] == "true"   # one address inside
    assert check(nrule(src="203.0.113.20", dport="443"), sources=small)["result"] == "false"
    big = [net("10.0.0.0/8")]
    assert check(nrule(src="10.20.30.0/24", dport="443"), sources=big)["result"] == "true"    # ranges overlap
    assert check(nrule(src="192.0.2.0/24", dport="443"), sources=big)["result"] == "false"
    assert check(nrule(src="lan", dport="443"), sources=big)["result"] == "true"              # interface network
    assert check(nrule(src="10.0.0.0/8", dport="443", negated=True), sources=big)["result"] == "false"  # !whole net
    assert check(nrule(src="tbl", dport="443"), ctx(aliases={"tbl": {"type": "urltable"}}), sources=big)["result"] == "unknown"


def test_no_query_possible_is_protected():
    got = check(nrule(src="", dport="443"), sources=[net("2001:db8::/64")])  # no IPv6 address on the firewall
    assert got["result"] == "unknown" and got["reasons"] == ["NO_MANAGEMENT_QUERY"]


def test_carp_and_pfsync_rules_are_infrastructure():
    c = ctx()
    assert protection.infrastructure_reason(nrule(protos={"112"}), c, hasync=None, clustered=False) == "infrastructure"
    assert protection.infrastructure_reason(nrule(protos={"240"}), c, hasync=None, clustered=False) == "infrastructure"
    lan_any = nrule(src="10.20.30.0/24", protos=None, interfaces={"lan"})
    assert protection.infrastructure_reason(lan_any, c, hasync=None, clustered=False) == "infrastructure"  # CARP adverts
    lan_tcp = nrule(src="10.20.30.0/24", dport="8081", interfaces={"lan"})
    assert protection.infrastructure_reason(lan_tcp, c, hasync=None, clustered=True) is None
    wan_any = nrule(src="", protos=None, interfaces={"wan"})
    off = {"hasync": {"pfsyncinterface": {"": {"selected": 1}, "wan": {"selected": 0}}, "pfsyncpeerip": ""}}
    on = {"hasync": {"pfsyncinterface": {"": {"selected": 0}, "wan": {"selected": 1}}, "pfsyncpeerip": "203.0.113.58"}}
    assert protection.infrastructure_reason(wan_any, c, hasync=off, clustered=True) is None
    assert protection.infrastructure_reason(wan_any, c, hasync=on, clustered=True) == "infrastructure"
    # HA settings not readable on a cluster node: every interface counts as a possible pfsync interface
    assert protection.infrastructure_reason(wan_any, c, hasync=None, clustered=True) == "infrastructure"


def test_confidence_caps():
    rule = nrule()
    unused = {"categories": [{"kind": "unused", "confidence": "sure"}]}
    assert selectable_level(unused, rule, unused_days=30, external=False)[0] == "sure"
    assert selectable_level(unused, rule, unused_days=29, external=False)[0] == "probable"
    assert selectable_level(unused, nrule(action="block"), unused_days=90, external=False)[0] == "probable"
    assert selectable_level(unused, nrule(action="reject"), unused_days=90, external=False)[0] == "probable"
    assert selectable_level(unused, rule, unused_days=90, external=True)[0] == "probable"
    both = {"categories": [{"kind": "unused", "confidence": "unknown"}, {"kind": "duplicate", "confidence": "sure"}]}
    assert selectable_level(both, nrule(action="block"), unused_days=7, external=False)[0] == "sure"  # best category
    assert selectable_level({"categories": [{"kind": "shadowed", "confidence": "unknown"}]}, rule, unused_days=30, external=False)[0] == "unknown"
    assert selectable_level({"categories": [{"kind": "disabled_long", "confidence": "sure"}]}, rule, unused_days=30, external=False)[0] is None


class Hit:
    def __init__(self, uuid, first_days, hit_days=None, sampled_minutes=10, enabled=True):
        self.rule_uuid, self.enabled = uuid, enabled
        self.first_seen_at = NOW - timedelta(days=first_days)
        self.last_hit_at = None if hit_days is None else NOW - timedelta(days=hit_days)
        self.last_sampled_at = NOW - timedelta(minutes=sampled_minutes)


def test_cluster_usage_merge():  # case 16
    master = {"r": Hit("r", 100, hit_days=1)}
    backup = {"r": Hit("r", 100)}
    merged = rule_hit_service.merge_cluster_hits(backup, [master])
    assert rule_hit_service.classify(merged["r"], NOW, 30) == "used"
    both_idle = rule_hit_service.merge_cluster_hits({"r": Hit("r", 100)}, [{"r": Hit("r", 20)}])
    assert rule_hit_service.classify(both_idle["r"], NOW, 30) == "collecting"  # window = latest start
    stale = rule_hit_service.merge_cluster_hits({"r": Hit("r", 100)}, [{"r": Hit("r", 100, sampled_minutes=60 * 48)}])
    assert stale["r"].last_sampled_at == NOW - timedelta(hours=48)  # as fresh as the oldest node
    missing = rule_hit_service.merge_cluster_hits({"r": Hit("r", 100)}, [{}])
    assert missing["r"].peer_missing and rule_hit_service.classify(missing["r"], NOW, 30) == "unused"
    rule = nrule()
    rule.loaded = True
    assert cleanup.unused_confidence(rule, missing["r"], NOW) == "probable"
    assert cleanup.unused_confidence(rule, both_idle["r"], NOW) == "sure"


def _live_rule(**kw):
    """A rule normalized inside the live fixture (tests/fixtures/rule_analysis_fw.json: built-in groups wireguard,
    openvpn, enc0 with members "" and unassigned devices without identifier)."""
    from app.services.rule_analysis.normalize import normalize
    from tests.test_rule_analysis import build, mvc
    row = mvc("bbbbbbbb-0000-4000-8000-000000000001", seq=900, proto="tcp", **kw)
    c = build([row])
    rules, _ = normalize(c)
    return next(r for r in rules if r.uuid == row["uuid"]), c


def test_rules_on_builtin_groups_without_known_members_are_protected():  # review finding 1
    for group in ("wireguard", "openvpn", "enc0"):
        rule, c = _live_rule(interface=group, src="203.0.113.10", dst="(self)", dport="443")
        got = check(rule, c)
        assert got["result"] == "unknown" and "IFACE_MEMBERS_UNKNOWN" in got["reasons"], (group, got)
        assert got["match"]["interface"] == protection.UNASSIGNED
    # the virtual ingress does not make every rule protected
    rule, c = _live_rule(interface="wireguard", src="203.0.113.10", dst="(self)", dport="8081")
    assert check(rule, c)["result"] == "false"
    rule, c = _live_rule(interface="lan", src="10.20.30.0/24", dst="(self)", dport="443")
    assert check(rule, c)["result"] == "false"
    # floating "any interface" and "not lan" also match a packet on wg0
    rule, c = _live_rule(interface="", src="203.0.113.10", dst="(self)", dport="22")
    assert check(rule, c)["result"] == "true"
    rule, c = _live_rule(interface="lan", interfacenot="1", src="203.0.113.10", dst="(self)", dport="22")
    assert check(rule, c)["result"] == "true"


def test_unassigned_interface_outcome():
    c = ctx()
    c.groups = {"mygroup": {"lan"}, "wireguard": set()}
    assert protection.unassigned_interface(nrule(interfaces={"lan"}), c).value is False
    assert protection.unassigned_interface(nrule(interfaces={"mygroup"}), c).value is False
    assert protection.unassigned_interface(nrule(interfaces={"wireguard"}), c).value is None
    assert protection.unassigned_interface(nrule(interfaces={"nosuch"}), c).value is None
    assert protection.unassigned_interface(nrule(interfaces=set()), c).value is True


def test_redacted_plan_names_no_source_port_or_firewall_address():  # review finding 8
    from app.services.rule_cleanup_service import redact
    plan = {"kind": "enable", "sources": ["203.0.113.10/32"], "ports": [2222], "nodes": [
        {"host_id": "h", "base_url": "https://203.0.113.57", "items": [
            {"uuid": "u", "protection": {"result": "true", "queries": 3, "reasons": [],
                                         "match": {"source": "203.0.113.10/32", "destination": "203.0.113.57"}}}]}]}
    out = redact({"plan": plan, "operation": {"plan": plan}})
    for p in (out["plan"], out["operation"]["plan"]):
        assert "203.0.113" not in str(p) and "2222" not in str(p)
        assert p["sources_configured"] is True and p["nodes"][0]["items"][0]["protection"]["result"] == "true"
    assert plan["sources"] and plan["nodes"][0]["base_url"]  # the stored plan itself is untouched
