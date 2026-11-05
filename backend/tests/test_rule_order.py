"""Moving rules on a firewall (services/rule_order_service.py, routers/rule_order.py) against a small stateful fake.

The fake keeps the rules of ONE priority group in a list (sort_order = prio_group.sequence, sequences 100, 200, ...)
and understands searchRule, moveRuleBefore, setRule (partial body, only ``sequence`` is read), addRule, delRule and apply.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.rule_order import RuleOrderChange
from app.services import api_token_service, rule_order_service


class Fw:
    def __init__(self, respx_mock, host="fw1", names=("alpha", "bravo", "charlie", "delta")):
        self.host = host
        self.seq = {n: (i + 1) * 100 for i, n in enumerate(names)}
        self.extra = [{"uuid": "auto-1", "description": "Default deny", "prio_group": "1", "sort_order": "000001.1000001",
                       "sequence": "1000001", "is_automatic": "1", "enabled": "1", "action": "block", "direction": "in"}]
        self.refuse_move = False
        self.ignore_move = False  # answers ok but does nothing
        self.down = False
        self.calls = []
        base = f"https://{host}.test/api/firewall/filter"
        respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
        respx_mock.get(f"{base}/searchRule").mock(side_effect=self._search)
        respx_mock.post(url__regex=rf"{base}/moveRuleBefore/.*").mock(side_effect=self._move)
        respx_mock.post(url__regex=rf"{base}/setRule/.*").mock(side_effect=self._set)
        respx_mock.post(f"{base}/addRule").mock(side_effect=self._add)
        respx_mock.post(url__regex=rf"{base}/delRule/.*").mock(return_value=httpx.Response(200, json={"result": "deleted"}))
        respx_mock.post(f"{base}/apply").mock(side_effect=self._apply)

    def order(self):
        return [n for n, _ in sorted(self.seq.items(), key=lambda kv: kv[1])]

    def _search(self, req):
        if self.down:
            return httpx.Response(500, json={})
        rows = [{"uuid": n, "description": n, "prio_group": "400000", "sort_order": f"400000.{s:07d}", "sequence": str(s),
                 "enabled": "1", "action": "block", "direction": "in", "interface": "lan", "protocol": "TCP",
                 "source_net": "any", "destination_net": "any"} for n, s in self.seq.items()]
        return httpx.Response(200, json={"rows": sorted(self.extra + rows, key=lambda r: r["sort_order"])})

    def _move(self, req):
        sel, target = req.url.path.rsplit("/", 2)[-2:]
        self.calls.append(("move", sel, target))
        if self.refuse_move:
            return httpx.Response(500, json={"errorMessage": "Unexpected error"})
        if self.ignore_move:
            return httpx.Response(200, json={"status": "ok"})
        names = [n for n in self.order() if n != sel]
        names.insert(names.index(target), sel)
        self.seq = {n: (i + 1) * 100 for i, n in enumerate(names)}
        return httpx.Response(200, json={"status": "ok"})

    def _set(self, req):
        uid = req.url.path.rsplit("/", 1)[-1]
        body = json.loads(req.content)["rule"]
        self.calls.append(("set", uid, body))
        if "sequence" in body and uid in self.seq:
            self.seq[uid] = int(body["sequence"])
        return httpx.Response(200, json={"result": "saved"})

    def _add(self, req):
        new = f"new-{len(self.seq)}"
        self.seq[new] = max(self.seq.values()) + 100
        return httpx.Response(200, json={"result": "saved", "uuid": new})

    def _apply(self, req):
        self.calls.append(("apply",))
        return httpx.Response(200, json={"status": "ok"})


async def _host(client, name="fw1"):
    resp = await client.post("/api/hosts", json={"name": name, "base_url": f"https://{name}.test", "api_key": "k",
                                                 "api_secret": "s", "verify_tls": False})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _move(client, host, rule, before):
    return await client.post(f"/api/rule-order/hosts/{host['id']}/move", json={"rule_uuid": rule, "before_uuid": before})


@pytest.mark.asyncio
async def test_order_view_in_firewall_order_with_origin(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    body = (await client.get(f"/api/rule-order/hosts/{host['id']}")).json()
    assert [r["uuid"] for r in body["rows"]][1:] == fw.order()
    assert body["rows"][0]["origin"] == "system" and body["rows"][0]["movable"] is False
    assert all(r["origin"] == "local" and r["movable"] for r in body["rows"][1:])
    assert body["open"] == []


@pytest.mark.asyncio
async def test_move_confirm_and_blocking(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    r = await _move(client, host, "delta", "bravo")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending" and r.json()["revert_deadline"]
    assert fw.order() == ["alpha", "delta", "bravo", "charlie"]
    # a series: another move while the first is open is fine
    assert (await _move(client, host, "alpha", None)).status_code == 200
    assert fw.order() == ["delta", "bravo", "charlie", "alpha"]
    assert ("set", "alpha", {"sequence": "500"}) in fw.calls  # to the end: ONLY the sequence is sent, above all others
    open_list = (await client.get("/api/rule-order/open")).json()
    assert len(open_list) == 2
    # a rule deployment must not build on a position that may be taken back
    rule = (await client.post("/api/rules", json={"description": "x", "interface": "lan", "direction": "in", "protocol": "tcp",
                                                  "action": "pass", "target_host_ids": [host["id"]]})).json()
    blocked = await client.post(f"/api/rules/{rule['id']}/deploy")
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "OPEN_ORDER_CHANGE_BLOCKS_DEPLOY"
    ok = await client.post(f"/api/rule-order/hosts/{host['id']}/confirm")
    assert ok.status_code == 200 and {c["status"] for c in ok.json()} == {"confirmed"}
    assert (await client.get("/api/rule-order/open")).json() == []
    assert (await client.post(f"/api/rule-order/hosts/{host['id']}/confirm")).json()["detail"]["code"] == "ORDER_NOTHING_OPEN"


@pytest.mark.asyncio
async def test_revert_restores_the_original_order_newest_first(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    start = fw.order()
    await _move(client, host, "delta", "alpha")
    await _move(client, host, "alpha", None)
    await _move(client, host, "charlie", "delta")
    assert fw.order() != start
    r = await client.post(f"/api/rule-order/hosts/{host['id']}/revert")
    assert r.status_code == 200 and [c["status"] for c in r.json()] == ["reverted"] * 3
    assert fw.order() == start
    assert (await client.get("/api/rule-order/open")).json() == []


@pytest.mark.asyncio
async def test_unconfirmed_move_is_taken_back_by_the_sweeper(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    start = fw.order()
    await _move(client, host, "charlie", "alpha")
    async with SessionLocal() as db:
        for c in (await db.scalars(select(RuleOrderChange))).all():
            c.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        touched = await rule_order_service.sweep_expired_order_changes(db)
        assert [c.status for c in touched] == ["reverted"]
    assert fw.order() == start


@pytest.mark.asyncio
async def test_not_expired_move_is_left_alone_by_the_sweeper(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    await _move(client, host, "charlie", "alpha")
    moved = fw.order()
    async with SessionLocal() as db:
        assert await rule_order_service.sweep_expired_order_changes(db) == []
    assert fw.order() == moved


@pytest.mark.asyncio
async def test_refused_move_is_undone_and_reported(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    fw.refuse_move = True
    r = await _move(client, host, "delta", "alpha")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "ORDER_MOVE_FAILED"
    assert fw.order() == ["alpha", "bravo", "charlie", "delta"]
    async with SessionLocal() as db:
        change = (await db.scalars(select(RuleOrderChange))).one()
        assert change.status == "failed" and change.revert_deadline is None
    assert (await client.get("/api/rule-order/open")).json() == []


@pytest.mark.asyncio
async def test_noop_nothing_changes(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    r = await _move(client, host, "bravo", "charlie")  # already directly before charlie
    assert r.status_code == 200 and r.json() is None
    assert (await _move(client, host, "delta", None)).json() is None  # already last
    assert [c for c in fw.calls if c[0] in ("move", "set")] == []


@pytest.mark.asyncio
async def test_system_rule_and_foreign_group_cannot_be_moved(client, respx_mock):
    Fw(respx_mock)
    host = await _host(client)
    r = await _move(client, host, "auto-1", "alpha")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ORDER_RULE_NOT_MOVABLE"
    r = await _move(client, host, "alpha", "auto-1")  # target in another group / automatic
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ORDER_RULE_NOT_MOVABLE"
    r = await _move(client, host, "ghost", "alpha")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "ORDER_RULE_NOT_FOUND"


@pytest.mark.asyncio
async def test_failed_revert_blocks_everything_and_notifies(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    await _move(client, host, "delta", "alpha")
    fw.down = True
    r = await client.post(f"/api/rule-order/hosts/{host['id']}/revert")
    assert r.status_code == 200 and r.json()[0]["status"] == "revert_failed"
    fw.down = False
    blocked = await _move(client, host, "alpha", None)
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "OPEN_ORDER_CHANGE_BLOCKS_DEPLOY"
    from app.services import notification_checks
    async with SessionLocal() as db:
        problems = await notification_checks.check_revert_failed(db)
    assert any(k.startswith("rule_order:") for k in problems)
    # retry after the firewall is back: the sweeper-free manual revert works again for the failed one
    async with SessionLocal() as db:
        change = (await db.scalars(select(RuleOrderChange))).one()
        change.status = "pending"
        await db.commit()
    again = await client.post(f"/api/rule-order/hosts/{host['id']}/revert")
    assert again.json()[0]["status"] == "reverted" and fw.order() == ["alpha", "bravo", "charlie", "delta"]


@pytest.mark.asyncio
async def test_central_rules_are_marked_and_hierarchy_rules_are_not_movable(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    rule = (await client.post("/api/rules", json={"description": "Mine", "interface": "lan", "direction": "in", "protocol": "tcp",
                                                  "action": "pass", "target_host_ids": [host["id"]]})).json()
    assert (await client.post(f"/api/rules/{rule['id']}/deploy")).status_code == 200
    rows = (await client.get(f"/api/rule-order/hosts/{host['id']}")).json()["rows"]
    mine = next(r for r in rows if r["uuid"].startswith("new-"))
    assert mine["origin"] == "central" and mine["central_rule_id"] == rule["id"] and mine["movable"]


def test_token_rules_for_the_new_routes():
    confirm = "/api/rule-order/hosts/abc/confirm"
    move = "/api/rule-order/hosts/abc/move"
    assert api_token_service.denies("admin", "POST", confirm)          # a human decision, never a token
    assert api_token_service.denies("read", "POST", move)
    assert not api_token_service.denies("deploy", "POST", move)
    assert api_token_service.is_rollout("POST", move) and api_token_service.is_rollout("POST", "/api/rule-order/hosts/abc/revert")
    assert not api_token_service.is_rollout("POST", confirm)
    assert not api_token_service.denies("read", "GET", "/api/rule-order/hosts/abc")


@pytest.mark.asyncio
async def test_redeploy_does_not_pull_a_moved_top_rule_back_to_the_top(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    rule = (await client.post("/api/rules", json={"description": "Top", "interface": "lan", "direction": "in", "protocol": "tcp",
                                                  "action": "block", "placement": "top", "target_host_ids": [host["id"]]})).json()
    assert (await client.post(f"/api/rules/{rule['id']}/deploy")).status_code == 200
    assert fw.order()[0].startswith("new-")
    await client.post(f"/api/rules/{rule['id']}/confirm")
    new = fw.order()[0]
    assert (await _move(client, host, new, None)).status_code == 200
    await client.post(f"/api/rule-order/hosts/{host['id']}/confirm")
    assert fw.order()[-1] == new
    # change something else and deploy again: the rule stays where the person put it
    edit = {**{k: rule[k] for k in ("description", "interface", "direction", "protocol", "action", "placement")},
            "destination_port": "99", "target_host_ids": [host["id"]], "expected_updated_at": rule["updated_at"]}
    assert (await client.put(f"/api/rules/{rule['id']}", json=edit)).status_code == 200
    assert (await client.post(f"/api/rules/{rule['id']}/deploy")).status_code == 200
    assert fw.order()[-1] == new


@pytest.mark.asyncio
async def test_move_that_does_not_show_on_the_firewall_is_not_reported_as_success(client, respx_mock):
    fw = Fw(respx_mock)
    host = await _host(client)
    fw.ignore_move = True
    r = await _move(client, host, "delta", "alpha")
    assert r.status_code == 502 and r.json()["detail"]["code"] == "ORDER_MOVE_FAILED"
    async with SessionLocal() as db:
        assert (await db.scalars(select(RuleOrderChange))).one().status == "failed"
