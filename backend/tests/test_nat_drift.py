import httpx
import pytest

from app.database import SessionLocal
from app.services import notification_checks


async def _create_host(client, name: str, base_url: str, cluster_name: str) -> dict:
    payload = {
        "name": name, "base_url": base_url, "api_key": "key", "api_secret": "secret",
        "verify_tls": False, "cluster_name": cluster_name,
    }
    resp = await client.post("/api/hosts", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _vip_empty():
    return {"rows": []}


def _pf_row(uuid_: str, descr: str, target: str = "10.0.0.5") -> dict:
    return {
        "uuid": uuid_, "descr": descr, "interface": "wan", "protocol": "tcp",
        "source.network": "any", "source.port": "", "destination.network": "wan_ip",
        "destination.port": "443", "target": target, "local-port": "443", "disabled": "0",
    }


def _one_to_one_row(uuid_: str, description: str) -> dict:
    return {
        "uuid": uuid_, "description": description, "interface": "wan", "type": "binat",
        "source_net": "10.0.0.10", "destination_net": "any", "external": "203.0.113.10", "enabled": "1",
    }


def _outbound_row(uuid_: str, description: str) -> dict:
    return {
        "uuid": uuid_, "description": description, "interface": "wan", "protocol": "tcp",
        "source_net": "10.0.0.0/24", "source_port": "", "destination_net": "any",
        "destination_port": "", "target": "wanip", "target_port": "", "enabled": "1",
    }


@pytest.mark.asyncio
async def test_check_nat_drift_detects_missing_port_forward(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json=_vip_empty()))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    respx_mock.get("https://fw-a.test/api/firewall/d_nat/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [_pf_row("p1", "Web-Server"), _pf_row("p2", "SSH-Jump")]})
    )
    respx_mock.get("https://fw-b.test/api/firewall/d_nat/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [_pf_row("p1", "Web-Server")]})
    )
    respx_mock.get(url__regex=r".*/firewall/one_to_one/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/firewall/source_nat/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))

    async with SessionLocal() as db:
        problems = await notification_checks.check_nat_drift(db)

    assert len(problems) == 1
    row = next(iter(problems.values()))
    assert row["nat_type"] == "Port-Forward"
    assert row["description"] == "SSH-Jump"
    assert row["present_on"] == ["fw-a"]
    assert row["missing_from"] == ["fw-b"]


@pytest.mark.asyncio
async def test_check_nat_drift_detects_missing_one_to_one_and_outbound(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json=_vip_empty()))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    respx_mock.get(url__regex=r".*/firewall/d_nat/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get("https://fw-a.test/api/firewall/one_to_one/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [_one_to_one_row("o1", "Public IP Mapping")]})
    )
    respx_mock.get("https://fw-b.test/api/firewall/one_to_one/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get("https://fw-a.test/api/firewall/source_nat/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [_outbound_row("s1", "Default Outbound")]})
    )
    respx_mock.get("https://fw-b.test/api/firewall/source_nat/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [_outbound_row("s1", "Default Outbound")]})
    )

    async with SessionLocal() as db:
        problems = await notification_checks.check_nat_drift(db)

    assert len(problems) == 1
    row = next(iter(problems.values()))
    assert row["nat_type"] == "1:1-NAT"
    assert row["description"] == "Public IP Mapping"


@pytest.mark.asyncio
async def test_check_nat_drift_no_drift_when_identical(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json=_vip_empty()))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    same_pf = {"rows": [_pf_row("p1", "Web-Server")]}
    respx_mock.get(url__regex=r".*/firewall/d_nat/searchRule").mock(return_value=httpx.Response(200, json=same_pf))
    respx_mock.get(url__regex=r".*/firewall/one_to_one/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/firewall/source_nat/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))

    async with SessionLocal() as db:
        problems = await notification_checks.check_nat_drift(db)

    assert problems == {}


@pytest.mark.asyncio
async def test_check_nat_drift_ignores_automatic_rules(client, respx_mock):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json=_vip_empty()))
    await _create_host(client, "fw-a", "https://fw-a.test", "ha")
    await _create_host(client, "fw-b", "https://fw-b.test", "ha")

    respx_mock.get("https://fw-a.test/api/firewall/d_nat/searchRule").mock(
        return_value=httpx.Response(200, json={"rows": [{**_pf_row("automatic_wan_1", "auto"), "is_automatic": True}]})
    )
    respx_mock.get("https://fw-b.test/api/firewall/d_nat/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/firewall/one_to_one/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(url__regex=r".*/firewall/source_nat/searchRule").mock(return_value=httpx.Response(200, json={"rows": []}))

    async with SessionLocal() as db:
        problems = await notification_checks.check_nat_drift(db)

    assert problems == {}
