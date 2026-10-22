"""A small stateful OPNsense for the rule-cleanup tests (respx side effects).

It keeps its rules in memory and answers searchRule, getRule, toggleRule, delRule, addRule, moveRuleBefore, apply,
ruleStats, pfStatistics (generated from the enabled rules, like pf after an apply), interfacesInfo, groups, aliases,
port forwards, getVipStatus and core/hasync/get in the shapes seen live (OPNsense 26.7, see opnsense_client.py).
``fail`` injects errors per call: {("toggle", uuid): httpx.ReadTimeout | httpx.Response | callable}.
Addresses: documentation/private ranges only.
"""
from __future__ import annotations

import copy
import re
import uuid as uuid_lib

import httpx

R_BASE = "aaaaaaaa-0000-4000-8000-000000000100"
R_DUP = "aaaaaaaa-0000-4000-8000-000000000200"
R_UNUSED = "aaaaaaaa-0000-4000-8000-000000000300"
R_BLOCK = "aaaaaaaa-0000-4000-8000-000000000400"
R_MGMT = "aaaaaaaa-0000-4000-8000-000000000500"
R_CARP = "aaaaaaaa-0000-4000-8000-000000000600"
R_ALIAS = "aaaaaaaa-0000-4000-8000-000000000700"
R_TAIL = "aaaaaaaa-0000-4000-8000-000000000800"
R_LEGACY = "0123456789abcdef0123456789abcdef"
AUTO = "02f4bab031b57d1e30553ce08e0ec131"
MGMT_SOURCE = "203.0.113.10"
DEVICES = {"wan": "vtnet0", "lan": "vtnet1"}


def rule(uuid, seq, *, action="pass", interface="lan", proto="TCP", src="any", dst="any", dport="", enabled="1",
         description=None, **extra) -> dict:
    row = {
        "uuid": uuid, "sort_order": f"400000.{seq:07d}", "prio_group": "400000", "sequence": str(seq),
        "enabled": enabled, "action": action, "quick": "1", "interface": interface, "interfacenot": "0",
        "direction": "in", "ipprotocol": "inet", "protocol": proto, "source_net": src, "source_not": "0",
        "source_port": "", "destination_net": dst, "destination_not": "0", "destination_port": dport,
        "gateway": "", "sched": "", "statetype": "keep", "tagged": "", "log": "0", "categories": "",
        "description": description or f"cleanup-{seq}",
    }
    row.update(extra)
    return row


def default_rules() -> list[dict]:
    return [
        {"uuid": AUTO, "sort_order": "000001.1000001", "legacy": True, "is_automatic": True, "enabled": "1",
         "action": "block", "quick": False, "direction": "in", "ipprotocol": "inet46", "protocol": "",
         "source_net": "", "source_port": "", "destination_net": "", "destination_port": "",
         "description": "Default deny / state violation rule"},
        rule(R_BASE, 100, src="10.20.30.0/24", dport="9000", description="base 9000"),
        rule(R_DUP, 200, src="10.20.30.0/24", dport="9000", description="dup 9000"),
        rule(R_UNUSED, 300, src="10.20.30.0/24", dport="8081", description="unused 8081"),
        rule(R_BLOCK, 400, action="block", proto="UDP", dport="9999", description="block 9999"),
        rule(R_MGMT, 500, interface="wan", src="203.0.113.0/24", dport="443", description="mgmt 443"),
        rule(R_CARP, 600, proto="CARP", description="carp"),
        rule(R_ALIAS, 700, interface="wan", src="tbl", dport="443", description="alias 443"),
        rule(R_TAIL, 800, src="10.20.30.7", dport="7000", description="tail 7000"),
    ]


class FakeFirewall:
    def __init__(self, base: str, *, role: str | None = None, rules: list[dict] | None = None,
                 pfsync: str = "", peer: str = "", lan_addr: str = "10.20.30.2", wan_addr: str = "203.0.113.57",
                 log: list | None = None):
        self.base = base
        self.log = log  # shared across firewalls: the order of calls over a cluster
        self.role = role
        self.rules: list[dict] = copy.deepcopy(rules if rules is not None else default_rules())
        self.pfsync, self.peer = pfsync, peer
        self.lan_addr, self.wan_addr = lan_addr, wan_addr
        self.fail: dict = {}
        self.calls: list[tuple[str, str]] = []
        self.applied = 0
        self.down = False
        self.on_call = None  # callable(kind, uuid) run before a write is answered (e.g. look at the database)

    # ── state helpers ──
    def get(self, uuid):
        return next((r for r in self.rules if r["uuid"].lower() == uuid.lower()), None)

    def _inject(self, key):
        effect = self.fail.get(key)
        if effect is None:
            return None
        if isinstance(effect, list):
            effect = effect.pop(0) if effect else None
            if effect is None:
                return None
        if isinstance(effect, type) and issubclass(effect, Exception):
            raise effect("injected")
        if isinstance(effect, Exception):
            raise effect
        if callable(effect) and not isinstance(effect, httpx.Response):
            return effect()
        return effect

    def pf_lines(self) -> dict:
        lines = {"@0 scrub in all fragment reassemble": {}}
        n = 1
        for r in sorted(self.rules, key=lambda r: r["sort_order"]):
            if str(r.get("enabled")) != "1":
                continue
            if r.get("is_automatic"):
                lines[f'@{n} block drop in log inet all label "{r["uuid"]}"'] = {"evaluations": 0}
            else:
                words = ["block" if r["action"] == "block" else "pass", "in", "quick", f"on {DEVICES.get(r['interface'], r['interface'])}", "inet"]
                if r["protocol"] not in ("", "any"):
                    words.append(f"proto {r['protocol'].lower()}")
                words.append(f'all label "{r["uuid"]}"')
                lines[f"@{n} " + " ".join(words)] = {"evaluations": 0, "packets": 0}
            n += 1
        return {"rules": {"filter rules": lines}}

    def model(self, row: dict) -> dict:
        """getRule shape: select fields as option dicts, no uuid key."""
        out = {k: v for k, v in row.items() if k not in ("uuid", "is_automatic", "legacy")}
        out["action"] = {a: {"value": a, "selected": 1 if row["action"] == a else 0} for a in ("pass", "block", "reject")}
        out["interface"] = {i: {"value": i.upper(), "selected": 1 if i in str(row["interface"]).split(",") else 0} for i in ("lan", "wan")}
        out["categories"] = []
        out["audit"] = {"created": "x", "updated": "y"}
        return out

    # ── respx wiring ──
    def mount(self, respx_mock):
        respx_mock.route(url__startswith=self.base).mock(side_effect=self.handle)
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        method = request.method
        self.calls.append((method, path))
        if self.log is not None:
            self.log.append((self.base, method, path))
        if self.down:
            raise httpx.ConnectError("down")
        if method == "GET" and path == "/diagnostics/interface/getVipStatus":
            rows = [] if self.role is None else [{"interface": "LAN", "vhid": "10", "status": self.role}]
            return httpx.Response(200, json={"rows": rows, "carp": {"allow": "1", "maintenancemode": False}})
        if method == "GET" and path == "/firewall/filter/searchRule":
            got = self._inject(("search", ""))
            if got is not None:
                return got
            return httpx.Response(200, json={"rows": copy.deepcopy(sorted(self.rules, key=lambda r: r["sort_order"]))})
        if method == "GET" and path == "/diagnostics/firewall/pfStatistics/rules":
            return httpx.Response(200, json=self.pf_lines())
        if method == "GET" and path == "/firewall/filter_util/ruleStats":
            return httpx.Response(200, json={"status": "ok", "stats": {}})
        if method == "GET" and path == "/interfaces/overview/interfacesInfo":
            return httpx.Response(200, json={"rows": [
                {"identifier": "wan", "device": "vtnet0", "description": "WAN", "ipv4": [{"ipaddr": f"{self.wan_addr}/24"}],
                 "ipv6": [], "gateways": ["203.0.113.1"], "routes": ["default"]},
                {"identifier": "lan", "device": "vtnet1", "description": "LAN",
                 "ipv4": [{"ipaddr": f"{self.lan_addr}/24"}, {"ipaddr": "10.20.30.1/24", "vhid": "10"}],
                 "ipv6": [], "gateways": [], "routes": ["10.20.30.0/24"]},
                {"identifier": "lo0", "device": "lo0", "description": "Loopback", "ipv4": [{"ipaddr": "127.0.0.1/8"}], "ipv6": []},
            ]})
        if method == "GET" and path == "/firewall/group/searchItem":
            return httpx.Response(200, json={"rows": []})
        if method == "GET" and path == "/firewall/alias/searchItem":
            return httpx.Response(200, json={"rows": [{"name": "tbl", "type": "urltable", "content": "https://203.0.113.9/list"}]})
        if method == "GET" and path == "/firewall/d_nat/searchRule":
            return httpx.Response(200, json={"rows": []})
        if method == "GET" and path == "/core/hasync/get":
            sel = {"": {"value": "Disabled", "selected": 1 if not self.pfsync else 0},
                   "lan": {"value": "LAN", "selected": 1 if self.pfsync == "lan" else 0}}
            return httpx.Response(200, json={"hasync": {"pfsyncinterface": sel, "pfsyncpeerip": self.peer, "password": "x"}})
        m = re.fullmatch(r"/firewall/filter/getRule/([^/]+)", path)
        if method == "GET" and m:
            got = self._inject(("get", m.group(1)))
            if got is not None:
                return got
            row = self.get(m.group(1))
            if row is None or row.get("is_automatic") or row.get("legacy"):
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"rule": self.model(row)})
        m = re.fullmatch(r"/firewall/filter/toggleRule/([^/]+)/([01])", path)
        if method == "POST" and m:
            if self.on_call:
                self.on_call("toggle", m.group(1))
            row = self.get(m.group(1))
            if row is not None and not row.get("legacy"):
                pending = ("toggle", m.group(1))
                if pending in self.fail and self.fail.get(("toggle_applies", m.group(1))):
                    row["enabled"] = m.group(2)  # applied on the firewall, but the answer is lost
                got = self._inject(pending)
                if got is not None:
                    return got
                changed = row["enabled"] != m.group(2)
                row["enabled"] = m.group(2)
                return httpx.Response(200, json={"result": "Enabled" if m.group(2) == "1" else "Disabled", "changed": changed})
            return httpx.Response(200, json={"result": "failed"})
        m = re.fullmatch(r"/firewall/filter/delRule/([^/]+)", path)
        if method == "POST" and m:
            if self.on_call:
                self.on_call("delete", m.group(1))
            row = self.get(m.group(1))
            applies = self.fail.get(("delete_applies", m.group(1)))
            if applies and row is not None:
                self.rules.remove(row)
            got = self._inject(("delete", m.group(1)))
            if got is not None:
                return got
            if row is None or row.get("legacy"):
                return httpx.Response(200, json={"result": "not found"})
            self.rules.remove(row)
            return httpx.Response(200, json={"result": "deleted"})
        if method == "POST" and path == "/firewall/filter/addRule":
            got = self._inject(("add", ""))
            if got is not None:
                return got
            import json as _json
            body = _json.loads(request.content)["rule"]
            new = str(uuid_lib.uuid4())
            top = max(int(float(r.get("sequence") or 0)) for r in self.rules if not r.get("is_automatic"))
            row = {**body, "uuid": new, "sequence": str(top + 100), "sort_order": f"400000.{top + 100:07d}", "prio_group": "400000"}
            self.rules.append(row)
            return httpx.Response(200, json={"result": "saved", "uuid": new})
        m = re.fullmatch(r"/firewall/filter/moveRuleBefore/([^/]+)/([^/]+)", path)
        if method == "POST" and m:
            got = self._inject(("move", m.group(1))) or self._inject(("move", "*"))
            if got is not None:
                return got
            moving, target = self.get(m.group(1)), self.get(m.group(2))
            if moving is None or target is None or moving is target:
                return httpx.Response(500, json={"errorMessage": "Unexpected error, check log for details"})
            seq = int(target["sequence"]) - 1
            moving["sequence"], moving["sort_order"] = str(seq), f"400000.{seq:07d}"
            return httpx.Response(200, json={"status": "ok"})
        if method == "POST" and path == "/firewall/filter/apply":
            if self.on_call:
                self.on_call("apply", "")
            got = self._inject(("apply", ""))
            if got is not None:
                return got
            self.applied += 1
            return httpx.Response(200, json={"status": "OK\n\n"})
        return httpx.Response(404, json={"message": f"unexpected {method} {path}"})

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.calls if m == "POST"]
