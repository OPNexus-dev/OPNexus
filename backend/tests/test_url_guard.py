"""S5 (1.29): the DNS-rebinding window between the URL check and the connection is closed. These tests open REAL
sockets (a local server) so that an httpx/httpcore upgrade that breaks the hook fails here, not silently."""
import asyncio
import ipaddress

import httpcore
import httpx
import pytest

from app.models.notification import NotificationChannel, NotificationProblem, NotificationSettings
from app.services import notification_delivery, oidc_service, url_guard
from app.services.notification_delivery import NotificationDeliveryError


class Server:
    """Local HTTP server on 127.0.0.1 that records connections and Host headers."""

    def __init__(self):
        self.connections = 0
        self.hosts: list[str] = []
        self.port = 0

    async def _handle(self, reader, writer):
        self.connections += 1
        head = await reader.readuntil(b"\r\n\r\n")
        for line in head.decode().split("\r\n"):
            if line.lower().startswith("host:"):
                self.hosts.append(line.split(":", 1)[1].strip())
        writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\nconnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


def resolver(*answers):
    """Resolver mock: answer i for call i (the last one repeats); records what it was asked."""
    calls = []

    async def fake(host, port=None):
        calls.append(host)
        try:
            ipaddress.ip_address(host)
            return [host]
        except ValueError:
            pass
        return list(answers[min(len(calls) - 1, len(answers) - 1)])
    fake.calls = calls
    return fake


@pytest.mark.asyncio
async def test_rebinding_public_then_private_is_refused(monkeypatch):
    async with Server() as server:
        monkeypatch.setattr(url_guard, "resolve_host", resolver(["203.0.113.9"], ["127.0.0.1"]))
        url = f"http://rebind.test:{server.port}/hook"
        await url_guard.ensure_resolved_safe(url)  # the early check sees the public answer ...
        async with url_guard.guarded_client(timeout=5) as client:
            with pytest.raises(httpx.ConnectError) as exc:
                await client.get(url)  # ... the connection resolves again, gets the private one and must not connect
        assert isinstance(exc.value.__cause__, url_guard.BlockedAddress)
        assert url_guard.failure_reason(exc.value) == url_guard.BLOCKED_MESSAGE
        assert server.connections == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [["127.0.0.1"], ["169.254.169.254"], ["::1"], ["0.0.0.0"], ["::ffff:127.0.0.1"],
                                    ["203.0.113.9", "127.0.0.1"], []])
async def test_any_forbidden_or_empty_answer_blocks_the_connection(monkeypatch, answer):
    async with Server() as server:
        monkeypatch.setattr(url_guard, "resolve_host", resolver(answer))
        async with url_guard.guarded_client(timeout=5) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://mixed.test:{server.port}/")
        assert server.connections == 0


@pytest.mark.asyncio
async def test_literal_loopback_is_refused_at_connection_time_too():
    async with Server() as server:
        async with url_guard.guarded_client(timeout=5) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"http://127.0.0.1:{server.port}/")
        assert server.connections == 0


@pytest.mark.asyncio
async def test_connects_to_the_checked_address_and_keeps_name_as_host(monkeypatch):
    """Private ranges stay allowed (self-hosted ntfy/Gotify); the request goes to exactly the address checked,
    the Host header keeps the name. (Loopback is only allowed here to have a socket to talk to.)"""
    async with Server() as server:
        monkeypatch.setattr(url_guard, "_forbidden", lambda ip: False)
        fake = resolver(["127.0.0.1"])
        monkeypatch.setattr(url_guard, "resolve_host", fake)
        async with url_guard.guarded_client(timeout=5) as client:
            resp = await client.get(f"http://ntfy.internal.test:{server.port}/topic")
        assert resp.status_code == 200 and resp.text == "ok"
        assert server.hosts == [f"ntfy.internal.test:{server.port}"]
        assert fake.calls == ["ntfy.internal.test"]  # one resolution, no second look-up inside the client


def test_cloud_metadata_addresses_outside_link_local_are_forbidden():
    for bad in ("fd00:ec2::254", "100.100.100.200", "169.254.169.254", "::ffff:100.100.100.200"):
        assert url_guard._any_forbidden([bad]), bad


@pytest.mark.asyncio
async def test_private_ranges_are_not_forbidden():
    for ok in ("10.20.30.40", "192.168.1.5", "172.16.0.9", "203.0.113.9", "2001:db8::1"):
        assert not url_guard._any_forbidden([ok])


@pytest.mark.asyncio
async def test_falls_back_to_the_next_checked_address(monkeypatch):
    async with Server() as server:
        monkeypatch.setattr(url_guard, "_forbidden", lambda ip: False)
        # The server listens on IPv4 only: "::1" is refused (or unreachable) and the next checked address is tried.
        monkeypatch.setattr(url_guard, "resolve_host", resolver(["::1", "127.0.0.1"]))
        async with url_guard.guarded_client(timeout=5) as client:
            resp = await client.get(f"http://dual.test:{server.port}/")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unresolvable_name_is_a_plain_connect_error(monkeypatch):
    async def fail(host, port=None):
        raise OSError("no such host")
    monkeypatch.setattr(url_guard, "resolve_host", fail)
    async with url_guard.guarded_client(timeout=5) as client:
        with pytest.raises(httpx.ConnectError) as exc:
            await client.get("http://nowhere.test/")
    assert not isinstance(exc.value.__cause__, url_guard.BlockedAddress)


def test_transport_hook_is_installed_and_proxy_env_ignored(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    client = url_guard.guarded_client()
    assert isinstance(client._transport._pool._network_backend, url_guard._GuardedBackend)
    assert not client._mounts  # no environment proxy mounted next to the guarded transport


@pytest.mark.asyncio
async def test_webhook_delivery_refuses_a_rebound_name(monkeypatch):
    async with Server() as server:
        monkeypatch.setattr(url_guard, "resolve_host", resolver(["203.0.113.9"], ["127.0.0.1"]))
        row = NotificationSettings(webhook_url=f"http://hooks.rebind.test:{server.port}/notify", webhook_signing_secret_set=False)
        problem = NotificationProblem(problem_type="host_offline", dedup_key="h1", context={"host_name": "fw1"})
        with pytest.raises(NotificationDeliveryError) as exc:
            await notification_delivery.send_webhook(row, problem, "opened")
        assert url_guard.BLOCKED_MESSAGE in str(exc.value) and str(server.port) not in str(exc.value)
        assert server.connections == 0


@pytest.mark.asyncio
async def test_channel_delivery_refuses_a_rebound_name(monkeypatch):
    from app.services import notification_channels
    async with Server() as server:
        monkeypatch.setattr(url_guard, "resolve_host", resolver(["203.0.113.9"], ["127.0.0.1"]))
        channel = NotificationChannel(name="ntfy", channel_type="ntfy", enabled=True, secret_set=False,
                                      config={"server_url": f"http://ntfy.rebind.test:{server.port}", "topic": "alerts"})
        problem = NotificationProblem(problem_type="host_offline", dedup_key="h1", context={"host_name": "fw1"})
        with pytest.raises(NotificationDeliveryError) as exc:
            await notification_channels.send(channel, problem, "opened")
        assert url_guard.BLOCKED_MESSAGE in str(exc.value)
        assert server.connections == 0


@pytest.mark.asyncio
async def test_oidc_discovery_uses_the_guard():
    async with Server() as server:
        with pytest.raises(oidc_service.OidcError) as exc:
            await oidc_service._get_json(f"https://127.0.0.1:{server.port}/.well-known/openid-configuration")
        assert exc.value.code == "provider"
        assert server.connections == 0  # refused before any connection was made
