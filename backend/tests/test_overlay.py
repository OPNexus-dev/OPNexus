"""Management overlay: keys, addressing, hub config, the hub's proxy, and routing requests through it."""
import asyncio
import ipaddress
import json
import os
import ssl
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.config import settings
from app.overlay_hub import Hub, parse_dump, wg_conf
from app.services import overlay_service as svc
from tests.test_api_tokens import _login


@pytest.fixture(autouse=True)
def _clean_overlay():
    root = Path(settings.overlay_dir)
    if root.exists():
        for f in root.iterdir():
            f.unlink()
    svc._active_network = None
    yield
    svc._active_network = None


# ── keys and addresses ───────────────────────────────────────────────────

def test_keys_are_valid_wireguard_keys_and_the_hub_key_is_stable():
    private = svc.generate_private_key()
    public = svc.public_from_private(private)
    assert svc.valid_public_key(public) and len(public) == 44 and public != private
    assert svc.hub_public_key() == svc.hub_public_key()
    assert oct((Path(settings.overlay_dir) / "hub.key").stat().st_mode & 0o777) == "0o600"
    # RFC 7748 section 6.1 vector (Alice): the standard X25519 public key for a given private key
    alice_priv = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    import base64
    assert svc.public_from_private(base64.b64encode(alice_priv).decode()) == base64.b64encode(
        bytes.fromhex("8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")).decode()


@pytest.mark.parametrize("value,ok", [
    ("A" * 43 + "=", True), ("", False), ("short", False), ("A" * 44, False), ("A" * 42 + "==", False), ("!" * 43 + "=", False),
])
def test_public_key_validation(value, ok):
    assert svc.valid_public_key(value) is ok


def test_subnets():
    assert str(svc.parse_subnet("10.99.0.0/24")) == "10.99.0.0/24"
    for bad in ("8.8.8.0/24", "10.99.0.5/24", "10.0.0.0/8", "10.99.0.0/30", "fd00::/64", "nonsense", "", "127.0.0.0/24", "169.254.1.0/24"):
        with pytest.raises(svc.OverlayError):
            svc.parse_subnet(bad)


def test_allocation_skips_the_hub_and_taken_addresses_and_runs_out():
    net = svc.parse_subnet("10.99.0.0/29")  # .1 hub, .2-.6 usable
    assert str(svc.hub_address(net)) == "10.99.0.1"
    taken: set[str] = set()
    for expected in ("10.99.0.2", "10.99.0.3", "10.99.0.4", "10.99.0.5", "10.99.0.6"):
        ip = svc.next_free_ip(net, taken)
        assert ip == expected
        taken.add(ip)
    with pytest.raises(svc.OverlayError):
        svc.next_free_ip(net, taken)
    taken.discard("10.99.0.3")
    assert svc.next_free_ip(net, taken) == "10.99.0.3"  # reuses holes


def test_proxy_routing_decision():
    svc._active_network = ipaddress.ip_network("10.99.0.0/24")
    assert svc.proxy_for("https://10.99.0.2") == settings.overlay_proxy_url
    assert svc.proxy_for("https://10.99.0.2:8443/x") == settings.overlay_proxy_url
    for direct in ("https://10.98.0.2", "https://fw.example.com", "https://[fd00::1]", "not a url", "https://"):
        assert svc.proxy_for(direct) is None
    svc._active_network = None
    assert svc.proxy_for("https://10.99.0.2") is None


# ── API ──────────────────────────────────────────────────────────────────

async def _enable(client, **extra):
    resp = await client.put("/api/overlay/settings", json={"enabled": True, "endpoint": "vpn.example.com", **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _hub_json():
    return json.loads((Path(settings.overlay_dir) / "hub.json").read_text())


@pytest.mark.asyncio
async def test_enabling_needs_an_endpoint_and_writes_the_hub_config(client):
    assert (await client.put("/api/overlay/settings", json={"enabled": True})).status_code == 422
    body = await _enable(client)
    assert body["enabled"] and body["hub_public_key"] == svc.hub_public_key() and body["hub_address"] == "10.99.0.1"
    assert body["hub_state"] == "absent"  # no hub container reporting
    config = _hub_json()
    assert config["enabled"] and config["address"] == "10.99.0.1/24" and config["listen_port"] == 51820 and config["peers"] == []
    assert config["private_key"] == svc.hub_private_key()
    assert svc._active_network == ipaddress.ip_network("10.99.0.0/24")
    assert oct((Path(settings.overlay_dir) / "hub.json").stat().st_mode & 0o777) == "0o600"
    await client.put("/api/overlay/settings", json={"enabled": False})
    off = _hub_json()
    assert off["enabled"] is False and off["private_key"] is None and off["peers"] == [] and svc._active_network is None


@pytest.mark.asyncio
async def test_peer_lifecycle_and_hub_config(client):
    await _enable(client, listen_port=51999)
    created = (await client.post("/api/overlay/peers", json={"name": "branch-1"})).json()
    assert created["ip"] == "10.99.0.2"
    ins = created["instructions"]
    assert ins["tunnel_address"] == "10.99.0.2/32" and ins["allowed_ips"] == "10.99.0.1/32" and ins["endpoint"] == "vpn.example.com:51999"
    assert ins["host_url"] == "https://10.99.0.2" and ins["keepalive"] == 25 and svc.valid_public_key(ins["hub_public_key"]) and ins["psk"]
    again = (await client.get(f"/api/overlay/peers/{created['id']}/instructions")).json()
    assert again["psk"] == ins["psk"]  # stable, so the admin can look it up again
    second = (await client.post("/api/overlay/peers", json={"name": "branch-2"})).json()
    assert second["ip"] == "10.99.0.3" and second["instructions"]["psk"] != ins["psk"]
    assert (await client.post("/api/overlay/peers", json={"name": "branch-1"})).status_code == 409
    # A peer without a public key is not in the hub config yet.
    assert _hub_json()["peers"] == []
    key = svc.public_from_private(svc.generate_private_key())
    assert (await client.patch(f"/api/overlay/peers/{created['id']}", json={"public_key": key})).status_code == 200
    peers = _hub_json()["peers"]
    assert peers == [{"public_key": key, "psk": ins["psk"], "ip": "10.99.0.2"}]
    # Deleting removes it from the config and the PSK from disk.
    assert (await client.delete(f"/api/overlay/peers/{created['id']}")).status_code == 204
    assert _hub_json()["peers"] == [] and not (Path(settings.overlay_dir) / f"psk-{created['id']}").exists()
    assert (await client.delete(f"/api/overlay/peers/{created['id']}")).status_code == 404
    # The freed address is reused.
    assert (await client.post("/api/overlay/peers", json={"name": "branch-3"})).json()["ip"] == "10.99.0.2"


@pytest.mark.asyncio
async def test_peer_key_validation_and_host_link(client, respx_mock):
    import httpx
    from tests.test_rules import _create_host
    await _enable(client)
    peer = (await client.post("/api/overlay/peers", json={"name": "p"})).json()
    other = (await client.post("/api/overlay/peers", json={"name": "q"})).json()
    for bad in ("nonsense", "A" * 44, svc.hub_public_key()):
        assert (await client.patch(f"/api/overlay/peers/{peer['id']}", json={"public_key": bad})).status_code == 422, bad
    good = svc.public_from_private(svc.generate_private_key())
    assert (await client.patch(f"/api/overlay/peers/{peer['id']}", json={"public_key": good})).status_code == 200
    assert (await client.patch(f"/api/overlay/peers/{other['id']}", json={"public_key": good})).status_code == 409  # one key, one peer
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    assert (await client.patch(f"/api/overlay/peers/{peer['id']}", json={"host_id": host["id"]})).status_code == 200
    listing = (await client.get("/api/overlay")).json()["peers"]
    assert next(p for p in listing if p["id"] == peer["id"])["host_name"] == "fw1"
    import uuid
    assert (await client.patch(f"/api/overlay/peers/{peer['id']}", json={"host_id": str(uuid.uuid4())})).status_code == 404
    assert (await client.patch(f"/api/overlay/peers/{peer['id']}", json={"clear_host": True})).status_code == 200
    assert (await client.patch(f"/api/overlay/peers/{peer['id']}", json={"public_key": ""})).status_code == 200  # clearing is allowed
    assert _hub_json()["peers"] == []


@pytest.mark.asyncio
async def test_subnet_changes_only_without_peers(client):
    await _enable(client)
    assert (await client.put("/api/overlay/settings", json={"subnet": "172.30.5.0/24"})).json()["hub_address"] == "172.30.5.1"
    assert (await client.put("/api/overlay/settings", json={"subnet": "8.8.8.0/24"})).status_code == 422
    peer = (await client.post("/api/overlay/peers", json={"name": "x"})).json()
    assert peer["ip"] == "172.30.5.2"
    resp = await client.put("/api/overlay/settings", json={"subnet": "10.50.0.0/24"})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "OVERLAY_SUBNET_LOCKED"
    assert (await client.put("/api/overlay/settings", json={"subnet": "172.30.5.0/24"})).status_code == 200  # same value is no change


@pytest.mark.asyncio
async def test_live_status_from_the_hubs_report(client):
    await _enable(client)
    key = svc.public_from_private(svc.generate_private_key())
    peer = (await client.post("/api/overlay/peers", json={"name": "p"})).json()
    await client.patch(f"/api/overlay/peers/{peer['id']}", json={"public_key": key})
    status_file = Path(settings.overlay_dir) / "status.json"
    now = time.time()
    status_file.write_text(json.dumps({"updated_at": now, "up": True, "error": None,
                                        "peers": [{"public_key": key, "endpoint": "203.0.113.9:4000", "handshake_at": int(now) - 30, "rx": 1000, "tx": 2000}]}))
    data = (await client.get("/api/overlay")).json()
    assert data["settings"]["hub_state"] == "running"
    p = data["peers"][0]
    assert p["connected"] is True and p["rx_bytes"] == 1000 and p["tx_bytes"] == 2000 and p["remote_endpoint"] == "203.0.113.9:4000"
    status_file.write_text(json.dumps({"updated_at": now, "up": True, "peers": [{"public_key": key, "handshake_at": int(now) - 900, "rx": 1, "tx": 1}]}))
    assert (await client.get("/api/overlay")).json()["peers"][0]["connected"] is False  # handshake too old
    status_file.write_text(json.dumps({"updated_at": now - 600, "up": True, "peers": [{"public_key": key, "handshake_at": int(now), "rx": 1, "tx": 1}]}))
    data = (await client.get("/api/overlay")).json()
    assert data["settings"]["hub_state"] == "stale" and data["peers"][0]["connected"] is False  # hub stopped reporting
    status_file.write_text("not json")
    assert (await client.get("/api/overlay")).json()["settings"]["hub_state"] == "absent"


@pytest.mark.asyncio
async def test_only_admins_write_or_read_the_psk(client):
    await _enable(client)
    peer = (await client.post("/api/overlay/peers", json={"name": "p"})).json()
    for role in ("operator", "read_only"):
        name = f"ov-{role}"
        assert (await client.post("/api/users", json={"username": name, "password": "overlay-password-1", "role": role})).status_code == 201
        user = await _login(name, "overlay-password-1")
        try:
            assert (await user.get("/api/overlay")).status_code == 200  # status is for everybody ...
            assert "psk" not in (await user.get("/api/overlay")).text
            assert (await user.get(f"/api/overlay/peers/{peer['id']}/instructions")).status_code == 403  # ... the pre-shared key is not
            for method, path, body in (("PUT", "/api/overlay/settings", {"enabled": False}), ("POST", "/api/overlay/peers", {"name": "z"}),
                                       ("PATCH", f"/api/overlay/peers/{peer['id']}", {"clear_host": True}), ("DELETE", f"/api/overlay/peers/{peer['id']}", None)):
                assert (await user.request(method, path, json=body)).status_code == 403, (role, path)
        finally:
            await user.aclose()


# ── the hub ──────────────────────────────────────────────────────────────

def test_wg_config_and_dump_parsing():
    conf = wg_conf({"private_key": "PRIV", "listen_port": 51820, "peers": [{"public_key": "PUB1", "psk": "PSK1", "ip": "10.99.0.2"}]})
    assert conf == "[Interface]\nPrivateKey = PRIV\nListenPort = 51820\n\n[Peer]\nPublicKey = PUB1\nPresharedKey = PSK1\nAllowedIPs = 10.99.0.2/32\n"
    dump = "hubpriv\thubpub\t51820\toff\n" + "PUB1\tpsk\t198.51.100.7:5000\t10.99.0.2/32\t1700000000\t111\t222\t25\n" + "PUB2\tpsk\t(none)\t10.99.0.3/32\t0\t0\t0\t25\n"
    assert parse_dump(dump) == [
        {"public_key": "PUB1", "endpoint": "198.51.100.7:5000", "handshake_at": 1700000000, "rx": 111, "tx": 222},
        {"public_key": "PUB2", "endpoint": None, "handshake_at": 0, "rx": 0, "tx": 0}]
    assert parse_dump("only-interface-line") == []


class _FakeRunner:
    def __init__(self, existing=False, fail=None):
        self.calls, self.existing, self.fail = [], existing, fail

    def __call__(self, *cmd, stdin=None):
        self.calls.append(cmd)
        if cmd[:3] == ("ip", "link", "show"):
            return (0 if self.existing else 1), ""
        if cmd[:2] == ("iptables", "-C"):
            return 1, ""
        if self.fail and cmd[0] == self.fail:
            return 1, "boom"
        if cmd[:2] == ("wg", "show"):
            return 0, "hubpriv\thubpub\t51820\toff\nPUB1\tpsk\t(none)\t10.99.0.2/32\t0\t0\t0\t25\n"
        return 0, ""


def _config(**over):
    return {"enabled": True, "address": "10.99.0.1/24", "subnet": "10.99.0.0/24", "listen_port": 51820, "private_key": "PRIV",
            "peers": [{"public_key": "PUB1", "psk": "PSK1", "ip": "10.99.0.2"}], **over}


def test_hub_applies_the_interface_peers_and_the_inbound_drop_rule(tmp_path):
    runner = _FakeRunner()
    hub = Hub(tmp_path, runner)
    assert hub.apply(_config()) is None
    cmds = [c[:3] for c in runner.calls]
    assert ("ip", "link", "add") in cmds and ("ip", "address", "replace") in cmds and ("wg", "syncconf", "wg0") in cmds
    assert any(c[0] == "iptables" and c[1] == "-I" and "-i" in c and "NEW" in c and c[-1] == "DROP" for c in runner.calls)
    assert ("iptables", "-I", "FORWARD", "-j", "DROP") in runner.calls  # no routing between peers or into the compose network
    assert not list(tmp_path.glob(".wg0.conf"))  # the private key file does not linger
    assert hub.subnet == ipaddress.ip_network("10.99.0.0/24")
    # An existing interface is reused, not re-created.
    runner2 = _FakeRunner(existing=True)
    Hub(tmp_path, runner2).apply(_config())
    assert ("ip", "link", "add", "wg0", "type", "wireguard") not in runner2.calls


def test_hub_takes_the_interface_down_when_disabled_and_reports_errors(tmp_path):
    runner = _FakeRunner(existing=True)
    hub = Hub(tmp_path, runner)
    assert hub.apply(_config(enabled=False)) is None and ("ip", "link", "del", "wg0") in runner.calls and hub.subnet is None
    failing = Hub(tmp_path, _FakeRunner(fail="wg"))
    assert "wg syncconf" in failing.apply(_config())
    assert "ip link add" in (Hub(tmp_path, _FakeRunner(fail="ip")).apply(_config()) or "")


def test_hub_tick_applies_on_change_and_reports_status(tmp_path):
    runner = _FakeRunner()
    hub = Hub(tmp_path, runner)
    assert hub.tick(None) is None  # no config yet: still reports
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["up"] is True and status["peers"][0]["public_key"] == "PUB1" and abs(status["updated_at"] - time.time()) < 5
    (tmp_path / "hub.json").write_text(json.dumps(_config()))
    hub.tick(None)
    applied = len([c for c in runner.calls if c[:2] == ("wg", "syncconf")])
    assert applied == 1
    hub.tick(None)
    assert len([c for c in runner.calls if c[:2] == ("wg", "syncconf")]) == 1  # unchanged file: no re-apply
    os.utime(tmp_path / "hub.json", (time.time() + 10, time.time() + 10))
    hub.tick(None)
    assert len([c for c in runner.calls if c[:2] == ("wg", "syncconf")]) == 2


async def _serve_echo(prefix=b"echo:"):
    async def handle(reader, writer):
        while data := await reader.read(1024):
            writer.write(prefix + data)
            await writer.drain()
        writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _proxy(subnet="127.0.0.0/8"):
    hub = Hub(Path(tempfile.mkdtemp()), _FakeRunner())
    hub.subnet = ipaddress.ip_network(subnet)
    server = await asyncio.start_server(hub.handle_client, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _connect(proxy_port, target):
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    return reader, writer, await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)


@pytest.mark.asyncio
async def test_proxy_relays_connections_into_the_overlay_subnet():
    echo, echo_port = await _serve_echo()
    proxy, proxy_port = await _proxy()
    try:
        reader, writer, head = await _connect(proxy_port, f"127.0.0.1:{echo_port}")
        assert head.startswith(b"HTTP/1.1 200")
        writer.write(b"hello")
        await writer.drain()
        assert await asyncio.wait_for(reader.readexactly(10), 5) == b"echo:hello"
        writer.close()
    finally:
        proxy.close(); echo.close()


@pytest.mark.asyncio
async def test_proxy_is_not_an_open_proxy():
    echo, echo_port = await _serve_echo()
    proxy, proxy_port = await _proxy(subnet="10.99.0.0/24")  # loopback is outside this overlay
    try:
        for target in (f"127.0.0.1:{echo_port}", "8.8.8.8:443", "example.com:443", "10.99.0.2:0", "10.99.0.2:99999", "10.99.0.2:abc", "nonsense"):
            _, writer, head = await _connect(proxy_port, target)
            assert head.startswith(b"HTTP/1.1 403"), target
            writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(b"GET http://10.99.0.2/ HTTP/1.1\r\nHost: 10.99.0.2\r\n\r\n")  # plain proxying is not offered either
        assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)).startswith(b"HTTP/1.1 403")
        writer.close()
    finally:
        proxy.close(); echo.close()


@pytest.mark.asyncio
async def test_proxy_reports_unreachable_firewalls_and_survives_garbage():
    proxy, proxy_port = await _proxy()
    free = socket_free_port()
    try:
        _, writer, head = await _connect(proxy_port, f"127.0.0.1:{free}")
        assert head.startswith(b"HTTP/1.1 502")
        writer.close()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(b"\x00\xff" * 100 + b"\r\n\r\n")
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()
        _, writer, head = await _connect(proxy_port, f"127.0.0.1:{free}")  # still serving
        assert head.startswith(b"HTTP/1.1 502")
        writer.close()
    finally:
        proxy.close()


def socket_free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── requests to overlay firewalls go through the proxy ───────────────────

def test_clients_for_overlay_hosts_use_the_proxy_and_others_do_not(monkeypatch):
    from app.models.host import OpnsenseHost
    from app.services import opnsense_client, opnsense_secret_store
    import uuid
    monkeypatch.setattr(opnsense_secret_store, "read_secret", lambda host_id: "secret")
    svc._active_network = ipaddress.ip_network("10.99.0.0/24")
    overlay = OpnsenseHost(id=uuid.uuid4(), name="o", base_url="https://10.99.0.2", api_key="k", verify_tls=False)
    direct = OpnsenseHost(id=uuid.uuid4(), name="d", base_url="https://10.10.10.2", api_key="k", verify_tls=False)
    assert opnsense_client._client(overlay)._mounts, "the overlay client must have a proxy mount"
    assert not opnsense_client._client(direct)._mounts


def _self_signed(cn="fw.test"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1)).not_valid_after(datetime.now(timezone.utc) + timedelta(days=30))
            .sign(key, hashes.SHA256()))
    return cert, key


@pytest.mark.asyncio
async def test_tls_probe_and_pin_check_work_through_the_proxy(tmp_path, monkeypatch):
    from app.services import tls_pin_service
    cert, key = _self_signed()
    (tmp_path / "c.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "k.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    fw = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0, ssl=ctx)
    fw_port = fw.sockets[0].getsockname()[1]
    proxy, proxy_port = await _proxy()
    monkeypatch.setattr(settings, "overlay_proxy_url", f"http://127.0.0.1:{proxy_port}")
    svc._active_network = ipaddress.ip_network("127.0.0.0/8")
    try:
        info = await tls_pin_service.probe(f"https://127.0.0.1:{fw_port}")
        assert info["pem"].startswith("-----BEGIN CERTIFICATE-----")
        await tls_pin_service.verify_matches(f"https://127.0.0.1:{fw_port}", info["pem"])
        other, _ = _self_signed("someone-else")
        with pytest.raises(tls_pin_service.TlsPinError):
            await tls_pin_service.verify_matches(f"https://127.0.0.1:{fw_port}", other.public_bytes(serialization.Encoding.PEM).decode())
        # The proxy refusing (target outside the overlay) surfaces as a connection error, not a hang.
        svc._active_network = ipaddress.ip_network("127.0.0.0/8")
        hub_only = await _proxy(subnet="10.99.0.0/24")
        monkeypatch.setattr(settings, "overlay_proxy_url", f"http://127.0.0.1:{hub_only[1]}")
        with pytest.raises(tls_pin_service.TlsPinError):
            await tls_pin_service.probe(f"https://127.0.0.1:{fw_port}")
        hub_only[0].close()
    finally:
        proxy.close(); fw.close()


@pytest.mark.asyncio
async def test_a_dead_proxy_is_a_clean_error(monkeypatch):
    from app.services import tls_pin_service
    monkeypatch.setattr(settings, "overlay_proxy_url", f"http://127.0.0.1:{socket_free_port()}")
    svc._active_network = ipaddress.ip_network("10.99.0.0/24")
    with pytest.raises(tls_pin_service.TlsPinError):
        await tls_pin_service.probe("https://10.99.0.2")
