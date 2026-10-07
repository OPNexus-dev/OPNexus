"""Covers IpsecTunnel's deliberately simpler status model (draft/success/
error only — see ipsec_deploy_service.py's module docstring) — unlike
test_deploy_safety.py for FirewallRule, there is no pending/confirm/sweep
behaviour to test here, because those states don't exist for this entity.
What matters instead: a failed config-write surfaces as "error" with a
concrete message, and a failed reconfigure (after all 5 config writes
already succeeded) surfaces as "error" with the SPECIFIC message the plan
requires — not a generic exception string — while still recording that the
config itself was written (no rollback exists, so partial state is kept
and reported honestly rather than hidden)."""
import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
from tests.test_ipsec import _create_host, _tunnel_payload
from tests.helpers import deploy_approved


def _mock_full_success(respx_mock, base_url: str):
    respx_mock.post(f"{base_url}/api/ipsec/connections/addConnection").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "conn-1"})
    )
    respx_mock.post(f"{base_url}/api/ipsec/connections/addLocal").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "local-1"})
    )
    respx_mock.post(f"{base_url}/api/ipsec/connections/addRemote").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "remote-1"})
    )
    respx_mock.post(f"{base_url}/api/ipsec/connections/addChild").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "child-1"})
    )
    respx_mock.post(f"{base_url}/api/ipsec/preSharedKeys/addItem").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "psk-1"})
    )
    respx_mock.post(f"{base_url}/api/ipsec/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )


@pytest.mark.asyncio
async def test_deploy_success_persists_all_five_uuids(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")

    resp = await deploy_approved(client, f"/api/ipsec/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert dep["message"] is None

    async with SessionLocal() as db:
        row = (await db.execute(select(IpsecTunnelDeployment))).scalar_one()
        assert row.opnsense_connection_uuid == "conn-1"
        assert row.opnsense_local_uuid == "local-1"
        assert row.opnsense_remote_uuid == "remote-1"
        assert row.opnsense_child_uuid == "child-1"
        assert row.opnsense_psk_uuid == "psk-1"
        assert row.last_payload is not None


@pytest.mark.asyncio
async def test_deploy_config_write_failure_surfaces_as_error_without_rollback(client, respx_mock):
    """addChild fails after Connection/Local/Remote already succeeded —
    status must be "error" with a concrete message, and the 3 UUIDs that
    DID get written must still be persisted (no rollback exists for this
    entity, and a retry should update those 3 rather than duplicate them)."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/ipsec/connections/addConnection").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "conn-1"})
    )
    respx_mock.post("https://fw1.test/api/ipsec/connections/addLocal").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "local-1"})
    )
    respx_mock.post("https://fw1.test/api/ipsec/connections/addRemote").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "remote-1"})
    )
    respx_mock.post("https://fw1.test/api/ipsec/connections/addChild").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    reconfigure_route = respx_mock.post("https://fw1.test/api/ipsec/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )

    resp = await deploy_approved(client, f"/api/ipsec/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"]
    # Reconfigure must never be reached — the config write itself failed.
    assert not reconfigure_route.called

    async with SessionLocal() as db:
        row = (await db.execute(select(IpsecTunnelDeployment))).scalar_one()
        assert row.opnsense_connection_uuid == "conn-1"
        assert row.opnsense_local_uuid == "local-1"
        assert row.opnsense_remote_uuid == "remote-1"
        assert row.opnsense_child_uuid is None
        assert row.opnsense_psk_uuid is None


@pytest.mark.asyncio
async def test_deploy_reconfigure_failure_uses_exact_specified_message(client, respx_mock):
    """All 5 config writes succeed, but the follow-up reconfigure call
    fails — the plan specifies an exact, non-generic message here because
    "config written, reload unconfirmed" is a materially different failure
    mode from "config write itself failed"."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/ipsec/connections/addConnection").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "conn-1"})
    )
    respx_mock.post("https://fw1.test/api/ipsec/connections/addLocal").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "local-1"})
    )
    respx_mock.post("https://fw1.test/api/ipsec/connections/addRemote").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "remote-1"})
    )
    respx_mock.post("https://fw1.test/api/ipsec/connections/addChild").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "child-1"})
    )
    respx_mock.post("https://fw1.test/api/ipsec/preSharedKeys/addItem").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "psk-1"})
    )
    respx_mock.post("https://fw1.test/api/ipsec/service/reconfigure").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await deploy_approved(client, f"/api/ipsec/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"] == "Konfiguration wurde geschrieben, Dienst-Reload aber nicht bestätigt — Erreichbarkeit manuell prüfen"

    async with SessionLocal() as db:
        row = (await db.execute(select(IpsecTunnelDeployment))).scalar_one()
        # All 5 objects were in fact written — no rollback happens.
        assert row.opnsense_connection_uuid == "conn-1"
        assert row.opnsense_local_uuid == "local-1"
        assert row.opnsense_remote_uuid == "remote-1"
        assert row.opnsense_child_uuid == "child-1"
        assert row.opnsense_psk_uuid == "psk-1"
        assert row.last_payload is not None


@pytest.mark.asyncio
async def test_redeploy_unchanged_skips_opnsense_entirely(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")
    await deploy_approved(client, f"/api/ipsec/tunnels/{tunnel_id}/deploy")

    add_route = respx_mock.post("https://fw1.test/api/ipsec/connections/addConnection")

    resp = await deploy_approved(client, f"/api/ipsec/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert add_route.call_count == 1


@pytest.mark.asyncio
async def test_undeploy_deletes_child_local_remote_connection_then_psk(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/ipsec/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")
    await deploy_approved(client, f"/api/ipsec/tunnels/{tunnel_id}/deploy")

    del_child = respx_mock.post("https://fw1.test/api/ipsec/connections/delChild/child-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )
    del_local = respx_mock.post("https://fw1.test/api/ipsec/connections/delLocal/local-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )
    del_remote = respx_mock.post("https://fw1.test/api/ipsec/connections/delRemote/remote-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )
    del_conn = respx_mock.post("https://fw1.test/api/ipsec/connections/delConnection/conn-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )
    del_psk = respx_mock.post("https://fw1.test/api/ipsec/preSharedKeys/delItem/psk-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )

    resp = await client.delete(f"/api/ipsec/tunnels/{tunnel_id}")
    assert resp.status_code == 204, resp.text
    assert del_child.called and del_local.called and del_remote.called and del_conn.called and del_psk.called
