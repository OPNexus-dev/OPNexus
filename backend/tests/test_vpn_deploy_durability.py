"""VPN deploys write several objects one after the other and the firewall hands out a UUID for each. A cancelled or
crashed request must not forget the UUIDs already handed out (retry would duplicate them), and two deploys or removals
of the same VPN must not run side by side (audit 2026-10-23, AUD-04)."""
import asyncio
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.host import OpnsenseHost
from app.models.ipsec_tunnel import IpsecTunnel
from app.models.ipsec_tunnel_deployment import IpsecTunnelDeployment
from app.models.openvpn_instance import OpenVpnInstance
from app.models.openvpn_instance_deployment import OpenVpnInstanceDeployment
from app.models.wireguard_tunnel import WireguardTunnel
from app.models.wireguard_tunnel_deployment import WireguardTunnelDeployment
from app.services import ipsec_deploy_service, opnsense_client, openvpn_deploy_service, wireguard_deploy_service
from app.services.deploy_lease import VPN_INTERRUPTED
from tests import test_ipsec, test_openvpn, test_wireguard


class Kind:
    def __init__(self, name, api, payload, model, deployment_model, service, deploy, create_calls, later_call, set_calls, uuids):
        self.name, self.api, self.payload, self.model, self.deployment_model = name, api, payload, model, deployment_model
        self.service, self.deploy = service, deploy
        self.create_calls, self.later_call, self.set_calls, self.uuids = create_calls, later_call, set_calls, uuids


KINDS = {
    "ipsec": Kind(
        "ipsec", "/api/ipsec/tunnels", test_ipsec._tunnel_payload, IpsecTunnel, IpsecTunnelDeployment, ipsec_deploy_service,
        "deploy_tunnel",
        # (function, uuid it returns) in write order; the second one is where the request gets cancelled
        [("add_ipsec_connection", "conn-1"), ("add_ipsec_local", None), ("add_ipsec_remote", None), ("add_ipsec_child", None), ("add_ipsec_psk", None)],
        "add_ipsec_local",
        ["set_ipsec_connection", "set_ipsec_local", "set_ipsec_remote", "set_ipsec_child", "set_ipsec_psk"],
        {"opnsense_connection_uuid": "conn-1"},
    ),
    "wireguard": Kind(
        "wireguard", "/api/wireguard/tunnels", test_wireguard._tunnel_payload, WireguardTunnel, WireguardTunnelDeployment,
        wireguard_deploy_service, "deploy_tunnel",
        [("add_wireguard_server", "server-1"), ("add_wireguard_peer", None)], "add_wireguard_peer",
        ["set_wireguard_server", "set_wireguard_peer"], {"opnsense_server_uuid": "server-1"},
    ),
    "openvpn": Kind(
        "openvpn", "/api/openvpn/instances",
        lambda ids: test_openvpn._instance_payload(ids, tls_key_enabled=True, tls_key_material="0123456789abcdef" * 8),
        OpenVpnInstance, OpenVpnInstanceDeployment, openvpn_deploy_service, "deploy_instance",
        [("add_openvpn_static_key", "statickey-1"), ("add_openvpn_instance", None)], "add_openvpn_instance",
        ["set_openvpn_static_key", "set_openvpn_instance"], {"opnsense_statickey_uuid": "statickey-1"},
    ),
}


async def _setup(client, respx_mock, kind: Kind):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await client.post("/api/hosts", json={"name": "vpn-fw", "base_url": "https://vpn-fw.test", "api_key": "k", "api_secret": "s", "verify_tls": False})
    assert host.status_code == 201, host.text
    host = host.json()
    made = await client.post(kind.api, json=kind.payload([host["id"]]))
    assert made.status_code == 201, made.text
    return host["id"], uuid.UUID(made.json()["id"])


async def _deploy(kind: Kind, object_id, host_id):
    async with SessionLocal() as db:
        obj = await db.get(kind.model, object_id)
        host = await db.get(OpnsenseHost, uuid.UUID(host_id))
        return await getattr(kind.service, kind.deploy)(db, obj, [host])


def _patch_writes(monkeypatch, kind: Kind, *, cancel_at_later_call: bool):
    mocks = {}
    for name, returned in kind.create_calls:
        if name == kind.later_call and cancel_at_later_call:
            mocks[name] = AsyncMock(side_effect=asyncio.CancelledError())
        else:
            mocks[name] = AsyncMock(return_value={"uuid": returned or f"{name}-uuid"})
        monkeypatch.setattr(opnsense_client, name, mocks[name])
    for name in kind.set_calls:
        mocks[name] = AsyncMock(return_value={})
        monkeypatch.setattr(opnsense_client, name, mocks[name])
    return mocks


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(KINDS))
async def test_cancellation_keeps_the_uuids_already_handed_out_and_a_retry_continues_with_them(client, respx_mock, monkeypatch, name):
    kind = KINDS[name]
    host_id, object_id = await _setup(client, respx_mock, kind)
    mocks = _patch_writes(monkeypatch, kind, cancel_at_later_call=True)
    # Keep the rest of a deploy quiet: enable checks / vpnid lookup / reconfigure are not what this test is about.
    for attr in ("ensure_ipsec_enabled", "ensure_wireguard_enabled", "reconfigure_ipsec", "reconfigure_openvpn", "reconfigure_wireguard"):
        monkeypatch.setattr(opnsense_client, attr, AsyncMock(return_value={}))
    monkeypatch.setattr(opnsense_client, "search_openvpn_instances", AsyncMock(return_value={"rows": []}))

    with pytest.raises(asyncio.CancelledError):
        await _deploy(kind, object_id, host_id)

    async with SessionLocal() as db:
        row = (await db.execute(select(kind.deployment_model))).scalar_one()  # the row exists although the request died
        assert row.status == "error" and row.message == VPN_INTERRUPTED
        for attr, expected in kind.uuids.items():
            assert getattr(row, attr) == expected

    # Retry: the object that already exists is updated, not created a second time.
    _patch_writes(monkeypatch, kind, cancel_at_later_call=False)
    first_create = kind.create_calls[0][0]
    mocks_after = {n: opnsense_client.__dict__[n] for n in [first_create, kind.set_calls[0]]}
    await _deploy(kind, object_id, host_id)
    assert mocks_after[first_create].call_count == 0
    assert mocks_after[kind.set_calls[0]].call_count == 1
    async with SessionLocal() as db:
        row = (await db.execute(select(kind.deployment_model))).scalar_one()
        assert row.status == "success" and row.message is None


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(KINDS))
async def test_a_second_deploy_or_removal_of_the_same_vpn_is_refused_while_one_runs(client, respx_mock, monkeypatch, name):
    from fastapi import HTTPException
    kind = KINDS[name]
    host_id, object_id = await _setup(client, respx_mock, kind)
    entered, release = asyncio.Event(), asyncio.Event()
    first_create = kind.create_calls[0][0]

    async def slow_first_write(host, payload):
        entered.set()
        await release.wait()
        return {"uuid": "slow-uuid"}

    _patch_writes(monkeypatch, kind, cancel_at_later_call=False)
    monkeypatch.setattr(opnsense_client, first_create, slow_first_write)
    for attr in ("ensure_ipsec_enabled", "ensure_wireguard_enabled", "reconfigure_ipsec", "reconfigure_openvpn", "reconfigure_wireguard"):
        monkeypatch.setattr(opnsense_client, attr, AsyncMock(return_value={}))
    monkeypatch.setattr(opnsense_client, "search_openvpn_instances", AsyncMock(return_value={"rows": []}))

    running = asyncio.create_task(_deploy(kind, object_id, host_id))
    try:
        await asyncio.wait_for(entered.wait(), 10)
        with pytest.raises(HTTPException) as again:
            await asyncio.wait_for(_deploy(kind, object_id, host_id), 5)  # without the lease this would wait for the first one
        assert again.value.status_code == 409 and again.value.detail["code"] == "VPN_DEPLOY_LAEUFT_BEREITS"
        async with SessionLocal() as db:
            obj = await db.get(kind.model, object_id)
            undeploy = getattr(kind.service, "undeploy_instance" if name == "openvpn" else "undeploy_tunnel")
            with pytest.raises(HTTPException) as removal:
                await asyncio.wait_for(undeploy(db, obj), 5)
            assert removal.value.status_code == 409
    finally:
        release.set()
    await running

    # The lease is gone afterwards: the next deploy (a no-op re-affirmation or an update) is accepted again.
    await _deploy(kind, object_id, host_id)
