import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.database import SessionLocal
from app.models.log_entry import FirewallLogEntry


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_host(client, respx_mock, name: str, base_url: str, cluster_name: str | None = None) -> dict:
    # Host creation synchronously checks live status (getVipStatus) — mock
    # it so no real network call is attempted, matching this project's
    # existing test convention (see test_rules.py/test_notifications.py).
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    payload = {"name": name, "base_url": base_url, "api_key": "key", "api_secret": "secret", "verify_tls": False}
    if cluster_name:
        payload["cluster_name"] = cluster_name
    resp = await client.post("/api/hosts", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _add_entry(
    host_id: uuid.UUID,
    *,
    timestamp: datetime,
    action: str = "pass",
    interface: str = "wan",
    protocol: str = "tcp",
    source_net: str | None = "10.0.0.1",
    destination_net: str | None = "10.0.0.2",
    destination_port: str | None = "443",
    label: str | None = "default-rule",
    digest: str,
) -> None:
    async with SessionLocal() as db:
        db.add(FirewallLogEntry(
            host_id=host_id, timestamp=timestamp, action=action, interface=interface,
            direction="in", protocol=protocol, source_net=source_net, source_port="5000",
            destination_net=destination_net, destination_port=destination_port,
            label=label, raw={}, digest=digest,
        ))
        await db.commit()


# ── /logs search/filter/pagination ──────────────────────────────────────

@pytest.mark.asyncio
async def test_search_logs_returns_all_hosts_by_default(client, respx_mock):
    a = await _create_host(client, respx_mock, "fw-a", "https://fw-a.test")
    b = await _create_host(client, respx_mock, "fw-b", "https://fw-b.test")
    await _add_entry(uuid.UUID(a["id"]), timestamp=_now(), digest="a-1")
    await _add_entry(uuid.UUID(b["id"]), timestamp=_now(), digest="b-1")

    resp = await client.get("/api/logs")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_search_logs_filters_by_host_id(client, respx_mock):
    a = await _create_host(client, respx_mock, "fw-a", "https://fw-a.test")
    b = await _create_host(client, respx_mock, "fw-b", "https://fw-b.test")
    await _add_entry(uuid.UUID(a["id"]), timestamp=_now(), digest="a-1")
    await _add_entry(uuid.UUID(b["id"]), timestamp=_now(), digest="b-1")

    resp = await client.get("/api/logs", params={"host_id": a["id"]})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["host_id"] == a["id"]


@pytest.mark.asyncio
async def test_search_logs_filters_by_cluster_name(client, respx_mock):
    a = await _create_host(client, respx_mock, "fw-a", "https://fw-a.test", cluster_name="ha")
    b = await _create_host(client, respx_mock, "fw-b", "https://fw-b.test", cluster_name="ha")
    c = await _create_host(client, respx_mock, "fw-c", "https://fw-c.test")
    await _add_entry(uuid.UUID(a["id"]), timestamp=_now(), digest="a-1")
    await _add_entry(uuid.UUID(b["id"]), timestamp=_now(), digest="b-1")
    await _add_entry(uuid.UUID(c["id"]), timestamp=_now(), digest="c-1")

    resp = await client.get("/api/logs", params={"cluster_name": "ha"})
    data = resp.json()
    assert data["total"] == 2
    assert {item["host_id"] for item in data["items"]} == {a["id"], b["id"]}


@pytest.mark.asyncio
async def test_search_logs_filters_by_action_interface_protocol(client, respx_mock):
    host = await _create_host(client, respx_mock, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_entry(hid, timestamp=_now(), action="pass", interface="wan", protocol="tcp", digest="d1")
    await _add_entry(hid, timestamp=_now(), action="block", interface="lan", protocol="udp", digest="d2")

    resp = await client.get("/api/logs", params={"host_id": host["id"], "action": "block"})
    assert resp.json()["total"] == 1
    resp = await client.get("/api/logs", params={"host_id": host["id"], "interface": "lan"})
    assert resp.json()["total"] == 1
    resp = await client.get("/api/logs", params={"host_id": host["id"], "protocol": "udp"})
    assert resp.json()["total"] == 1
    resp = await client.get("/api/logs", params={"host_id": host["id"], "action": "pass", "protocol": "udp"})
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_search_logs_free_text_search(client, respx_mock):
    host = await _create_host(client, respx_mock, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_entry(hid, timestamp=_now(), label="allow-ssh", source_net="192.168.1.5", digest="d1")
    await _add_entry(hid, timestamp=_now(), label="block-scan", source_net="203.0.113.9", digest="d2")

    resp = await client.get("/api/logs", params={"host_id": host["id"], "q": "ssh"})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["label"] == "allow-ssh"

    resp = await client.get("/api/logs", params={"host_id": host["id"], "q": "203.0.113"})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["source_net"] == "203.0.113.9"


@pytest.mark.asyncio
async def test_search_logs_time_range(client, respx_mock):
    host = await _create_host(client, respx_mock, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    now = _now()
    await _add_entry(hid, timestamp=now - timedelta(hours=5), digest="old")
    await _add_entry(hid, timestamp=now, digest="new")

    since = (now - timedelta(hours=1)).isoformat()
    resp = await client.get("/api/logs", params={"host_id": host["id"], "since": since})
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["timestamp"].startswith(str(now.year))

    until = (now - timedelta(hours=1)).isoformat()
    resp = await client.get("/api/logs", params={"host_id": host["id"], "until": until})
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_search_logs_pagination_and_sort_order(client, respx_mock):
    host = await _create_host(client, respx_mock, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    now = _now()
    for i in range(5):
        await _add_entry(hid, timestamp=now - timedelta(minutes=i), digest=f"d{i}")

    resp = await client.get("/api/logs", params={"host_id": host["id"], "limit": 2, "offset": 0})
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    # newest first: page 1 should be [d0 (now), d1 (now-1min)]
    assert data["items"][0]["timestamp"] > data["items"][1]["timestamp"]
    first_page_ids = [item["id"] for item in data["items"]]

    resp = await client.get("/api/logs", params={"host_id": host["id"], "limit": 2, "offset": 2})
    data2 = resp.json()
    assert len(data2["items"]) == 2
    assert not set(first_page_ids) & {item["id"] for item in data2["items"]}


# ── /logs/stats ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_log_stats_by_action_and_interface(client, respx_mock):
    host = await _create_host(client, respx_mock, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_entry(hid, timestamp=_now(), action="pass", interface="wan", digest="d1")
    await _add_entry(hid, timestamp=_now(), action="pass", interface="wan", digest="d2")
    await _add_entry(hid, timestamp=_now(), action="block", interface="lan", digest="d3")

    resp = await client.get("/api/logs/stats", params={"host_id": host["id"]})
    data = resp.json()
    assert data["by_action"] == {"pass": 2, "block": 1}
    assert data["by_interface"] == {"wan": 2, "lan": 1}


@pytest.mark.asyncio
async def test_log_stats_top_blocked_sources_excludes_passed_traffic(client, respx_mock):
    host = await _create_host(client, respx_mock, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_entry(hid, timestamp=_now(), action="block", source_net="1.2.3.4", digest="d1")
    await _add_entry(hid, timestamp=_now(), action="block", source_net="1.2.3.4", digest="d2")
    await _add_entry(hid, timestamp=_now(), action="block", source_net="5.6.7.8", digest="d3")
    await _add_entry(hid, timestamp=_now(), action="pass", source_net="9.9.9.9", digest="d4")

    resp = await client.get("/api/logs/stats", params={"host_id": host["id"]})
    data = resp.json()
    values = {row["value"]: row["count"] for row in data["top_blocked_sources"]}
    assert values == {"1.2.3.4": 2, "5.6.7.8": 1}
    assert "9.9.9.9" not in values


@pytest.mark.asyncio
async def test_log_stats_top_destination_ports_counts_all_actions(client, respx_mock):
    host = await _create_host(client, respx_mock, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_entry(hid, timestamp=_now(), action="pass", destination_port="443", digest="d1")
    await _add_entry(hid, timestamp=_now(), action="block", destination_port="443", digest="d2")
    await _add_entry(hid, timestamp=_now(), action="pass", destination_port="22", digest="d3")

    resp = await client.get("/api/logs/stats", params={"host_id": host["id"]})
    data = resp.json()
    values = {row["value"]: row["count"] for row in data["top_destination_ports"]}
    assert values == {"443": 2, "22": 1}


@pytest.mark.asyncio
async def test_log_stats_respects_cluster_and_time_filters(client, respx_mock):
    a = await _create_host(client, respx_mock, "fw-a", "https://fw-a.test", cluster_name="ha")
    b = await _create_host(client, respx_mock, "fw-b", "https://fw-b.test", cluster_name="ha")
    now = _now()
    await _add_entry(uuid.UUID(a["id"]), timestamp=now, action="pass", digest="a-1")
    await _add_entry(uuid.UUID(b["id"]), timestamp=now - timedelta(days=2), action="pass", digest="b-1")

    resp = await client.get("/api/logs/stats", params={"cluster_name": "ha", "since": (now - timedelta(hours=1)).isoformat()})
    data = resp.json()
    assert data["by_action"] == {"pass": 1}


# ── /logs/filter-options ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filter_options_returns_sorted_distinct_values(client, respx_mock):
    host = await _create_host(client, respx_mock, "fw1", "https://fw1.test")
    hid = uuid.UUID(host["id"])
    await _add_entry(hid, timestamp=_now(), action="pass", interface="wan", protocol="tcp", digest="d1")
    await _add_entry(hid, timestamp=_now(), action="block", interface="lan", protocol="udp", digest="d2")
    await _add_entry(hid, timestamp=_now(), action="pass", interface="lan", protocol="tcp", digest="d3")

    resp = await client.get("/api/logs/filter-options")
    data = resp.json()
    assert data["actions"] == ["block", "pass"]
    assert data["interfaces"] == ["lan", "wan"]
    assert data["protocols"] == ["tcp", "udp"]


# ── /logs/settings ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_log_settings_defaults(client):
    resp = await client.get("/api/logs/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is True
    assert data["poll_interval_seconds"] == 30
    assert data["retention_days"] == 7


@pytest.mark.asyncio
async def test_log_settings_update_persists(client):
    resp = await client.put("/api/logs/settings", json={"retention_days": 3, "enabled": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["retention_days"] == 3
    assert data["enabled"] is False
    assert data["poll_interval_seconds"] == 30  # untouched field keeps its value

    resp = await client.get("/api/logs/settings")
    assert resp.json()["retention_days"] == 3


@pytest.mark.asyncio
async def test_log_settings_poll_interval_floored(client):
    resp = await client.put("/api/logs/settings", json={"poll_interval_seconds": 1})
    assert resp.status_code == 200
    from app.config import settings as app_settings
    assert resp.json()["poll_interval_seconds"] == app_settings.log_poll_min_interval_seconds


@pytest.mark.asyncio
async def test_log_settings_rejects_non_positive_retention(client):
    resp = await client.put("/api/logs/settings", json={"retention_days": 0})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_log_settings_size_limit_default_update_and_usage(client):
    data = (await client.get("/api/logs/settings")).json()
    assert data["max_size_gb"] == 10
    assert data["used_bytes"] >= 0 and data["entries"] >= 0
    assert "oldest_at" in data

    resp = await client.put("/api/logs/settings", json={"max_size_gb": 2.5})
    assert resp.status_code == 200 and resp.json()["max_size_gb"] == 2.5
    resp = await client.put("/api/logs/settings", json={"max_size_gb": 0})
    assert resp.status_code == 200 and resp.json()["max_size_gb"] == 0  # 0 = unlimited


@pytest.mark.asyncio
async def test_log_settings_rejects_negative_size_limit(client):
    assert (await client.put("/api/logs/settings", json={"max_size_gb": -1})).status_code == 422
