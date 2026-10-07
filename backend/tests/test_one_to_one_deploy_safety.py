import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.one_to_one_deployment import OneToOneDeployment
from app.services.one_to_one_deploy_service import sweep_expired_one_to_one_deployments

from tests.test_rules import _create_host
from tests.test_one_to_one import _nat_payload, _mock_nat_push


@pytest.mark.asyncio
async def test_nat_deploy_starts_in_pending_state(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_nat_push(respx_mock, "https://fw1.test")

    resp = await client.post(f"/api/one-to-one/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "pending"
    assert dep["revert_deadline"] is not None


@pytest.mark.asyncio
async def test_nat_confirm_makes_deployment_permanent(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_nat_push(respx_mock, "https://fw1.test")
    await client.post(f"/api/one-to-one/{rule_id}/deploy")

    resp = await client.post(f"/api/one-to-one/{rule_id}/confirm")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert dep["revert_deadline"] is None


@pytest.mark.asyncio
async def test_nat_redeploy_unchanged_payload_skips_pending(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))
    rule_id = resp.json()["id"]

    add_route = respx_mock.post("https://fw1.test/api/firewall/one_to_one/addRule").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "pf-uuid-1"})
    )
    respx_mock.post("https://fw1.test/api/firewall/one_to_one/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))

    await client.post(f"/api/one-to-one/{rule_id}/deploy")
    await client.post(f"/api/one-to-one/{rule_id}/confirm")

    resp = await client.post(f"/api/one-to-one/{rule_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert dep["revert_deadline"] is None
    assert add_route.call_count == 1


@pytest.mark.asyncio
async def test_nat_sweep_reverts_fresh_deploy_by_deleting_it(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_nat_push(respx_mock, "https://fw1.test", uuid_="pf-uuid-1")
    await client.post(f"/api/one-to-one/{rule_id}/deploy")

    del_route = respx_mock.post("https://fw1.test/api/firewall/one_to_one/delRule/pf-uuid-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )

    async with SessionLocal() as db:
        deployment = (await db.execute(select(OneToOneDeployment))).scalar_one()
        deployment.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        await sweep_expired_one_to_one_deployments(db)

        remaining = (await db.execute(select(OneToOneDeployment))).scalar_one_or_none()
        assert remaining is None

    assert del_route.called


@pytest.mark.asyncio
async def test_nat_sweep_reverts_redeploy_by_restoring_previous_payload(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))
    rule_id = resp.json()["id"]

    old_payload = {"descr": "old", "external": "10.0.1.10"}
    new_payload = {"descr": "new", "external": "10.0.1.20"}

    set_route = respx_mock.post("https://fw1.test/api/firewall/one_to_one/setRule/existing-uuid").mock(
        return_value=httpx.Response(200, json={"result": "saved"})
    )
    respx_mock.post("https://fw1.test/api/firewall/one_to_one/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))

    async with SessionLocal() as db:
        deployment = OneToOneDeployment(
            one_to_one_id=uuid.UUID(rule_id),
            host_id=uuid.UUID(host["id"]),
            status="pending",
            opnsense_one_to_one_uuid="existing-uuid",
            previous_payload=old_payload,
            last_payload=new_payload,
            revert_deadline=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        db.add(deployment)
        await db.commit()

        reverted = await sweep_expired_one_to_one_deployments(db)
        assert len(reverted) == 1
        assert reverted[0].status == "reverted"
        assert reverted[0].last_payload == old_payload

    assert set_route.called
    sent = json.loads(set_route.calls.last.request.content)
    assert sent["rule"] == old_payload


@pytest.mark.asyncio
async def test_nat_sweep_marks_revert_failed_when_host_unreachable(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/one-to-one", json=_nat_payload([host["id"]]))
    rule_id = resp.json()["id"]

    _mock_nat_push(respx_mock, "https://fw1.test", uuid_="pf-uuid-1")
    await client.post(f"/api/one-to-one/{rule_id}/deploy")

    respx_mock.post("https://fw1.test/api/firewall/one_to_one/delRule/pf-uuid-1").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    async with SessionLocal() as db:
        deployment = (await db.execute(select(OneToOneDeployment))).scalar_one()
        deployment.revert_deadline = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.commit()

        reverted = await sweep_expired_one_to_one_deployments(db)
        assert len(reverted) == 1
        assert reverted[0].status == "revert_failed"
        assert reverted[0].message

        still_there = (await db.execute(select(OneToOneDeployment))).scalar_one_or_none()
        assert still_there is not None
        assert still_there.status == "revert_failed"
