"""Prometheus endpoint: format, content and that it never calls a firewall."""
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.database import SessionLocal
from app.models.carp_event import CarpEvent
from app.models.notification import NotificationProblem
from app.models.resource_sample import HostResourceSample
from tests.test_api_tokens import _bearer, _make
from tests.test_rules import _create_host

_SAMPLE_LINE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*"(,|(?=\})))*\})? -?\d+(\.\d+)?(e[+-]?\d+)?$')


def _parse(text: str) -> dict[str, float]:
    """name{labels} -> value, asserting the exposition grammar line by line."""
    values = {}
    for line in text.splitlines():
        if not line or line.startswith("# HELP ") or line.startswith("# TYPE "):
            continue
        assert _SAMPLE_LINE.match(line), f"not valid exposition: {line!r}"
        key, _, value = line.rpartition(" ")
        values[key] = float(value)
    return values


async def _seed(host_id: str, *, age_s: int, carp: str | None):
    async with SessionLocal() as db:
        db.add(HostResourceSample(
            host_id=host_id, sampled_at=datetime.now(timezone.utc) - timedelta(seconds=age_s),
            mem_used_pct=25.0, load_avg_1m=0.42, disk_used_pct=12.0, raw={},
        ))
        if carp:
            db.add(CarpEvent(host_id=host_id, host_name="fw1", cluster_name="ha", from_status=None, to_status=carp))
        await db.commit()


@pytest.mark.asyncio
async def test_metrics_requires_auth_and_is_prometheus_text(client, anon_client):
    assert (await anon_client.get("/api/metrics")).status_code == 401
    resp = await client.get("/api/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain; version=0.0.4")
    values = _parse(resp.text)
    assert values["opnexus_hosts"] == 0
    assert any(key.startswith('opnexus_info{version="') for key in values)


@pytest.mark.asyncio
async def test_host_metrics_from_stored_samples_without_calling_the_firewall(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    host = await _create_host(client, "fw1", "https://fw1.test")
    await _seed(host["id"], age_s=30, carp="MASTER")
    before = len(respx_mock.calls)
    values = _parse((await client.get("/api/metrics")).text)
    # The scrape is DB-only: not a single request went to a firewall.
    assert len(respx_mock.calls) == before
    assert values["opnexus_hosts"] == 1
    assert values['opnexus_host_up{host="fw1",cluster=""}'] == 1
    assert values['opnexus_host_memory_used_percent{host="fw1",cluster=""}'] == 25
    assert values['opnexus_host_load1{host="fw1",cluster=""}'] == 0.42
    assert values['opnexus_host_disk_used_percent{host="fw1",cluster=""}'] == 12
    assert values['opnexus_host_carp_master{host="fw1",cluster=""}'] == 1


@pytest.mark.asyncio
async def test_stale_sample_means_host_down_and_unknown_carp_is_omitted(client):
    host = await _create_host(client, "fw1", "https://fw1.test")
    await _seed(host["id"], age_s=600, carp=None)
    text = (await client.get("/api/metrics")).text
    values = _parse(text)
    assert values['opnexus_host_up{host="fw1",cluster=""}'] == 0
    assert "opnexus_host_carp_master{" not in text


@pytest.mark.asyncio
async def test_open_problems_are_counted_and_resolved_ones_are_not(client):
    async with SessionLocal() as db:
        db.add(NotificationProblem(problem_type="host_offline", dedup_key="a", status="open"))
        db.add(NotificationProblem(problem_type="host_offline", dedup_key="b", status="open"))
        db.add(NotificationProblem(problem_type="cert_expiry", dedup_key="c", status="resolved"))
        await db.commit()
    values = _parse((await client.get("/api/metrics")).text)
    assert values['opnexus_problems_open{type="host_offline"}'] == 2
    assert not any("cert_expiry" in key for key in values)


@pytest.mark.asyncio
async def test_label_values_are_escaped(client):
    await _create_host(client, 'we"ird\\name', "https://fw1.test")
    text = (await client.get("/api/metrics")).text
    assert 'host="we\\"ird\\\\name"' in text
    _parse(text)


@pytest.mark.asyncio
async def test_read_token_can_scrape(client):
    created = await _make(client, "read", name="prometheus")
    async with _bearer(created["token"]) as api:
        resp = await api.get("/api/metrics")
    assert resp.status_code == 200 and "opnexus_info" in resp.text
