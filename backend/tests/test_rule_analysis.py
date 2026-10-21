"""Rule analysis (1.26): normalization, resolver and pf semantics of the engine.

Pure functions against an anonymized live fixture (OPNsense 26.7: automatic rules, antispoof
lines, a CARP VIP on LAN). Synthetic rules get pf lines in sort order, as pf would load them.
The numbers in the test names follow the test matrix of the design (section 8).
"""
import copy
import ipaddress
import json
from pathlib import Path

import pytest

from app.services.rule_analysis import context as ctx_mod
from app.services.rule_analysis import pf_ruleset, resolve
from app.services.rule_analysis.engine import evaluate
from app.services.rule_analysis.normalize import normalize
from app.services.rule_analysis.service import build_query

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "rule_analysis_fw.json").read_text())
DEVICES = {"lan": "vtnet1_vlan10", "wan": "vtnet0"}
LAN_CLIENT, INTERNET, WAN_IP = "10.20.30.50", "198.51.100.10", "203.0.113.57"


def mvc(uuid, *, prio=400000, seq=1, action="pass", quick="1", interface="lan", src="any", dst="any",
        proto="any", ipprotocol="inet", dport="", sport="", direction="in", loaded=True, **extra):
    row = {
        "uuid": uuid, "sort_order": f"{prio:06d}.{seq:07d}", "prio_group": str(prio), "sequence": str(seq),
        "enabled": "1", "action": action, "quick": quick, "interface": interface, "interfacenot": "0",
        "direction": direction, "ipprotocol": ipprotocol, "protocol": proto, "source_net": src, "source_not": "0",
        "source_port": sport, "destination_net": dst, "destination_not": "0", "destination_port": dport,
        "gateway": "", "sched": "", "statetype": "keep", "tagged": "", "description": uuid,
    }
    row.update(extra)
    row["_loaded"] = loaded
    return row


def build(extra_rows=(), *, drop=(), aliases=None, refs=None, groups=None, pf=True, port_forwards=()):
    """A Context from the fixture plus synthetic MVC rules. pf lines of MVC rules are generated
    in sort order (pf numbers follow the evaluation order like on a real firewall)."""
    data = copy.deepcopy(FIXTURE)
    rows = [r for r in data["rules"]["rows"] if r["description"] not in drop] + [dict(r) for r in extra_rows]
    interfaces = ctx_mod._interfaces(data["interfaces"])
    group_rows = data["groups"]["rows"] + [{"ifname": k, "members": ",".join(v)} for k, v in (groups or {}).items()]
    stats = None
    if pf:
        auto = {k: v for k, v in data["pf"]["rules"]["filter rules"].items()
                if not any(r.get("uuid") in k for r in data["rules"]["rows"] if not r.get("is_automatic"))}
        lines = sorted(((int(k.split()[0][1:]), k.split(" ", 1)[1]) for k in auto), key=lambda x: x[0])
        number = max(n for n, _ in lines) + 1
        mvc_rows = sorted((r for r in rows if not r.get("is_automatic") and r.get("_loaded", True)
                           and r.get("enabled") == "1"), key=lambda r: r.get("sort_order") or "400000." + r["sequence"].zfill(7))
        for r in mvc_rows:
            words = ["block" if r["action"] == "block" else ("block return" if r["action"] == "reject" else "pass"),
                     r["direction"] if r["direction"] != "any" else ""]
            if r["quick"] == "1":
                words.append("quick")
            ifs = [i for i in str(r["interface"]).split(",") if i]
            if len(ifs) == 1 and ifs[0] in DEVICES:
                words.append(f"on {DEVICES[ifs[0]]}")
            if r["ipprotocol"] in ("inet", "inet6"):
                words.append(r["ipprotocol"])
            words.append(f'all label "{r["uuid"]}"')
            lines.append((number, " ".join(w for w in words if w)))
            number += 1
        stats = {"rules": {"filter rules": {f"@{n} {t}": {} for n, t in lines}}}
    for r in rows:
        r.pop("_loaded", None)
    alias_rows = {a["name"]: a for a in data["aliases"]["rows"]}
    alias_rows.update({name: {"name": name, **a} for name, a in (aliases or {}).items()})
    ctx = ctx_mod.Context(rows=rows, pf=pf_ruleset.parse(stats) if pf else None, interfaces=interfaces,
                          groups=ctx_mod._groups({"rows": group_rows}, interfaces), aliases=alias_rows,
                          port_forwards=list(port_forwards), notes=[] if pf else ["PF_RULESET_UNAVAILABLE"])
    default_refs = {LAN_CLIENT: ["__lan_network"], INTERNET: [], WAN_IP: ["__wan_network"]}
    ctx.refs = {**default_refs, **(refs or {})}
    return ctx


def run(ctx, src=LAN_CLIENT, dst=INTERNET, proto="tcp", dport="443", iface="lan", **kw):
    q = build_query(src=src, dst=dst, proto=proto, dport=dport, iface=iface, **kw)
    for ip in (str(q.src), str(q.dst)):
        ctx.refs.setdefault(ip, [])
    return evaluate(ctx, q)


def decided_by(result):
    return result["decisive_rule"]["description"] if result["decisive_rule"] else None


# ── Normalization ─────────────────────────────────────────────────────────

def test_normalize_live_fixture_uses_pf_for_automatic_rules():
    rules, notes = normalize(build())
    by_desc = {}
    for r in rules:
        by_desc.setdefault(r.description, r)
    assert notes == []
    deny = by_desc["Default deny / state violation rule"]
    assert deny.quick is False and deny.action == "block" and deny.loaded and deny.pf_numbers
    dhcp = by_desc["allow access to DHCP server"]
    assert dhcp.action == "pass" and dhcp.quick is True and dhcp.interfaces == {"lan"}  # "LAN" + empty action
    lockout = by_desc["anti-lockout rule"]
    assert lockout.interfaces == {"lan"} and lockout.automatic and lockout.legacy
    allow = by_desc["Default allow LAN to any rule"]
    assert allow.section == "interface" and allow.quick and allow.af == {4} and not allow.automatic
    spoof = [r for r in rules if r.section == "antispoof"]
    assert spoof and spoof[0].interfaces == {"lan"} and spoof[0].if_not and spoof[0].quick is False
    # Antispoof lines sit before the first automatic rule, like in pf.
    assert rules.index(spoof[0]) < rules.index(deny)


def test_normalize_reports_order_mismatch_and_missing_sort_order():
    ctx = build([mvc("r-a", seq=1)])
    # Swap pf positions: the rule now appears in pf before the system rules.
    key = next(k for k in ctx.pf.by_label if k == "r-a")
    ctx.pf.by_label[key][0].number = -1
    assert "ORDER_MISMATCH" in normalize(ctx)[1]
    row = mvc("r-b")
    row.pop("sort_order")
    assert "ORDER_GUESSED" in normalize(build([row]))[1]


def test_unmigrated_legacy_rule_with_interface_description():
    # Seen live: legacy user rules have a 32-hex label and "WAN" instead of "wan".
    legacy = mvc("0123456789abcdef0123456789abcdef", seq=1, action="block", interface="WAN", legacy=True)
    r = run(build([legacy], drop=("Allow ALL",)), src=INTERNET, dst=WAN_IP, iface="wan")
    assert decided_by(r) == legacy["uuid"] and r["confidence"] == "sure"
    assert r["decisive_rule"]["legacy"] and not r["decisive_rule"]["editable"]


# ── Engine: matrix 1-8, 14-20 ─────────────────────────────────────────────

def test_01_lan_client_to_internet_is_default_allow_sure():
    r = run(build())
    assert r["action"] == "pass" and decided_by(r) == "Default allow LAN to any rule"
    assert r["confidence"] == "sure" and r["rule_confidence"] == "sure" and r["unknowns"] == []


def test_02_wan_to_firewall_ssh_hits_default_deny():
    r = run(build(drop=("Allow ALL",)), src=INTERNET, dst=WAN_IP, dport="22", iface="wan")
    assert r["action"] == "block" and decided_by(r) == "Default deny / state violation rule"
    assert r["confidence"] == "sure" and r["decisive_rule"]["quick"] is False


def test_03_floating_non_quick_block_loses_to_interface_quick_pass():
    ctx = build([mvc("float-block", prio=200000, action="block", quick="0", interface="")])
    r = run(ctx)
    assert r["action"] == "pass" and decided_by(r) == "Default allow LAN to any rule"
    effects = {s["rule"]["description"]: s["effect"] for s in r["trace"][0]["steps"]}
    assert effects["float-block"] == "overridden"


def test_04_floating_quick_block_on_lan_and_wan_wins():
    r = run(build([mvc("float-quick", prio=200000, action="block", quick="1", interface="lan,wan")]))
    assert r["action"] == "block" and decided_by(r) == "float-quick" and r["confidence"] == "sure"
    assert r["decisive_rule"]["section"] == "floating"


def test_05_floating_interfacenot_applies_to_the_other_interfaces():
    ctx = build([mvc("not-lan", prio=200000, action="block", interface="lan", interfacenot="1")])
    assert decided_by(run(ctx)) == "Default allow LAN to any rule"
    wan = run(ctx, src=INTERNET, dst=WAN_IP, dport="22", iface="wan")
    assert wan["action"] == "block" and decided_by(wan) == "not-lan"


def test_06_group_rule_comes_before_interface_rules():
    ctx = build([mvc("grp-block", prio=300005, seq=900, action="block", interface="office")], groups={"office": ["lan"]})
    r = run(ctx)
    assert r["action"] == "block" and decided_by(r) == "grp-block" and r["decisive_rule"]["section"] == "group"


def test_07_last_non_quick_match_wins():
    ctx = build([mvc("first-pass", seq=1, quick="0", interface="wan"), mvc("then-block", seq=2, action="block", quick="0", interface="wan")],
                drop=("Allow ALL",))
    r = run(ctx, src=INTERNET, dst=WAN_IP, iface="wan")
    assert r["action"] == "block" and decided_by(r) == "then-block" and r["confidence"] == "sure"


def test_08_negated_source_alias():
    aliases = {"BADHOSTS": {"type": "host", "content": "10.20.30.66"}}
    rule = mvc("not-bad", seq=1, action="block", src="BADHOSTS", source_not="1")
    ctx = build([rule], aliases=aliases, refs={LAN_CLIENT: ["__lan_network"], "10.20.30.66": ["BADHOSTS", "__lan_network"]})
    assert run(ctx)["action"] == "block"                       # .50 is not in BADHOSTS -> !BADHOSTS matches
    assert run(ctx, src="10.20.30.66")["action"] == "pass"     # in BADHOSTS -> negation does not match
    # Table membership unknown: the negation stays unknown, the verdict is not "sure".
    unknown = build([dict(rule, source_net="FEED")], aliases={"FEED": {"type": "urltable", "content": ""}},
                    refs={LAN_CLIENT: None})
    r = run(unknown)
    assert r["action"] == "pass" and r["confidence"] == "unknown" and "ALIAS_TABLE_UNAVAILABLE" in r["unknowns"]


def test_14_rule_not_loaded_is_skipped():
    ctx = build([mvc("scheduled", seq=1, action="block", sched="weekends", loaded=False)])
    r = run(ctx)
    assert decided_by(r) == "Default allow LAN to any rule"
    step = next(s for s in r["trace"][0]["steps"] if s["rule"]["description"] == "scheduled")
    assert step["effect"] == "not_loaded" and "RULES_NOT_LOADED" in r["notes"]


def test_15_16_gateway_and_stateless_notes():
    r = run(build([mvc("via-gw", seq=1, gateway="WAN_GW", statetype="none")]))
    assert decided_by(r) == "via-gw" and {"POLICY_ROUTE", "STATELESS"} <= set(r["notes"])
    assert r["confidence"] == "sure"


def test_17_tagged_rule_is_never_sure():
    r = run(build([mvc("tagged-block", seq=1, action="block", tagged="FROM_NAT")]))
    assert r["confidence"] == "unknown" and "TAGGED" in r["unknowns"]


def test_18_unknown_rule_with_same_action_keeps_the_verdict_sure():
    ctx = build([mvc("maybe-pass", seq=1, src="GHOST")])  # alias unknown on the firewall
    r = run(ctx)
    assert r["action"] == "pass" and r["confidence"] == "sure" and r["rule_confidence"] == "probable"


def test_19_unknown_rule_with_other_action_makes_the_verdict_unknown():
    ctx = build([mvc("maybe-block", seq=1, action="block", src="GHOST")])
    r = run(ctx)
    assert r["action"] == "pass" and r["confidence"] == "unknown" and "ALIAS_MISSING" in r["unknowns"]
    assert any(s["effect"] == "possible" for s in r["trace"][0]["steps"])


def test_20_rule_with_missing_interface_never_matches():
    row = mvc("orphan", prio=600000, action="pass", interface="opt9", loaded=False)
    r = run(build([row], drop=("Allow ALL",)), src=INTERNET, dst=WAN_IP, iface="wan")
    assert decided_by(r) == "Default deny / state violation rule"
    assert next(s for s in r["trace"][0]["steps"] if s["rule"]["description"] == "orphan")["effect"] == "not_loaded"
    # Even without a pf ruleset a 600000 rule counts as not loaded; the verdict is no longer sure.
    no_pf = run(build([row], pf=False))
    assert decided_by(no_pf) == "Default allow LAN to any rule" and no_pf["confidence"] != "sure"


def test_guessed_interface_and_nat_nearby_lower_confidence():
    r = run(build(), iface=None)
    assert r["legs"][0]["interface"] == "lan" and "IFACE_GUESSED" in r["notes"] and r["confidence"] == "probable"
    nat = run(build(port_forwards=[{"interface": "lan", "protocol": "tcp", "destination.port": "443"}]))
    assert "NAT_NEARBY" in nat["notes"] and nat["confidence"] == "probable"
    # "no rdr" exclusions and forwards for other ports do not count.
    other = run(build(port_forwards=[{"interface": "lan", "protocol": "tcp", "destination.port": "22", "nordr": "0"},
                                     {"interface": "lan", "protocol": "tcp", "destination.port": "443", "nordr": "1"}]))
    assert "NAT_NEARBY" not in other["notes"] and other["confidence"] == "sure"


def test_egress_leg_blocks_when_out_rule_blocks():
    ctx = build([mvc("no-out", prio=200000, action="block", interface="wan", direction="out")])
    r = run(ctx, egress="wan")
    assert [leg["action"] for leg in r["legs"]] == ["pass", "block"]
    assert r["action"] == "block" and decided_by(r) == "no-out" and r["confidence"] == "sure"
    # Without the block the automatic "let out anything" rule passes the out leg.
    ok = run(build(), egress="wan")
    assert ok["action"] == "pass" and ok["legs"][1]["decisive_rule"]["description"] == "let out anything from firewall host itself"


# ── Resolver: matrix 9-13 ─────────────────────────────────────────────────

def _ip(text):
    return ipaddress.ip_address(text)


def test_09_nested_aliases_and_cycles():
    ctx = build(aliases={
        "OUTER": {"type": "host", "content": "MIDDLE\n192.0.2.9"},
        "MIDDLE": {"type": "network", "content": "10.20.30.0/25\nINNER"},
        "INNER": {"type": "host", "content": "10.20.30.200"},
        "LOOP_A": {"type": "host", "content": "LOOP_B"}, "LOOP_B": {"type": "host", "content": "LOOP_A"},
        "NAMED": {"type": "host", "content": "server.example.org"},
    })
    ctx.refs = {}
    assert resolve.alias_contains("OUTER", _ip("10.20.30.5"), ctx).value is True
    assert resolve.alias_contains("OUTER", _ip("10.20.30.200"), ctx).value is True
    assert resolve.alias_contains("OUTER", _ip("10.20.30.150"), ctx).value is False
    loop = resolve.alias_contains("LOOP_A", _ip("10.20.30.5"), ctx)
    assert loop.value is None and "ALIAS_CYCLE" in loop.reasons
    fqdn = resolve.alias_contains("NAMED", _ip("10.20.30.5"), ctx)
    assert fqdn.value is None and "FQDN_UNRESOLVED" in fqdn.reasons
    assert resolve.alias_contains("NOPE", _ip("10.20.30.5"), ctx).value is None


def test_10_table_aliases_need_find_references():
    ctx = build(aliases={"FEED": {"type": "urltable", "content": "https://example.org/list"}})
    ctx.refs = {"192.0.2.7": ["FEED"], "192.0.2.8": []}
    assert resolve.alias_contains("FEED", _ip("192.0.2.7"), ctx).value is True
    assert resolve.alias_contains("FEED", _ip("192.0.2.8"), ctx).value is False
    ctx.refs = {"192.0.2.7": None}
    out = resolve.alias_contains("FEED", _ip("192.0.2.7"), ctx)
    assert out.value is None and out.reasons == ["ALIAS_TABLE_UNAVAILABLE"]


def test_11_address_family():
    ctx = build([mvc("v4-only", seq=1, action="block", interface="wan", ipprotocol="inet")], drop=("Allow ALL",))
    v6 = run(ctx, src="2001:db8::5", dst="2001:db8:1::1", iface="wan")
    assert decided_by(v6) == "Default deny / state violation rule"
    both = build([mvc("v46", seq=1, action="reject", interface="wan", ipprotocol="inet46")], drop=("Allow ALL",))
    assert decided_by(run(both, src="2001:db8::5", dst="2001:db8:1::1", iface="wan")) == "v46"


def test_12_port_ranges_protocol_pairs_and_nested_port_aliases():
    ctx = build(aliases={"WEB": {"type": "port", "content": "1000:2000\nMORE"}, "MORE": {"type": "port", "content": "https\n8000-8001"}})
    for port, expected in ((999, False), (1000, True), (2000, True), (2001, False), (443, True), (8001, True), (8002, False)):
        assert resolve.port_match("WEB", port, ctx).value is expected, port
    assert resolve.port_match("1000:2000", 1500, ctx).value is True
    rule = mvc("tcpudp", seq=1, action="block", proto="TCP/UDP", dport="1000-2000")
    assert decided_by(run(build([rule]), proto="udp", dport="1000")) == "tcpudp"
    assert decided_by(run(build([rule]), proto="tcp", dport="2001")) == "Default allow LAN to any rule"
    # No source port given: assumed ephemeral, so "port 0" rules do not match, a narrow one is unknown.
    assert resolve.port_match("0", None, ctx, ephemeral_default=True).value is False
    assert resolve.port_match("5000", None, ctx, ephemeral_default=True).value is None
    assert resolve.port_match("1024-65535", None, ctx, ephemeral_default=True).value is True


def test_13_interface_networks_addresses_self_and_carp_vip():
    ctx = build()
    vip, primary, client = _ip("10.20.30.1"), _ip("10.20.30.2"), _ip("10.20.30.50")
    assert resolve.addr_item("lan", vip, ctx).value is True and resolve.addr_item("lan", client, ctx).value is True
    assert resolve.addr_item("lanip", primary, ctx).value is True
    assert resolve.addr_item("lanip", client, ctx).value is False
    assert resolve.addr_item("lanip", vip, ctx).value is None          # VIP vs interface address: not provable
    assert resolve.addr_item("(self)", vip, ctx).value is True
    assert resolve.addr_item("(self)", _ip(WAN_IP), ctx).value is True
    assert resolve.addr_item("(self)", client, ctx).value is False
    assert resolve.addr_item("__wan_network", _ip("203.0.113.9"), ctx).value is True


def test_query_validation_errors():
    from fastapi import HTTPException
    for kwargs, code in (({"src": "host.example", "dst": INTERNET}, "ANALYSE_ADRESSE_UNGUELTIG"),
                         ({"src": "2001:db8::1", "dst": INTERNET}, "ANALYSE_FAMILIEN_MISCHUNG"),
                         ({"src": LAN_CLIENT, "dst": INTERNET, "dport": "70000"}, "ANALYSE_PARAMETER_UNGUELTIG"),
                         ({"src": LAN_CLIENT, "dst": INTERNET, "proto": "icmp", "dport": "80"}, "ANALYSE_PARAMETER_UNGUELTIG")):
        with pytest.raises(HTTPException) as exc:
            build_query(**kwargs)
        assert exc.value.detail["code"] == code
