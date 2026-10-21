"""Linking hand-made objects on a second firewall to the central object that already exists
(services/adopt_link_service.py), instead of creating a duplicate."""
import json

import httpx
import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models.alias import FirewallAlias
from app.models.audit import AuditEvent
from app.models.port_forward import PortForwardRule
from tests.helpers import deploy_approved
from tests.test_api_tokens import _login
from tests.test_object_adopt import AUTOMATIC_NAT, NAT_KINDS, SYSTEM_ALIASES, alias_row, o2o_row, pf_row, snat_row
from tests.test_object_adopt import _mode
from tests.test_rule_adopt import _row as rule_row

A = "https://fw-link-a.test"
B = "https://fw-link-b.test"


async def _host(client, respx_mock, base, name):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    r = await client.post("/api/hosts", json={"name": name, "base_url": base, "api_key": "k", "api_secret": "s"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _search(respx_mock, base, path, rows):
    return respx_mock.get(f"{base}/api/firewall/{path}").mock(return_value=httpx.Response(200, json={"rows": rows, "total": len(rows)}))


def _live_writes(respx_mock):
    return [c for c in respx_mock.calls if c.request.method != "GET" and "/firewall/" in str(c.request.url)]


@pytest.fixture
async def two(client, respx_mock):
    a = await _host(client, respx_mock, A, "fw-link-a")
    b = await _host(client, respx_mock, B, "fw-link-b")
    _mode(respx_mock)  # outbound NAT mode is read per base url below
    for base in (A, B):
        respx_mock.get(f"{base}/api/firewall/source_nat/get").mock(return_value=httpx.Response(
            200, json={"filter": {"general": {"snat_mode": {m: {"selected": 1 if m == "hybrid" else 0} for m in
                                                              ("automatic", "hybrid", "advanced", "disabled")}}}}))
    return a, b


async def _put_vars(client, host_id, values):
    r = await client.put(f"/api/hosts/{host_id}/variables", json={"variables": values})
    assert r.status_code == 200, r.text


# ── aliases ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alias_is_linked_not_duplicated_and_rolls_out_to_both_hosts(client, respx_mock, two):
    a, b = two
    live_a = alias_row("web_servers", "host", "10.0.0.1\n10.0.0.2", uuid="uuid-a")
    live_b = alias_row("web_servers", "host", "10.0.0.1\n10.0.0.2", uuid="uuid-b")
    _search(respx_mock, A, "alias/searchItem", SYSTEM_ALIASES + [live_a])
    _search(respx_mock, B, "alias/searchItem", SYSTEM_ALIASES + [live_b])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "uuid-a"})).json()

    # host B: the candidate carries the exact twin (name_exists no longer decides alone)
    cand = (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json()
    row = next(r for r in cand if r["uuid"] == "uuid-b")
    assert row["linkable"] is True and row["reasons"] == ["name_exists"]
    assert [(m["id"], m["name"], m["targets"], m["diff"]) for m in row["matches"]] == [(first["id"], "web_servers", ["fw-link-a"], [])]
    assert row["similar"] == [] and row["bulk_action"] == "link"

    # adopting it as an own object is still refused (the name is taken)
    assert (await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "uuid-b"})).status_code == 409

    resp = await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "uuid-b", "link_to": first["id"]})
    assert resp.status_code == 201, resp.text
    linked = resp.json()
    assert linked["id"] == first["id"] and sorted(linked["target_host_ids"]) == sorted([a, b])
    assert {d["host_id"]: d["status"] for d in linked["deployments"]} == {a: "success", b: "success"}
    assert not _live_writes(respx_mock)  # nothing is written to a firewall
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FirewallAlias)) == 1
        events = (await db.scalars(select(AuditEvent).where(AuditEvent.kind == "alias"))).all()
    assert any(e.message == "Mit vorhandenem Objekt verknüpft" and e.details["remote_uuid"] == "uuid-b" for e in events)
    revisions = (await client.get(f"/api/aliases/{first['id']}/revisions")).json()
    assert any("fw-link-b verknüpft" in r["note"] for r in revisions)

    # preview: unchanged on both hosts; it is no longer offered on B
    preview = (await client.get(f"/api/aliases/{first['id']}/preview")).json()
    assert sorted(h["action"] for h in preview["hosts"]) == ["unchanged", "unchanged"]
    assert [r for r in (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json() if r["reasons"] != ["system"]] == []

    # one edit rolls out to both remote objects, never as a new one
    edit = {"name": "web_servers", "type": "host", "content": "10.0.0.1\n10.0.0.3", "description": "web_servers desc", "enabled": True,
            "target_host_ids": [a, b], "target_group_ids": [], "expected_updated_at": linked["updated_at"]}
    assert (await client.put(f"/api/aliases/{first['id']}", json=edit)).status_code == 200
    add = respx_mock.post(url__regex=r".*/alias/addItem").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "dup"}))
    set_a = respx_mock.post(f"{A}/api/firewall/alias/setItem/uuid-a").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    set_b = respx_mock.post(f"{B}/api/firewall/alias/setItem/uuid-b").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(url__regex=r".*/alias/reconfigure").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert (await deploy_approved(client, f"/api/aliases/{first['id']}/deploy")).status_code == 200
    assert set_a.called and set_b.called and not add.called


@pytest.mark.asyncio
async def test_link_is_idempotent(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("dup_me", uuid="uuid-a")])
    _search(respx_mock, B, "alias/searchItem", [alias_row("dup_me", uuid="uuid-b")])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "uuid-a"})).json()
    body = {"host_id": b, "uuid": "uuid-b", "link_to": first["id"]}
    assert (await client.post("/api/aliases/adopt", json=body)).status_code == 201
    again = await client.post("/api/aliases/adopt", json=body)
    assert again.status_code == 201, again.text
    assert len(again.json()["deployments"]) == 2 and len(again.json()["target_host_ids"]) == 2
    # linking the same live object to ANOTHER central object is refused
    other = (await client.post("/api/aliases", json={"name": "other", "type": "host", "content": "1.1.1.1"})).json()
    assert (await client.post("/api/aliases/adopt", json={**body, "link_to": other["id"]})).status_code == 409


@pytest.mark.asyncio
async def test_different_content_shows_a_diff_and_is_never_linked(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("ntp", "host", "10.0.0.1", uuid="uuid-a")])
    _search(respx_mock, B, "alias/searchItem", [alias_row("ntp", "host", "10.0.0.1\n10.0.0.9", uuid="uuid-b", description="other text")])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "uuid-a"})).json()
    row = (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json()[0]
    assert row["linkable"] is False and row["matches"] == [] and row["bulk_action"] == "skip"
    sim = row["similar"][0]
    assert sim["id"] == first["id"]
    assert {d["field"]: (d["central"], d["live"]) for d in sim["diff"]} == {
        "content": ("10.0.0.1", "10.0.0.1\n10.0.0.9"), "description": ("ntp desc", "other text")}
    # forcing the link by id is refused too: the live state is compared again on the server
    resp = await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "uuid-b", "link_to": first["id"]})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "ADOPT_LINK_TARGET_CHANGED"
    assert "content" in resp.json()["detail"]["params"]["fields"]
    # nothing was linked
    alias = next(x for x in (await client.get("/api/aliases")).json() if x["id"] == first["id"])
    assert alias["target_host_ids"] == [a] and len(alias["deployments"]) == 1


@pytest.mark.asyncio
async def test_browser_state_is_not_trusted_live_state_decides(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("srv", "host", "10.0.0.1", uuid="uuid-a")])
    _search(respx_mock, B, "alias/searchItem", [alias_row("srv", "host", "10.0.0.1", uuid="uuid-b")])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "uuid-a"})).json()
    assert (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json()[0]["linkable"] is True
    # between listing and clicking somebody changes the alias on host B by hand
    _search(respx_mock, B, "alias/searchItem", [alias_row("srv", "host", "10.0.0.1\n10.0.0.77", uuid="uuid-b")])
    resp = await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "uuid-b", "link_to": first["id"]})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "ADOPT_LINK_TARGET_CHANGED"


@pytest.mark.asyncio
async def test_variables_are_resolved_per_host(client, respx_mock, two):
    a, b = two
    await _put_vars(client, a, {"LAN_NET": "192.168.1.0/24"})
    await _put_vars(client, b, {"LAN_NET": "192.168.2.0/24"})
    central = (await client.post("/api/aliases", json={"name": "lan_net", "type": "network", "content": "{{LAN_NET}}", "description": "lan_net desc",
                                                       "target_host_ids": [a]})).json()
    _search(respx_mock, B, "alias/searchItem", [alias_row("lan_net", "network", "192.168.2.0/24", uuid="uuid-b")])
    row = (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json()[0]
    assert row["linkable"] is True and row["matches"][0]["id"] == central["id"]
    resp = await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "uuid-b", "link_to": central["id"]})
    assert resp.status_code == 201, resp.text
    dep_b = next(d for d in resp.json()["deployments"] if d["host_id"] == b)
    assert dep_b["status"] == "success"
    async with SessionLocal() as db:
        from app.models.alias_deployment import AliasDeployment
        deps = {str(d.host_id): d for d in (await db.scalars(select(AliasDeployment))).all()}
    assert deps[b].opnsense_alias_uuid == "uuid-b" and deps[b].last_payload["content"] == "192.168.2.0/24"
    preview = (await client.get(f"/api/aliases/{central['id']}/preview")).json()
    assert {h["host_name"]: h["action"] for h in preview["hosts"]}["fw-link-b"] == "unchanged"  # resolved payload == live

    # a host with a DIFFERENT value for the variable does not match; the diff shows the resolved value
    _search(respx_mock, B, "alias/searchItem", [alias_row("lan_net", "network", "192.168.9.0/24", uuid="uuid-x")])
    # (host B already has the object, so ask for it through a third host)
    c = await _host(client, respx_mock, "https://fw-link-c.test", "fw-link-c")
    await _put_vars(client, c, {"LAN_NET": "192.168.3.0/24"})
    _search(respx_mock, "https://fw-link-c.test", "alias/searchItem", [alias_row("lan_net", "network", "192.168.9.0/24", uuid="uuid-c")])
    row = (await client.get(f"/api/aliases/unmanaged?host_id={c}")).json()[0]
    assert row["matches"] == [] and row["similar"][0]["diff"] == [{"field": "content", "central": "192.168.3.0/24", "live": "192.168.9.0/24"}]


@pytest.mark.asyncio
async def test_missing_variable_on_the_host_never_matches(client, respx_mock, two):
    a, b = two
    await _put_vars(client, a, {"LAN_NET": "192.168.1.0/24"})
    await client.post("/api/aliases", json={"name": "lan_net", "type": "network", "content": "{{LAN_NET}}", "description": "lan_net desc", "target_host_ids": [a]})
    _search(respx_mock, B, "alias/searchItem", [alias_row("lan_net", "network", "192.168.2.0/24", uuid="uuid-b")])
    row = (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json()[0]
    assert row["matches"] == [] and row["similar"][0]["diff"][0]["field"] == "variables"


@pytest.mark.asyncio
async def test_nested_alias_references_are_part_of_the_comparison(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [
        alias_row("inner", "network", "10.1.0.0/16", uuid="in-a"),
        alias_row("outer", "networkgroup", "inner\n10.2.0.0/16", uuid="out-a")])
    result = (await client.post("/api/aliases/adopt-all", json={"host_id": a})).json()
    assert len(result["adopted"]) == 2
    _search(respx_mock, B, "alias/searchItem", [
        alias_row("inner", "network", "10.1.0.0/16", uuid="in-b"),
        alias_row("outer", "networkgroup", "inner\n10.2.0.0/16", uuid="out-b")])
    rows = {r["title"]: r for r in (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json()}
    assert rows["inner"]["linkable"] and rows["outer"]["linkable"]
    # a different reference is a diff, not a match
    _search(respx_mock, B, "alias/searchItem", [
        alias_row("inner", "network", "10.1.0.0/16", uuid="in-b"),
        alias_row("outer", "networkgroup", "other_alias\n10.2.0.0/16", uuid="out-b"),
        alias_row("other_alias", "network", "10.5.0.0/16", uuid="oth-b")])
    rows = {r["title"]: r for r in (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json()}
    assert rows["outer"]["matches"] == [] and rows["outer"]["similar"][0]["diff"][0]["field"] == "content"

    # "adopt all" with link: both linked, nothing created
    _search(respx_mock, B, "alias/searchItem", [
        alias_row("inner", "network", "10.1.0.0/16", uuid="in-b"),
        alias_row("outer", "networkgroup", "inner\n10.2.0.0/16", uuid="out-b")])
    done = (await client.post("/api/aliases/adopt-all", json={"host_id": b, "link_existing": True})).json()
    assert sorted((x["title"], x["linked"]) for x in done["adopted"]) == [("inner", True), ("outer", True)] and done["skipped"] == []
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FirewallAlias)) == 2


@pytest.mark.asyncio
async def test_adopt_all_without_link_flag_keeps_old_behaviour(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("x1", uuid="u-a")])
    await client.post("/api/aliases/adopt-all", json={"host_id": a})
    _search(respx_mock, B, "alias/searchItem", [alias_row("x1", uuid="u-b")])
    done = (await client.post("/api/aliases/adopt-all", json={"host_id": b})).json()
    assert done["adopted"] == [] and done["skipped"][0]["reasons"] == ["name_exists"]


# ── NAT + rules, every kind ─────────────────────────────────────────────

KINDS = {
    "port-forward": ("d_nat/searchRule", pf_row, {"descr": "Web"}, {"destination.port": "9999"}, "local_port"),
    "one-to-one": ("one_to_one/searchRule", o2o_row, {"description": "Mail 1:1"}, {"external": "203.0.113.77"}, "external"),
    "outbound-nat": ("source_nat/searchRule", snat_row, {"description": "LAN out"}, {"source_net": "192.168.9.0/24"}, "source_net"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", list(KINDS))
async def test_nat_kinds_link_diff_and_bulk(client, respx_mock, two, prefix):
    a, b = two
    path, factory, _, change, _ = KINDS[prefix]
    _search(respx_mock, A, path, [factory(uuid="a-1"), *AUTOMATIC_NAT])
    first = (await client.post(f"/api/{prefix}/adopt", json={"host_id": a, "uuid": "a-1"})).json()

    # different on B -> hint + diff, no link possible
    _search(respx_mock, B, path, [factory(uuid="b-1", **change)])
    row = (await client.get(f"/api/{prefix}/unmanaged?host_id={b}")).json()[0]
    assert row["adoptable"] is True and row["linkable"] is False and row["matches"] == []
    assert row["similar"][0]["id"] == first["id"] and row["similar"][0]["diff"]
    resp = await client.post(f"/api/{prefix}/adopt", json={"host_id": b, "uuid": "b-1", "link_to": first["id"]})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "ADOPT_LINK_TARGET_CHANGED"
    adopt_all = (await client.post(f"/api/{prefix}/adopt-all", json={"host_id": b, "link_existing": True})).json()
    assert adopt_all["adopted"][0]["linked"] is False  # a different object is adopted as its own, not linked

    # fresh start for the exact case on a third host
    c = await _host(client, respx_mock, "https://fw-link-c.test", "fw-link-c")
    respx_mock.get("https://fw-link-c.test/api/firewall/source_nat/get").mock(return_value=httpx.Response(
        200, json={"filter": {"general": {"snat_mode": {m: {"selected": 1 if m == "hybrid" else 0} for m in
                                                          ("automatic", "hybrid", "advanced", "disabled")}}}}))
    _search(respx_mock, "https://fw-link-c.test", path, [factory(uuid="c-1")])
    row = (await client.get(f"/api/{prefix}/unmanaged?host_id={c}")).json()[0]
    ids = [m["id"] for m in row["matches"]]
    assert ids == [first["id"]] and row["bulk_action"] == "link" and row["linkable"]
    resp = await client.post(f"/api/{prefix}/adopt", json={"host_id": c, "uuid": "c-1", "link_to": first["id"]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == first["id"] and c in resp.json()["target_host_ids"]
    preview = (await client.get(f"/api/{prefix}/{first['id']}/preview")).json()
    assert {h["host_name"]: h["action"] for h in preview["hosts"]}["fw-link-c"] == "unchanged"
    assert not _live_writes(respx_mock)
    listed = (await client.get(f"/api/{prefix}")).json()
    assert len(listed) == 2  # first (A + C) and the different one from B


@pytest.mark.asyncio
async def test_port_forward_link_updates_remote_object_without_duplicate(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "d_nat/searchRule", [pf_row(uuid="pf-a")])
    _search(respx_mock, B, "d_nat/searchRule", [pf_row(uuid="pf-b")])
    first = (await client.post("/api/port-forward/adopt", json={"host_id": a, "uuid": "pf-a"})).json()
    linked = (await client.post("/api/port-forward/adopt", json={"host_id": b, "uuid": "pf-b", "link_to": first["id"]})).json()
    edit = {k: linked[k] for k in ("description", "interface", "ipprotocol", "protocol", "source_net", "source_port",
                                    "destination_net", "destination_port", "target", "local_port", "natreflection", "enabled")}
    for base in (A, B):  # alias lookups and the interface consistency check of a multi-host rollout
        _search(respx_mock, base, "alias/searchItem", [])
    for base in (A, B):  # the interface consistency check of a multi-host rollout
        respx_mock.get(f"{base}/api/firewall/filter/getRule").mock(return_value=httpx.Response(
            200, json={"rule": {"interface": {"wan": {"value": "WAN", "selected": 1}}}}))
    edit.update(target="192.168.1.99", target_host_ids=[a, b], target_group_ids=[], expected_updated_at=linked["updated_at"])
    assert (await client.put(f"/api/port-forward/{first['id']}", json=edit)).status_code == 200
    add = respx_mock.post(url__regex=r".*/d_nat/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "dup"}))
    sa = respx_mock.post(f"{A}/api/firewall/d_nat/setRule/pf-a").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    sb = respx_mock.post(f"{B}/api/firewall/d_nat/setRule/pf-b").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(url__regex=r".*/d_nat/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    assert (await deploy_approved(client, f"/api/port-forward/{first['id']}/deploy")).status_code == 200
    assert sa.called and sb.called and not add.called
    assert json.loads(sb.calls.last.request.content)["rule"]["target"] == "192.168.1.99"


@pytest.mark.asyncio
async def test_rule_link_diff_and_errors(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "filter/searchRule", [rule_row("r-a")])
    _search(respx_mock, B, "filter/searchRule", [rule_row("r-b")])
    first = (await client.post("/api/rules/adopt", json={"host_id": a, "rule_uuid": "r-a"})).json()
    row = (await client.get(f"/api/rules/unmanaged?host_id={b}")).json()[0]
    assert row["linkable"] is True and [m["id"] for m in row["matches"]] == [first["id"]]
    resp = await client.post("/api/rules/adopt", json={"host_id": b, "rule_uuid": "r-b", "link_to": first["id"]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == first["id"] and sorted(resp.json()["target_host_ids"]) == sorted([a, b])
    again = await client.post("/api/rules/adopt", json={"host_id": b, "rule_uuid": "r-b", "link_to": first["id"]})
    assert again.status_code == 201 and len(again.json()["deployments"]) == 2
    preview = (await client.get(f"/api/rules/{first['id']}/preview")).json()
    assert sorted(h["action"] for h in preview["hosts"]) == ["unchanged", "unchanged"]
    assert not _live_writes(respx_mock)

    # different port -> diff only
    c = await _host(client, respx_mock, "https://fw-link-c.test", "fw-link-c")
    _search(respx_mock, "https://fw-link-c.test", "filter/searchRule", [rule_row("r-c", destination_port="2222")])
    row = (await client.get(f"/api/rules/unmanaged?host_id={c}")).json()[0]
    assert row["linkable"] is False and row["similar"][0]["diff"] == [{"field": "destination_port", "central": "22", "live": "2222"}]
    refused = await client.post("/api/rules/adopt", json={"host_id": c, "rule_uuid": "r-c", "link_to": first["id"]})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "ADOPT_LINK_TARGET_CHANGED"


# ── error cases ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deleted_target_and_host_already_has_object_and_busy(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("gone", uuid="u-a")])
    _search(respx_mock, B, "alias/searchItem", [alias_row("gone", uuid="u-b")])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "u-a"})).json()
    # busy: an unconfirmed deployment on the central object
    respx_mock.post(f"{A}/api/firewall/alias/setItem/u-a").mock(return_value=httpx.Response(200, json={"result": "saved"}))
    respx_mock.post(url__regex=r".*/alias/reconfigure").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    edit = {"name": "gone", "type": "host", "content": "10.9.9.1\n10.9.9.2", "description": "gone desc", "enabled": False,
            "target_host_ids": [a], "target_group_ids": [], "expected_updated_at": first["updated_at"]}
    assert (await client.put(f"/api/aliases/{first['id']}", json=edit)).status_code == 200
    assert (await deploy_approved(client, f"/api/aliases/{first['id']}/deploy")).status_code == 200  # pending now
    busy = await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "u-b", "link_to": first["id"]})
    assert busy.status_code == 409  # content differs now AND it is pending; either way nothing is linked
    await client.post(f"/api/aliases/{first['id']}/confirm")

    # deleted in the meantime
    respx_mock.post(url__regex=r".*/alias/delItem/.*").mock(return_value=httpx.Response(200, json={"result": "deleted"}))
    assert (await client.delete(f"/api/aliases/{first['id']}")).status_code == 204
    gone = await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "u-b", "link_to": first["id"]})
    assert gone.status_code == 404 and gone.json()["detail"]["code"] == "ADOPT_LINK_TARGET_NOT_FOUND"


@pytest.mark.asyncio
async def test_pending_deployment_blocks_the_link(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("busy1", uuid="u-a")])
    _search(respx_mock, B, "alias/searchItem", [alias_row("busy1", uuid="u-b")])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "u-a"})).json()
    async with SessionLocal() as db:
        from app.models.alias_deployment import AliasDeployment
        dep = (await db.scalars(select(AliasDeployment))).one()
        dep.status = "pending"
        await db.commit()
    resp = await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "u-b", "link_to": first["id"]})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "ADOPT_LINK_BUSY"


@pytest.mark.asyncio
async def test_host_that_already_has_the_object_is_not_a_candidate_and_cannot_link(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("twice", uuid="u-a")])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "u-a"})).json()
    # a second, differently-UUID'd copy on host A itself: A already has the object
    _search(respx_mock, A, "alias/searchItem", [alias_row("twice", uuid="u-a"), alias_row("twice", uuid="u-a2")])
    rows = (await client.get(f"/api/aliases/unmanaged?host_id={a}")).json()
    assert [r["uuid"] for r in rows] == ["u-a2"] and rows[0]["matches"] == [] and rows[0]["linkable"] is False
    resp = await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "u-a2", "link_to": first["id"]})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "ADOPT_LINK_HOST_HAS_OBJECT"


@pytest.mark.asyncio
async def test_bulk_never_guesses_between_two_twins_and_links_each_object_once(client, respx_mock, two):
    a, b = two
    # two identical central objects exist from earlier separate adoptions
    for _ in range(2):
        await client.post("/api/port-forward", json={
            "description": "Web", "interface": "wan", "ipprotocol": "inet", "protocol": "tcp", "source_net": "any",
            "destination_net": "wanip", "destination_port": "8443", "target": "192.168.1.50", "local_port": "443",
            "natreflection": "", "enabled": True, "target_host_ids": [a], "target_group_ids": []})
    _search(respx_mock, B, "d_nat/searchRule", [pf_row(uuid="pf-b")])
    row = (await client.get(f"/api/port-forward/unmanaged?host_id={b}")).json()[0]
    assert len(row["matches"]) == 2 and row["bulk_action"] == "new"  # ask the user, do not guess
    done = (await client.post("/api/port-forward/adopt-all", json={"host_id": b, "link_existing": True})).json()
    assert [x["linked"] for x in done["adopted"]] == [False]


@pytest.mark.asyncio
async def test_bulk_links_each_central_object_only_once(client, respx_mock, two):
    a, b = two
    await client.post("/api/port-forward", json={
        "description": "Web", "interface": "wan", "ipprotocol": "inet", "protocol": "tcp", "source_net": "any",
        "destination_net": "wanip", "destination_port": "8443", "target": "192.168.1.50", "local_port": "443",
        "natreflection": "", "enabled": True, "target_host_ids": [a], "target_group_ids": []})
    # two identical live rows on B, ONE twin: the first links, the second becomes its own object
    _search(respx_mock, B, "d_nat/searchRule", [pf_row(uuid="b-1"), pf_row(uuid="b-2")])
    rows = (await client.get(f"/api/port-forward/unmanaged?host_id={b}")).json()
    assert sorted(r["bulk_action"] for r in rows) == ["link", "new"]  # the dialog's summary equals what happens
    done = (await client.post("/api/port-forward/adopt-all", json={"host_id": b, "link_existing": True})).json()
    assert sorted(x["linked"] for x in done["adopted"]) == [False, True]
    assert len((await client.get("/api/port-forward")).json()) == 2


@pytest.mark.asyncio
async def test_roles_for_linking(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("roles1", uuid="u-a")])
    _search(respx_mock, B, "alias/searchItem", [alias_row("roles1", uuid="u-b")])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "u-a"})).json()
    for role, expected in (("read_only", 403), ("operator", 201)):
        user = await client.post("/api/users", json={"username": f"lnk-{role}", "password": "adopt-test-password-1", "role": role})
        assert user.status_code == 201, user.text
        session = await _login(f"lnk-{role}", "adopt-test-password-1")
        try:
            resp = await session.post("/api/aliases/adopt", json={"host_id": b, "uuid": "u-b", "link_to": first["id"]})
            assert resp.status_code == expected, (role, resp.text)
        finally:
            await session.aclose()


@pytest.mark.asyncio
async def test_bulk_leaves_a_twin_alone_that_waits_for_a_confirmation(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("busy2", uuid="u-a")])
    _search(respx_mock, B, "alias/searchItem", [alias_row("busy2", uuid="u-b")])
    await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "u-a"})
    async with SessionLocal() as db:
        from app.models.alias_deployment import AliasDeployment
        (await db.scalars(select(AliasDeployment))).one().status = "pending"
        await db.commit()
    row = next(r for r in (await client.get(f"/api/aliases/unmanaged?host_id={b}")).json() if r["uuid"] == "u-b")
    assert row["linkable"] is False and row["bulk_action"] == "skip"
    resp = await client.post("/api/aliases/adopt-all", json={"host_id": b, "link_existing": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["adopted"] == []
    assert "twin_busy" in resp.json()["skipped"][0]["reasons"]
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(FirewallAlias)) == 1


@pytest.mark.asyncio
async def test_linking_invalidates_an_open_edit_form(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "alias/searchItem", [alias_row("stale1", uuid="u-a")])
    _search(respx_mock, B, "alias/searchItem", [alias_row("stale1", uuid="u-b")])
    first = (await client.post("/api/aliases/adopt", json={"host_id": a, "uuid": "u-a"})).json()
    linked = (await client.post("/api/aliases/adopt", json={"host_id": b, "uuid": "u-b", "link_to": first["id"]})).json()
    assert linked["updated_at"] != first["updated_at"]
    stale = {"name": "stale1", "type": "host", "content": linked["content"], "description": linked["description"],
             "enabled": linked["enabled"], "target_host_ids": [a], "target_group_ids": [],
             "expected_updated_at": first["updated_at"]}
    assert (await client.put(f"/api/aliases/{first['id']}", json=stale)).status_code == 409


@pytest.mark.asyncio
async def test_bulk_neither_links_nor_duplicates_a_busy_nat_twin(client, respx_mock, two):
    a, b = two
    _search(respx_mock, A, "d_nat/searchRule", [pf_row(uuid="pf-a")])
    _search(respx_mock, B, "d_nat/searchRule", [pf_row(uuid="pf-b")])
    await client.post("/api/port-forward/adopt", json={"host_id": a, "uuid": "pf-a"})
    async with SessionLocal() as db:
        from app.models.port_forward_deployment import PortForwardDeployment
        (await db.scalars(select(PortForwardDeployment))).one().status = "pending"
        await db.commit()
    resp = await client.post("/api/port-forward/adopt-all", json={"host_id": b, "link_existing": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["adopted"] == [] and "twin_busy" in resp.json()["skipped"][0]["reasons"]
    async with SessionLocal() as db:
        assert await db.scalar(select(func.count()).select_from(PortForwardRule)) == 1
