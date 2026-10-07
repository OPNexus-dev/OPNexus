"""Covers WireguardTunnel's deliberately simpler status model (draft/
success/error only — see wireguard_deploy_service.py's module docstring)
— same rationale as test_ipsec_deploy_safety.py/test_openvpn_deploy_safety.py,
adapted to this entity's 2-object shape (Server, then Peer, linked via the
denormalized `servers` field on the Peer payload) and to WireGuard's OWN
reconfigure response shape ({"result": "ok"}, matching OpenVPN's shape,
NOT IPsec's {"status": "ok"}), which this suite checks explicitly rather
than assuming symmetry with either sibling feature."""
import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.wireguard_tunnel_deployment import WireguardTunnelDeployment
from tests.test_wireguard import _create_host, _tunnel_payload


def _mock_full_success(respx_mock, base_url: str):
    respx_mock.post(f"{base_url}/api/wireguard/server/addServer").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "server-1"})
    )
    respx_mock.post(f"{base_url}/api/wireguard/client/addClient").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "peer-1"})
    )
    respx_mock.post(f"{base_url}/api/wireguard/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )


@pytest.mark.asyncio
async def test_deploy_success_persists_server_and_peer_uuid(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")

    resp = await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert dep["message"] is None

    async with SessionLocal() as db:
        row = (await db.execute(select(WireguardTunnelDeployment))).scalar_one()
        assert row.opnsense_server_uuid == "server-1"
        assert row.opnsense_peer_uuid == "peer-1"
        assert row.last_payload is not None


@pytest.mark.asyncio
async def test_peer_payload_carries_injected_server_uuid(client, respx_mock):
    """The denormalized peer<->server linkage (verified directly from
    ClientController::setClientAction — see module docstring) is written
    by this app sending `servers` = the Server's own UUID on the Peer's
    add/set call. Locks in that this app actually injects it, in the
    correct order (Server created before Peer references it)."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    add_server = respx_mock.post("https://fw1.test/api/wireguard/server/addServer").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "server-1"})
    )
    add_peer = respx_mock.post("https://fw1.test/api/wireguard/client/addClient").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "peer-1"})
    )
    respx_mock.post("https://fw1.test/api/wireguard/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )

    resp = await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text

    assert add_server.called and add_peer.called
    peer_body = add_peer.calls.last.request.content
    import json as _json
    posted = _json.loads(peer_body)
    assert posted["client"]["servers"] == "server-1"


@pytest.mark.asyncio
async def test_deploy_config_write_failure_surfaces_as_error_without_rollback(client, respx_mock):
    """The Server add fails — status must be "error" with a concrete
    message, the Peer must never be written, and reconfigure must never be
    reached (config write itself failed, no rollback exists for this
    entity)."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/wireguard/server/addServer").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    peer_route = respx_mock.post("https://fw1.test/api/wireguard/client/addClient").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "peer-1"})
    )
    reconfigure_route = respx_mock.post("https://fw1.test/api/wireguard/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "ok"})
    )

    resp = await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"]
    assert not peer_route.called
    assert not reconfigure_route.called

    async with SessionLocal() as db:
        row = (await db.execute(select(WireguardTunnelDeployment))).scalar_one()
        assert row.opnsense_server_uuid is None
        assert row.opnsense_peer_uuid is None


@pytest.mark.asyncio
async def test_deploy_reconfigure_failure_uses_exact_specified_message(client, respx_mock):
    """Both config writes succeed, but the follow-up reconfigure call
    fails — the plan specifies the exact same non-generic message IPsec/
    OpenVPN use ("config written, reload unconfirmed" is a materially
    different failure mode from "config write itself failed")."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/wireguard/server/addServer").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "server-1"})
    )
    respx_mock.post("https://fw1.test/api/wireguard/client/addClient").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "peer-1"})
    )
    respx_mock.post("https://fw1.test/api/wireguard/service/reconfigure").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"] == "Konfiguration wurde geschrieben, Dienst-Reload aber nicht bestätigt — Erreichbarkeit manuell prüfen"

    async with SessionLocal() as db:
        row = (await db.execute(select(WireguardTunnelDeployment))).scalar_one()
        assert row.opnsense_server_uuid == "server-1"
        assert row.opnsense_peer_uuid == "peer-1"
        assert row.last_payload is not None


@pytest.mark.asyncio
async def test_reconfigure_checks_result_key_explicitly(client, respx_mock):
    """Regression guard locking in WireGuard's own reconfigure response
    shape: ServiceController::reconfigureAction (a hand-rolled override,
    read directly from source) reports failure via {"result": "failed"} —
    matching OpenVPN's "result"-based shape, NOT IPsec's "status"-based
    one. A mock returning {"result": "failed"} must be treated as an
    unconfirmed reconfigure with the same exact message as a
    transport-level failure, proving this app checks the actual key/value
    WireGuard's reconfigure reports through rather than assuming either
    sibling feature's shape."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    respx_mock.post("https://fw1.test/api/wireguard/server/addServer").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "server-1"})
    )
    respx_mock.post("https://fw1.test/api/wireguard/client/addClient").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "peer-1"})
    )
    respx_mock.post("https://fw1.test/api/wireguard/service/reconfigure").mock(
        return_value=httpx.Response(200, json={"result": "failed"})
    )

    resp = await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "error"
    assert dep["message"] == "Konfiguration wurde geschrieben, Dienst-Reload aber nicht bestätigt — Erreichbarkeit manuell prüfen"


@pytest.mark.asyncio
async def test_redeploy_unchanged_skips_opnsense_entirely(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")
    await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")

    add_route = respx_mock.post("https://fw1.test/api/wireguard/server/addServer")

    resp = await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    dep = resp.json()["deployments"][0]
    assert dep["status"] == "success"
    assert add_route.call_count == 1


@pytest.mark.asyncio
async def test_undeploy_deletes_peer_before_server(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]]))
    tunnel_id = resp.json()["id"]

    _mock_full_success(respx_mock, "https://fw1.test")
    await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")

    del_peer = respx_mock.post("https://fw1.test/api/wireguard/client/delClient/peer-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )
    del_server = respx_mock.post("https://fw1.test/api/wireguard/server/delServer/server-1").mock(
        return_value=httpx.Response(200, json={"result": "deleted"})
    )

    resp = await client.delete(f"/api/wireguard/tunnels/{tunnel_id}")
    assert resp.status_code == 204, resp.text
    assert del_peer.called and del_server.called


@pytest.mark.asyncio
async def test_secrets_never_persisted_and_rotation_is_not_a_no_op(client, respx_mock):
    """Regression test for a real bug found and fixed 2026-09-09:
    build_payloads() embeds the raw server private key / peer PSK for the
    wire request, and that exact dict used to also become
    `deployment.last_payload` verbatim — i.e. both secrets were persisted
    in plaintext into Postgres, contradicting wireguard_key_store.py's own
    "never touches the database, disk-only" docstring. It also meant a
    key ROTATION (re-uploading a new private key with every other field
    unchanged) was invisible to the no-op check, since the boolean-style
    "is a key present" state never changed — the redeploy would have
    silently skipped OPNsense entirely, leaving the OLD key live on the
    firewall while the UI kept showing "success". Fixed by storing a
    SHA-256 hash of each secret instead of the secret itself."""
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    original_key = "p" * 43 + "="
    resp = await client.post("/api/wireguard/tunnels", json=_tunnel_payload([host["id"]], server_private_key=original_key))
    assert resp.status_code == 201, resp.text
    tunnel = resp.json()
    tunnel_id = tunnel["id"]

    _mock_full_success(respx_mock, "https://fw1.test")
    resp = await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deployments"][0]["status"] == "success"

    async with SessionLocal() as db:
        deployment = (await db.execute(
            select(WireguardTunnelDeployment).where(WireguardTunnelDeployment.tunnel_id == tunnel_id)
        )).scalar_one()
        stored = deployment.last_payload
        assert original_key not in str(stored), "raw private key must never be persisted in last_payload"
        assert stored["server"]["privkey"] != original_key
        assert stored["server"]["privkey"], "a hash stand-in must still be present so rotation is detectable"

    add_server_route = respx_mock.post("https://fw1.test/api/wireguard/server/addServer")
    set_server_route = respx_mock.post("https://fw1.test/api/wireguard/server/setServer/server-1").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "server-1"})
    )
    respx_mock.post("https://fw1.test/api/wireguard/client/setClient/peer-1").mock(
        return_value=httpx.Response(200, json={"result": "saved", "uuid": "peer-1"})
    )

    rotated_key = "q" * 43 + "="
    resp = await client.put(
        f"/api/wireguard/tunnels/{tunnel_id}",
        json={**_tunnel_payload([host["id"]], server_private_key=rotated_key), "expected_updated_at": tunnel["updated_at"]},
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(f"/api/wireguard/tunnels/{tunnel_id}/deploy")
    assert resp.status_code == 200, resp.text
    assert resp.json()["deployments"][0]["status"] == "success"
    assert add_server_route.call_count == 1, "unchanged from the first deploy — the rotation must go through setServer, not addServer"
    assert set_server_route.call_count == 1, "the rotated key must actually be re-pushed, not silently no-op'd"
    assert rotated_key.encode() in set_server_route.calls.last.request.content
