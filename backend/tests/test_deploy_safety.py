import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.rule_deployment import RuleDeployment
from app.services.rule_deploy_service import sweep_expired_deployments


async def _create_host(client, name: str, base_url: str) -> dict:
    resp = await client.post(
        "/api/hosts",
        json={"name": name, "base_url": base_url, "api_key": "key", "api_secret": "secret", "verify_tls": False},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _rule_payload(target_host_ids: list[str], **overrides) -> dict:
    payload = {
        "description": "Allow SSH",
        "interface": "lan",
        "direction": "in",
        "protocol": "tcp",
        "action": "pass",
        "source_net": "any",
        "destination_net": "lan",
        "destination_port": "22",
        "target_host_ids": target_host_ids,
    }
    payload.update(overrides)
    return payload


def _mock_deploy_success(respx_mock, base_url: str, uuid_: str = "abc-123"):
    respx_mock.post(f"{base_url}/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": uuid_})
    )
    respx_mock.post(f"{base_url}/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))


@pytest.mark.asyncio
async def test_deploy_starts_in_pending_state(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_deploy_success(respx_mock, "https://fw1.test")

    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "pending"
    assert dep["revert_deadline"] is not None


@pytest.mark.asyncio
async def test_confirm_makes_deployment_permanent(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_deploy_success(respx_mock, "https://fw1.test")
    await client.post(f"/api/rules/{rule_id}/deploy")

    resp = await client.post(f"/api/rules/{rule_id}/confirm")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert dep["revert_deadline"] is None


@pytest.mark.asyncio
async def test_redeploy_unchanged_payload_skips_pending(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    rule_id = resp.json()["id"]

    add_route = respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "abc-123"})
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))

    await client.post(f"/api/rules/{rule_id}/deploy")
    await client.post(f"/api/rules/{rule_id}/confirm")

    # Same rule, same targets, nothing changed — deploying again must not
    # touch OPNsense at all (addRule called exactly once) and must go
    # straight back to "success" with no new pending window.
    resp = await client.post(f"/api/rules/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert dep["revert_deadline"] is None
    assert add_route.call_count == 1


@pytest.mark.asyncio
async def test_sweep_reverts_fresh_deploy_by_deleting_it(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_deploy_success(respx_mock, "https://fw1.test", uuid_="abc-123")
    await client.post(f"/api/rules/{rule_id}/deploy")

    del_route = respx_mock.post("https://fw1.test/api/firewall/filter/delRule/abc-123").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    async with SessionLocal() as db:
        deployment = (await db.execute(select(RuleDeployment))).scalar_one()
        deployment.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        await sweep_expired_deployments(db)

        remaining = (await db.execute(select(RuleDeployment))).scalar_one_or_none()
        assert remaining is None

    assert del_route.called


@pytest.mark.asyncio
async def test_sweep_reverts_redeploy_by_restoring_previous_payload(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    rule_id = resp.json()["id"]

    old_payload = {"description": "old", "action": "pass"}
    new_payload = {"description": "new", "action": "block"}

    set_route = respx_mock.post("https://fw1.test/api/firewall/filter/setRule/existing-uuid").mock(
        return_value=httpx.Response(200, json={"result": "saved"})
    )
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))

    async with SessionLocal() as db:
        deployment = RuleDeployment(
            rule_id=uuid.UUID(rule_id),
            host_id=uuid.UUID(host["id"]),
            status="pending",
            opnsense_rule_uuid="existing-uuid",
            previous_payload=old_payload,
            last_payload=new_payload,
            revert_deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        db.add(deployment)
        await db.commit()

        reverted = await sweep_expired_deployments(db)
        assert len(reverted) == 1
        assert reverted[0].status == "reverted"
        assert reverted[0].last_payload == old_payload

    assert set_route.called
    sent = json.loads(set_route.calls.last.request.content)
    assert sent["rule"] == old_payload


@pytest.mark.asyncio
async def test_sweep_marks_revert_failed_when_host_unreachable(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/rules", json=_rule_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_deploy_success(respx_mock, "https://fw1.test", uuid_="abc-123")
    await client.post(f"/api/rules/{rule_id}/deploy")

    respx_mock.post("https://fw1.test/api/firewall/filter/delRule/abc-123").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with SessionLocal() as db:
        deployment = (await db.execute(select(RuleDeployment))).scalar_one()
        deployment.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        reverted = await sweep_expired_deployments(db)
        assert len(reverted) == 1
        assert reverted[0].status == "revert_failed"
        assert reverted[0].message

        # The row must survive a failed revert — it needs human attention,
        # not to silently vanish.
        still_there = (await db.execute(select(RuleDeployment))).scalar_one_or_none()
        assert still_there is not None
        assert still_there.status == "revert_failed"


# ── A retry after "written, apply failed" must keep the ORIGINAL rollback target (audit 2026-10-23) ──

def _patch_rule_calls(monkeypatch, *, apply_results):
    from unittest.mock import AsyncMock
    from app.services import opnsense_client
    calls = {
        "add": AsyncMock(return_value={"uuid": "rule-uuid-1"}),
        "set": AsyncMock(return_value={}),
        "delete": AsyncMock(return_value={}),
        "apply": AsyncMock(side_effect=apply_results),
    }
    monkeypatch.setattr(opnsense_client, "add_rule", calls["add"])
    monkeypatch.setattr(opnsense_client, "set_rule", calls["set"])
    monkeypatch.setattr(opnsense_client, "del_rule", calls["delete"])
    monkeypatch.setattr(opnsense_client, "apply", calls["apply"])
    return calls


@pytest.mark.asyncio
async def test_retry_after_failed_apply_still_removes_a_first_installation(client, respx_mock, monkeypatch):
    from app.services.opnsense_client import OpnsenseAPIError
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    rule_id = (await client.post("/api/rules", json=_rule_payload([host["id"]]))).json()["id"]
    calls = _patch_rule_calls(monkeypatch, apply_results=[OpnsenseAPIError("apply failed"), {}, {}])

    assert (await client.post(f"/api/rules/{rule_id}/deploy")).status_code == 200
    async with SessionLocal() as db:
        dep = (await db.execute(select(RuleDeployment))).scalar_one()
        assert dep.status == "error" and dep.revert_deadline is not None and dep.previous_payload is None

    assert (await client.post(f"/api/rules/{rule_id}/deploy")).status_code == 200
    async with SessionLocal() as db:
        dep = (await db.execute(select(RuleDeployment))).scalar_one()
        assert dep.status == "pending"
        assert dep.previous_payload is None  # the rollback is still "remove the rule"
        dep.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        await sweep_expired_deployments(db)
        assert (await db.execute(select(RuleDeployment))).scalar_one_or_none() is None

    assert calls["delete"].call_count == 1
    assert calls["delete"].call_args.args[1] == "rule-uuid-1"


@pytest.mark.asyncio
async def test_retry_after_failed_apply_restores_the_confirmed_payload_of_an_existing_rule(client, respx_mock, monkeypatch):
    from app.services.opnsense_client import OpnsenseAPIError
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    rule_id = (await client.post("/api/rules", json=_rule_payload([host["id"]]))).json()["id"]
    confirmed = {"description": "confirmed before", "action": "pass"}
    async with SessionLocal() as db:
        db.add(RuleDeployment(
            rule_id=uuid.UUID(rule_id), host_id=uuid.UUID(host["id"]), status="success",
            opnsense_rule_uuid="existing-uuid", last_payload=confirmed,
        ))
        await db.commit()
    calls = _patch_rule_calls(monkeypatch, apply_results=[OpnsenseAPIError("apply failed"), {}, {}])

    assert (await client.post(f"/api/rules/{rule_id}/deploy")).status_code == 200
    assert (await client.post(f"/api/rules/{rule_id}/deploy")).status_code == 200
    async with SessionLocal() as db:
        dep = (await db.execute(select(RuleDeployment))).scalar_one()
        assert dep.status == "pending"
        assert dep.previous_payload == confirmed
        assert dep.last_payload != confirmed
        dep.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        await sweep_expired_deployments(db)

    assert calls["delete"].call_count == 0
    assert calls["set"].call_args.args[2] == confirmed  # the last set_rule is the rollback to the confirmed payload


@pytest.mark.asyncio
async def test_an_armed_error_row_blocks_cleanup_and_maintenance_like_a_pending_one(client, respx_mock, monkeypatch):
    from app.models.rule_deployment import RuleDeployment as Dep
    from app.services.deploy_flow import is_unsettled, unsettled
    now = datetime.now(timezone.utc)
    assert is_unsettled(Dep(status="error", revert_deadline=now + timedelta(minutes=1)))
    assert not is_unsettled(Dep(status="error", revert_deadline=None))
    assert not is_unsettled(Dep(status="success", revert_deadline=None))
    assert is_unsettled(Dep(status="revert_failed", revert_deadline=None))
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    rule_id = (await client.post("/api/rules", json=_rule_payload([host["id"]]))).json()["id"]
    async with SessionLocal() as db:
        db.add(Dep(rule_id=uuid.UUID(rule_id), host_id=uuid.UUID(host["id"]), status="error",
                   opnsense_rule_uuid="u", last_payload={"a": 1}, revert_deadline=now + timedelta(minutes=1)))
        await db.commit()
        assert await db.scalar(select(Dep.id).where(unsettled(Dep)))


@pytest.mark.asyncio
async def test_alias_retry_after_failed_reconfigure_still_removes_a_first_installation(client, respx_mock, monkeypatch):
    from unittest.mock import AsyncMock
    from app.models.alias_deployment import AliasDeployment
    from app.services import opnsense_client
    from app.services.alias_deploy_service import sweep_expired_alias_deployments
    from app.services.opnsense_client import OpnsenseAPIError
    from tests.test_aliases import _alias_payload
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    alias_id = (await client.post("/api/aliases", json=_alias_payload([host["id"]]))).json()["id"]
    delete = AsyncMock(return_value={})
    monkeypatch.setattr(opnsense_client, "add_alias_item", AsyncMock(return_value={"uuid": "alias-uuid-1"}))
    monkeypatch.setattr(opnsense_client, "set_alias_item", AsyncMock(return_value={}))
    monkeypatch.setattr(opnsense_client, "del_alias_item", delete)
    monkeypatch.setattr(opnsense_client, "reconfigure_aliases", AsyncMock(side_effect=[OpnsenseAPIError("reconfigure failed"), {}, {}]))

    assert (await client.post(f"/api/aliases/{alias_id}/deploy")).status_code == 200
    assert (await client.post(f"/api/aliases/{alias_id}/deploy")).status_code == 200
    async with SessionLocal() as db:
        dep = (await db.execute(select(AliasDeployment))).scalar_one()
        assert dep.previous_payload is None
        dep.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()
        await sweep_expired_alias_deployments(db)
    assert delete.call_count == 1
