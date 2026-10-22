"""Client calls for filter rules that the cleanup assistant (1.31) builds on: toggleRule, getRule, the
getRule -> addRule payload, and the request shape / failure classification of add/del/move/apply.

The JSON fixtures are shaped like live answers of OPNsense 26.7.7 (checked 2026-10-22 on a test firewall) with
documentation addresses and made-up UUIDs. Evidence for each endpoint: comment block in services/opnsense_client.py.
"""
import json
import uuid

import httpx
import pytest

from app.models.host import OpnsenseHost
from app.services import opnsense_client as c

BASE = "https://fw.test/api/firewall/filter"
RULE = "5c6f3bfa-1718-4216-81c8-04a75527960f"
OTHER = "74190f58-f35f-4402-8794-5410e99ea364"


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(c.opnsense_secret_store, "read_secret", lambda host_id: "secret")
    return OpnsenseHost(id=uuid.uuid4(), name="fw-a", base_url="https://fw.test", api_key="key", verify_tls=False)


def _opts(selected: list[str], options: dict[str, str]) -> dict:
    return {k: {"value": v, "selected": 1 if k in selected else 0} for k, v in options.items()}


def live_rule() -> dict:
    """A `getRule/<uuid>` record: select fields as option dicts, `categories` an option dict, read-only extras."""
    return {
        "enabled": "1", "sequence": "566", "sort_order": "200000.0000566", "prio_group": "200000", "quick": "1",
        "action": _opts(["pass"], {"pass": "Pass", "block": "Block", "reject": "Reject"}),
        "interface": _opts(["lan", "wan"], {"lan": "LAN", "wan": "WAN", "wireguard": "WireGuard (Group)"}),
        "direction": _opts(["in"], {"in": "In", "out": "Out", "any": "Both"}),
        "protocol": _opts(["TCP"], {"any": "any", "TCP": "TCP", "UDP": "UDP"}),
        "gateway": _opts([""], {"": "None", "WAN_GW": "WAN_GW - 203.0.113.1"}),
        "categories": _opts(["c1", "c2"], {"c1": "one", "c2": "two", "c3": "three"}),
        "tcpflags1": _opts([], {"syn": "SYN", "ack": "ACK"}),
        "source_net": "bogons", "destination_net": "203.0.113.9", "destination_port": "8080-8443", "source_port": "",
        "description": "opnexus-b4-probe", "statetimeout": None, "log": 1,
        "audit": {"created": {"username": "", "time": "", "description": ""}, "userdata": {"note": ""}},
    }


def _body(route) -> dict:
    return json.loads(route.calls.last.request.content)


# ── toggle_rule ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, tail, answer", [(False, "0", "Disabled"), (True, "1", "Enabled")])
async def test_toggle_rule_sends_the_explicit_value_and_returns_the_answer(host, respx_mock, enabled, tail, answer):
    route = respx_mock.post(f"{BASE}/toggleRule/{RULE}/{tail}").mock(
        return_value=httpx.Response(200, json={"result": answer, "changed": True}))
    assert await c.toggle_rule(host, RULE, enabled) == {"result": answer, "changed": True}
    assert route.call_count == 1 and not route.calls.last.request.content  # never the flipping form, no body


@pytest.mark.asyncio
async def test_toggle_rule_is_idempotent_an_unchanged_answer_is_success(host, respx_mock):
    respx_mock.post(f"{BASE}/toggleRule/{RULE}/0").mock(
        return_value=httpx.Response(200, json={"result": "Disabled", "changed": False}))
    assert (await c.toggle_rule(host, RULE, False))["changed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, 1, "0", "1", None, "false"])
async def test_toggle_rule_refuses_anything_but_a_bool_without_a_request(host, respx_mock, bad):
    route = respx_mock.post(url__regex=r".*/toggleRule/.*").mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(TypeError):
        await c.toggle_rule(host, RULE, bad)
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_toggle_rule_answer_for_the_other_state_is_not_accepted(host, respx_mock):
    respx_mock.post(f"{BASE}/toggleRule/{RULE}/0").mock(return_value=httpx.Response(200, json={"result": "Enabled", "changed": True}))
    with pytest.raises(c.OpnsenseAPIError) as info:
        await c.toggle_rule(host, RULE, False)
    assert not isinstance(info.value, c.OpnsenseAmbiguousError)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [{"result": "failed"}, {"result": "failed", "changed": False}])
async def test_toggle_rule_unknown_uuid_is_a_plain_refusal(host, respx_mock, answer):
    """Live: an unknown or legacy UUID answers HTTP 200 {"result": "failed"} (no "changed" key)."""
    respx_mock.post(f"{BASE}/toggleRule/{RULE}/0").mock(return_value=httpx.Response(200, json=answer))
    with pytest.raises(c.OpnsenseAPIError) as info:
        await c.toggle_rule(host, RULE, False)
    assert type(info.value) is c.OpnsenseAPIError


@pytest.mark.asyncio
async def test_toggle_rule_failure_classes(host, respx_mock):
    route = respx_mock.post(f"{BASE}/toggleRule/{RULE}/0")
    route.mock(return_value=httpx.Response(403, json={"status": 403, "message": "Forbidden"}))
    with pytest.raises(c.OpnsenseForbiddenError):
        await c.toggle_rule(host, RULE, False)
    route.mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(c.OpnsenseAmbiguousError):  # the change may have been saved
        await c.toggle_rule(host, RULE, False)
    route.mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(c.OpnsenseAmbiguousError):
        await c.toggle_rule(host, RULE, False)
    route.mock(side_effect=httpx.ConnectError("down"))  # never sent: a plain error, not an unknown outcome
    with pytest.raises(c.OpnsenseAPIError) as info:
        await c.toggle_rule(host, RULE, False)
    assert not isinstance(info.value, c.OpnsenseAmbiguousError)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["../x", "a/b", "a?b", "", "x" * 65, "a b"])
async def test_rule_references_cannot_leave_the_url_path(host, respx_mock, bad):
    route = respx_mock.route().mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(c.OpnsenseAPIError):
        await c.toggle_rule(host, bad, False)
    with pytest.raises(c.OpnsenseAPIError):
        await c.get_rule(host, bad)
    assert route.call_count == 0


# ── get_rule ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_rule_returns_the_inner_record(host, respx_mock):
    route = respx_mock.get(f"{BASE}/getRule/{RULE}").mock(return_value=httpx.Response(200, json={"rule": live_rule()}))
    rule = await c.get_rule(host, RULE)
    assert route.call_count == 1 and rule["description"] == "opnexus-b4-probe" and rule["interface"]["lan"]["selected"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [[], {}])
async def test_get_rule_empty_answer_means_not_found(host, respx_mock, answer):
    """Live: unknown, legacy (hex label) and automatic UUIDs answer HTTP 200 with a bare `[]`."""
    respx_mock.get(f"{BASE}/getRule/{RULE}").mock(return_value=httpx.Response(200, json=answer))
    with pytest.raises(c.OpnsenseNotFoundError):
        await c.get_rule(host, RULE)


@pytest.mark.asyncio
async def test_get_rule_failure_classes(host, respx_mock):
    route = respx_mock.get(f"{BASE}/getRule/{RULE}")
    route.mock(return_value=httpx.Response(200, json={"rule": "nonsense"}))
    with pytest.raises(c.OpnsenseAPIError) as info:
        await c.get_rule(host, RULE)
    assert not isinstance(info.value, c.OpnsenseNotFoundError)  # an unreadable answer is not proof that the rule is gone
    route.mock(return_value=httpx.Response(403, text="no"))
    with pytest.raises(c.OpnsenseForbiddenError):
        await c.get_rule(host, RULE)
    route.mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(c.OpnsenseAmbiguousError):
        await c.get_rule(host, RULE)


# ── rule_write_payload (getRule -> addRule) ───────────────────────────────

def test_rule_write_payload_reduces_selects_and_drops_read_only_fields():
    payload = c.rule_write_payload(live_rule())
    assert payload["interface"] == "lan,wan"          # multi-interface: comma separated
    assert payload["categories"] == "c1,c2"
    assert payload["action"] == "pass" and payload["protocol"] == "TCP" and payload["direction"] == "in"
    assert payload["gateway"] == ""                    # the "None" option has the key ""
    assert payload["tcpflags1"] == ""                  # nothing selected
    assert payload["statetimeout"] == "" and payload["log"] == "1" and payload["source_port"] == ""
    assert payload["enabled"] == "1" and payload["destination_port"] == "8080-8443"
    assert not set(payload) & {"uuid", "sequence", "sort_order", "prio_group", "audit"}
    assert all(isinstance(v, str) for v in payload.values())


def test_rule_write_payload_empty_category_list_becomes_an_empty_string():
    """Live: a rule without categories answers `"categories": []` (PHP's empty array), not an option dict."""
    rule = live_rule() | {"categories": []}
    assert c.rule_write_payload(rule)["categories"] == ""


def test_rule_write_payload_does_not_mutate_its_input():
    rule = live_rule()
    before = json.dumps(rule, sort_keys=True)
    c.rule_write_payload(rule)
    assert json.dumps(rule, sort_keys=True) == before


@pytest.mark.asyncio
async def test_a_copy_is_added_with_exactly_the_payload_and_stays_disabled(host, respx_mock):
    """The restore path: getRule -> payload -> addRule(enabled=0). The body must carry the payload unchanged."""
    respx_mock.get(f"{BASE}/getRule/{RULE}").mock(return_value=httpx.Response(200, json={"rule": live_rule()}))
    add = respx_mock.post(f"{BASE}/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": OTHER}))
    payload = c.rule_write_payload(await c.get_rule(host, RULE)) | {"enabled": "0"}
    assert (await c.add_rule(host, payload))["uuid"] == OTHER
    sent = _body(add)["rule"]
    assert sent == payload and sent["enabled"] == "0" and "sequence" not in sent and "uuid" not in sent


# ── add / set / del / move / apply: request shape and classification ─────

@pytest.mark.asyncio
async def test_add_rule_validation_failure_is_a_plain_refusal(host, respx_mock):
    respx_mock.post(f"{BASE}/addRule").mock(return_value=httpx.Response(
        200, json={"result": "failed", "validations": {"rule.destination_port": "Please specify a valid portnumber"}}))
    with pytest.raises(c.OpnsenseAPIError) as info:
        await c.add_rule(host, {"destination_port": "1,2"})
    assert type(info.value) is c.OpnsenseAPIError and "destination_port" in str(info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["deleted", "not found"])
async def test_del_rule_accepts_deleted_and_not_found(host, respx_mock, answer):
    route = respx_mock.post(f"{BASE}/delRule/{RULE}").mock(return_value=httpx.Response(200, json={"result": answer}))
    assert (await c.del_rule(host, RULE))["result"] == answer and route.call_count == 1


@pytest.mark.asyncio
async def test_del_rule_failed_answer_and_5xx(host, respx_mock):
    route = respx_mock.post(f"{BASE}/delRule/{RULE}")
    route.mock(return_value=httpx.Response(200, json={"result": "failed"}))
    with pytest.raises(c.OpnsenseAPIError):
        await c.del_rule(host, RULE)
    route.mock(return_value=httpx.Response(500, text="x"))
    with pytest.raises(c.OpnsenseAmbiguousError):
        await c.del_rule(host, RULE)


@pytest.mark.asyncio
async def test_move_rule_before_url_and_answer(host, respx_mock):
    route = respx_mock.post(f"{BASE}/moveRuleBefore/{RULE}/{OTHER}").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert await c.move_rule_before(host, RULE, OTHER) == {"status": "ok"} and route.call_count == 1


@pytest.mark.asyncio
async def test_move_rule_before_refusal_is_http_500_and_therefore_ambiguous(host, respx_mock):
    """Live: unknown UUID, the same UUID twice and rules of different prio_groups all answer HTTP 500
    {"errorMessage": "Unexpected error, check log for details"}. Callers must read the position back."""
    respx_mock.post(f"{BASE}/moveRuleBefore/{RULE}/{OTHER}").mock(return_value=httpx.Response(
        500, json={"errorMessage": "Unexpected error, check log for details", "errorTitle": "Filter", "errorLevel": "warning"}))
    with pytest.raises(c.OpnsenseAmbiguousError):
        await c.move_rule_before(host, RULE, OTHER)


@pytest.mark.asyncio
async def test_apply_url_and_answer(host, respx_mock):
    route = respx_mock.post(f"{BASE}/apply").mock(return_value=httpx.Response(200, json={"status": "OK\n\n"}))
    assert (await c.apply(host))["status"].strip() == "OK" and route.call_count == 1
