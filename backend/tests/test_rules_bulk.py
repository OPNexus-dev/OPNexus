"""Deploying and confirming several rules in one request (POST /api/rules/bulk/deploy, /bulk/confirm).

The bulk endpoint must be exactly N single deploys: same preview approval, same checks, same pending window.
"""
import httpx
import pytest

from app.services import api_token_service

VIP = r".*/get_vip_status"


async def _host(client, name: str, base_url: str) -> dict:
    resp = await client.post("/api/hosts", json={"name": name, "base_url": base_url, "api_key": "key",
                                                  "api_secret": "secret", "verify_tls": False})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _rule(client, targets: list[str], description: str, port: str = "22") -> dict:
    resp = await client.post("/api/rules", json={
        "description": description, "interface": "lan", "direction": "in", "protocol": "tcp", "action": "pass",
        "source_net": "any", "destination_net": "lan", "destination_port": port, "target_host_ids": targets})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _item(client, rule: dict) -> dict:
    plan = (await client.get(f"/api/rules/{rule['id']}/preview")).json()
    return {"rule_id": rule["id"], "preview_token": plan["token"]}


def _firewall(respx_mock, base: str, uuids: list[str]):
    respx_mock.post(f"{base}/api/firewall/filter/addRule").mock(
        side_effect=[httpx.Response(200, json={"result": "saved", "uuid": u}) for u in uuids])
    respx_mock.post(f"{base}/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))


@pytest.mark.asyncio
async def test_bulk_deploy_deploys_every_rule_as_pending(client, respx_mock):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    rules = [await _rule(client, [host["id"]], f"Rule {i}", str(2000 + i)) for i in range(3)]
    _firewall(respx_mock, "https://fw1.test", ["u-1", "u-2", "u-3"])
    items = [await _item(client, r) for r in rules]

    resp = await client.post("/api/rules/bulk/deploy", json={"items": items})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["deployed"], body["attention"], body["failed"], body["skipped"]) == (3, 0, 0, 0)
    assert [r["rule_id"] for r in body["results"]] == [r["id"] for r in rules]  # order is kept
    for result in body["results"]:
        assert result["status"] == "deployed"
        assert [d["status"] for d in result["rule"]["deployments"]] == ["pending"]
        assert result["rule"]["deployments"][0]["revert_deadline"]  # auto-revert armed, as for a single deploy


@pytest.mark.asyncio
async def test_bulk_deploy_stale_token_fails_only_that_rule(client, respx_mock):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    good, stale = await _rule(client, [host["id"]], "good", "80"), await _rule(client, [host["id"]], "stale", "81")
    _firewall(respx_mock, "https://fw1.test", ["u-1"])
    items = [await _item(client, good), {**await _item(client, stale), "preview_token": "0" * 64}]

    body = (await client.post("/api/rules/bulk/deploy", json={"items": items})).json()
    by_id = {r["rule_id"]: r for r in body["results"]}
    assert by_id[good["id"]]["status"] == "deployed"
    assert by_id[stale["id"]]["status"] == "failed"
    assert by_id[stale["id"]]["error"]["code"] == "ENTWURF_ODER_ZIELE_WURDEN_GEAENDERT_VORSCHAU_ERNEUT_OEFFNEN"
    assert (body["deployed"], body["failed"]) == (1, 1)
    # the refused rule never reached the firewall
    rules = {r["id"]: r for r in (await client.get("/api/rules")).json()}
    assert rules[stale["id"]]["deployments"] == []


@pytest.mark.asyncio
async def test_bulk_deploy_refuses_rule_with_open_confirmation(client, respx_mock):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    first, second = await _rule(client, [host["id"]], "first", "80"), await _rule(client, [host["id"]], "second", "81")
    _firewall(respx_mock, "https://fw1.test", ["u-1", "u-2"])
    assert (await client.post("/api/rules/bulk/deploy", json={"items": [await _item(client, first)]})).status_code == 200

    # `first` is still pending: it is refused again, `second` goes through
    items = [await _item(client, first), await _item(client, second)]
    body = (await client.post("/api/rules/bulk/deploy", json={"items": items})).json()
    by_id = {r["rule_id"]: r for r in body["results"]}
    assert by_id[first["id"]]["error"]["code"] == "OFFENES_DEPLOYMENT_ZUERST_BESTAETIGEN_ODER_ROLLBACK_KLAEREN"
    assert by_id[second["id"]]["status"] == "deployed"


@pytest.mark.asyncio
async def test_bulk_deploy_rule_without_targets_fails_alone(client, respx_mock):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    ok, empty = await _rule(client, [host["id"]], "ok", "80"), await _rule(client, [], "no targets", "81")
    _firewall(respx_mock, "https://fw1.test", ["u-1"])
    body = (await client.post("/api/rules/bulk/deploy", json={"items": [await _item(client, empty), await _item(client, ok)]})).json()
    by_id = {r["rule_id"]: r for r in body["results"]}
    assert by_id[empty["id"]]["error"]["code"] == "REGEL_HAT_KEINE_ZIEL_HOSTS"
    assert by_id[ok["id"]]["status"] == "deployed"


@pytest.mark.asyncio
async def test_bulk_deploy_reports_unreachable_target_as_attention(client, respx_mock):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    rule = await _rule(client, [host["id"]], "down", "80")
    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(return_value=httpx.Response(500, text="boom"))
    body = (await client.post("/api/rules/bulk/deploy", json={"items": [await _item(client, rule)]})).json()
    assert body["results"][0]["status"] == "attention"
    assert body["results"][0]["rule"]["deployments"][0]["message"]


@pytest.mark.asyncio
async def test_bulk_deploy_deduplicates_and_validates_request(client, respx_mock):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    rule = await _rule(client, [host["id"]], "once", "80")
    add = respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "u-1"}))
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    item = await _item(client, rule)

    body = (await client.post("/api/rules/bulk/deploy", json={"items": [item, item]})).json()
    assert len(body["results"]) == 1 and add.call_count == 1

    assert (await client.post("/api/rules/bulk/deploy", json={"items": []})).status_code == 422
    assert (await client.post("/api/rules/bulk/deploy", json={"items": [item] * 51})).status_code == 422
    assert (await client.post("/api/rules/bulk/deploy", json={"items": [{**item, "preview_token": "x"}]})).status_code == 422


@pytest.mark.asyncio
async def test_bulk_deploy_stops_on_unexpected_error_and_skips_the_rest(client, respx_mock, monkeypatch):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    rules = [await _rule(client, [host["id"]], f"r{i}", str(80 + i)) for i in range(3)]
    _firewall(respx_mock, "https://fw1.test", ["u-1", "u-2", "u-3"])
    items = [await _item(client, r) for r in rules]

    from app.routers import rules as router
    real = router.deploy_rule
    calls = {"n": 0}

    async def flaky(db, rule, hosts):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database went away")
        return await real(db, rule, hosts)

    monkeypatch.setattr(router, "deploy_rule", flaky)
    body = (await client.post("/api/rules/bulk/deploy", json={"items": items})).json()
    assert [r["status"] for r in body["results"]] == ["deployed", "failed", "skipped"]
    assert body["results"][1]["error"]["code"] == "BULK_DEPLOY_UNEXPECTED"


@pytest.mark.asyncio
async def test_bulk_confirm_confirms_all_pending(client, respx_mock):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    rules = [await _rule(client, [host["id"]], f"r{i}", str(80 + i)) for i in range(2)]
    _firewall(respx_mock, "https://fw1.test", ["u-1", "u-2"])
    await client.post("/api/rules/bulk/deploy", json={"items": [await _item(client, r) for r in rules]})

    resp = await client.post("/api/rules/bulk/confirm", json={"rule_ids": [r["id"] for r in rules]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["confirmed"] == 2
    for rule in (await client.get("/api/rules")).json():
        assert [d["status"] for d in rule["deployments"]] == ["success"]
        assert rule["deployments"][0]["revert_deadline"] is None
    assert [r["status"] for r in resp.json()["results"]] == ["deployed", "deployed"]
    # a second confirm finds nothing open: reported as skipped, not as a deployment
    again = (await client.post("/api/rules/bulk/confirm", json={"rule_ids": [r["id"] for r in rules]})).json()
    assert again["confirmed"] == 0 and [r["status"] for r in again["results"]] == ["skipped", "skipped"]


def test_bulk_deploy_counts_as_a_rollout_write_bulk_confirm_does_not():
    # separation of duties ("operators may not deploy") must cover the bulk path too
    assert api_token_service.is_rollout("POST", "/api/rules/bulk/deploy")
    assert not api_token_service.is_rollout("POST", "/api/rules/bulk/confirm")
    assert api_token_service.denies("read", "POST", "/api/rules/bulk/deploy")


@pytest.mark.asyncio
async def test_bulk_requests_are_not_audited_as_entity_bulk(client, respx_mock):
    respx_mock.get(url__regex=VIP).mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _host(client, "fw1", "https://fw1.test")
    rule = await _rule(client, [host["id"]], "audited", "80")
    _firewall(respx_mock, "https://fw1.test", ["u-1"])
    await client.post("/api/rules/bulk/deploy", json={"items": [await _item(client, rule)]})
    events = (await client.get("/api/audit?limit=200")).json()
    items = events["items"] if isinstance(events, dict) else events
    assert items and all(e.get("entity_id") != "bulk" for e in items)
    assert any(e["action"] == "deploy" and e["entity_id"] == rule["id"] for e in items)
