"""Ping / traceroute / port probe / DNS lookup run from a firewall."""
import asyncio
import json

import httpx
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models.audit import AuditEvent

BASE = "https://10.20.30.85"


async def _host(client, respx_mock, name="fw-diag"):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    return (await client.post("/api/hosts", json={"name": name, "base_url": BASE, "api_key": "k", "api_secret": "s"})).json()["id"]


def _url(path):
    return f"{BASE}/api{path}"


async def _events(host_id):
    async with SessionLocal() as db:
        rows = await db.scalars(select(AuditEvent).where(AuditEvent.host_id == host_id, AuditEvent.kind == "diagnostic").order_by(AuditEvent.created_at))
        return list(rows.all())


@pytest.fixture
def fast_sleep(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(asyncio, "sleep", instant)


@pytest.mark.asyncio
async def test_traceroute_is_parsed_and_audited(client, respx_mock):
    host_id = await _host(client, respx_mock)
    sent = respx_mock.post(_url("/diagnostics/traceroute/set")).mock(return_value=httpx.Response(200, json={
        "result": "ok", "response": {"notice": "traceroute to x", "rows": [
            {"address": "10.0.0.1", "ttl": "1", "AS": "AS0", "host": "gw.lan", "probes": "0.4 ms"}]}}))
    resp = await client.post(f"/api/hosts/{host_id}/diagnostics/traceroute", json={"target": "example.com", "protocol": "icmp"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["hops"] == [{"ttl": "1", "address": "10.0.0.1", "host": "gw.lan", "as": "AS0", "probes": "0.4 ms"}]
    assert json.loads(sent.calls.last.request.content)["traceroute"]["settings"]["hostname"] == "example.com"
    events = await _events(host_id)
    assert [(e.action, e.status, e.entity_name) for e in events] == [("diag.traceroute", "ok", "example.com")]


@pytest.mark.asyncio
async def test_bad_targets_never_reach_the_firewall(client, respx_mock):
    host_id = await _host(client, respx_mock, "fw-diag2")
    route = respx_mock.post(_url("/diagnostics/traceroute/set")).mock(return_value=httpx.Response(200, json={"result": "ok", "response": {}}))
    for bad in ("a;b", "x y", "$(reboot)", "-h", "a" * 300, "host/../x"):
        resp = await client.post(f"/api/hosts/{host_id}/diagnostics/traceroute", json={"target": bad})
        assert resp.status_code == 422, bad
    assert not route.called
    assert (await client.post(f"/api/hosts/{host_id}/diagnostics/portprobe", json={"target": "x.test", "port": 70000})).status_code == 422
    assert (await client.post(f"/api/hosts/{host_id}/diagnostics/ping", json={"target": "x.test", "count": 500})).status_code == 422
    assert (await client.post(f"/api/hosts/{host_id}/diagnostics/ping", json={"target": "x.test", "source": "not-an-ip"})).status_code == 422


@pytest.mark.asyncio
async def test_the_firewalls_own_validation_is_reported_as_422_and_audited(client, respx_mock):
    host_id = await _host(client, respx_mock, "fw-diag3")
    respx_mock.post(_url("/diagnostics/dns_diagnostics/set")).mock(return_value=httpx.Response(200, json={
        "result": "failed", "validations": {"dns.settings.server": "Provide a valid server."}}))
    resp = await client.post(f"/api/hosts/{host_id}/diagnostics/dns", json={"name": "example.com", "server": "1.2.3.4"})
    assert resp.status_code == 422 and resp.json()["detail"]["params"]["field"] == "server"
    assert [(e.status) for e in await _events(host_id)] == ["failed"]


@pytest.mark.asyncio
async def test_portprobe_reports_open_or_closed_and_never_the_banner(client, respx_mock):
    host_id = await _host(client, respx_mock, "fw-diag4")
    answers = iter([
        {"result": "ok", "response": {"message": "Connection to 10.0.0.1 443 port [tcp/https] succeeded!", "payload": "SSH-2.0-SECRET-BANNER"}},
        {"result": "ok", "response": {"message": "nc: connect to 10.0.0.1 port 1 (tcp) failed: Operation timed out", "payload": ""}},
    ])
    respx_mock.post(_url("/diagnostics/portprobe/set")).mock(side_effect=lambda r: httpx.Response(200, json=next(answers)))
    opened = await client.post(f"/api/hosts/{host_id}/diagnostics/portprobe", json={"target": "10.0.0.1", "port": 443})
    closed = await client.post(f"/api/hosts/{host_id}/diagnostics/portprobe", json={"target": "10.0.0.1", "port": 1})
    assert opened.json()["open"] is True and closed.json()["open"] is False
    assert "SECRET-BANNER" not in opened.text


@pytest.mark.asyncio
async def test_dns_lookup(client, respx_mock):
    host_id = await _host(client, respx_mock, "fw-diag5")
    respx_mock.post(_url("/diagnostics/dns_diagnostics/set")).mock(return_value=httpx.Response(200, json={"result": "ok", "response": {
        "A": {"answers": ["example.com.\t300\tIN\tA\t192.0.2.1"], "query_time": "42 msec", "server": "10.0.0.1"}}}))
    body = (await client.post(f"/api/hosts/{host_id}/diagnostics/dns", json={"name": "example.com"})).json()
    assert body["records"]["A"]["answers"][0].endswith("192.0.2.1") and body["records"]["A"]["query_time"] == "42 msec"


@pytest.mark.asyncio
async def test_ping_runs_a_job_and_always_cleans_it_up(client, respx_mock, fast_sleep):
    host_id = await _host(client, respx_mock, "fw-diag6")
    respx_mock.post(_url("/diagnostics/ping/set")).mock(return_value=httpx.Response(200, json={"result": "ok", "uuid": "job-1"}))
    start = respx_mock.post(_url("/diagnostics/ping/start/job-1")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    row = {"id": "job-1", "status": "running", "send": 4, "received": 4, "loss": "0.0%", "min": 0.3, "avg": 0.4, "max": 0.5, "std-dev": 0.05, "last_error": None}
    respx_mock.get(_url("/diagnostics/ping/searchJobs")).mock(return_value=httpx.Response(200, json={"rows": [row]}))
    stop = respx_mock.post(_url("/diagnostics/ping/stop/job-1")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    remove = respx_mock.post(_url("/diagnostics/ping/remove/job-1")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    resp = await client.post(f"/api/hosts/{host_id}/diagnostics/ping", json={"target": "10.0.0.1", "count": 4})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["sent"], body["received"], body["loss"], body["avg"]) == (4, 4, "0.0%", 0.4)
    assert start.called and stop.called and remove.called


@pytest.mark.asyncio
async def test_ping_job_is_removed_even_when_polling_fails(client, respx_mock, fast_sleep):
    host_id = await _host(client, respx_mock, "fw-diag7")
    respx_mock.post(_url("/diagnostics/ping/set")).mock(return_value=httpx.Response(200, json={"result": "ok", "uuid": "job-2"}))
    respx_mock.post(_url("/diagnostics/ping/start/job-2")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    respx_mock.get(_url("/diagnostics/ping/searchJobs")).mock(side_effect=httpx.ConnectError("gone"))
    stop = respx_mock.post(_url("/diagnostics/ping/stop/job-2")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    remove = respx_mock.post(_url("/diagnostics/ping/remove/job-2")).mock(return_value=httpx.Response(200, json={"status": "ok"}))
    resp = await client.post(f"/api/hosts/{host_id}/diagnostics/ping", json={"target": "10.0.0.1"})
    assert resp.status_code == 502
    assert stop.called and remove.called       # no orphaned job on the firewall
    assert [e.status for e in await _events(host_id)] == ["failed"]


@pytest.mark.asyncio
async def test_only_admins_can_run_probes(anon_client, client, respx_mock):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    host_id = await _host(client, respx_mock, "fw-diag8")
    assert (await anon_client.post(f"/api/hosts/{host_id}/diagnostics/dns", json={"name": "example.com"})).status_code == 401
    created = await client.post("/api/users", json={"username": "diag_ro", "password": "Diag-Read-Only-2026", "role": "read_only"})
    assert created.status_code == 201, created.text
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ro:
            assert (await ro.post("/api/auth/login", json={"username": "diag_ro", "password": "Diag-Read-Only-2026"})).status_code == 200
            probe = respx_mock.post(_url("/diagnostics/dns_diagnostics/set")).mock(return_value=httpx.Response(200, json={"result": "ok", "response": {}}))
            for tool, body in (("dns", {"name": "example.com"}), ("ping", {"target": "10.0.0.1"}), ("traceroute", {"target": "10.0.0.1"}), ("portprobe", {"target": "10.0.0.1", "port": 22})):
                assert (await ro.post(f"/api/hosts/{host_id}/diagnostics/{tool}", json=body)).status_code == 403, tool
            assert not probe.called
    finally:
        await client.delete(f"/api/users/{created.json()['id']}")
