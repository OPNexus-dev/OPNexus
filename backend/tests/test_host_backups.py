import httpx
import pytest


async def _create_host(client, respx_mock, name, base_url):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    resp = await client.post(
        "/api/hosts",
        json={"name": name, "base_url": base_url, "api_key": "k", "api_secret": "s"},
    )
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_trigger_backup_stores_and_lists_snapshot(client, respx_mock):
    host_id = await _create_host(client, respx_mock, "fw-bk1", "https://fw-bk1.test")

    respx_mock.get("https://fw-bk1.test/api/core/backup/download/this").mock(
        return_value=httpx.Response(200, content=b"<opnsense><hostname>fw-bk1</hostname></opnsense>")
    )

    resp = await client.post(f"/api/hosts/{host_id}/backups")
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["size"] == len(b"<opnsense><hostname>fw-bk1</hostname></opnsense>")
    assert created["id"].endswith(".xml")

    resp = await client.get(f"/api/hosts/{host_id}/backups")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    assert len(items) == 1
    assert items[0]["id"] == created["id"]


@pytest.mark.asyncio
async def test_download_stored_backup_returns_exact_bytes(client, respx_mock):
    host_id = await _create_host(client, respx_mock, "fw-bk2", "https://fw-bk2.test")

    payload = b"<opnsense><hostname>fw-bk2</hostname></opnsense>"
    respx_mock.get("https://fw-bk2.test/api/core/backup/download/this").mock(
        return_value=httpx.Response(200, content=payload)
    )

    created = (await client.post(f"/api/hosts/{host_id}/backups")).json()

    resp = await client.get(f"/api/hosts/{host_id}/backups/{created['id']}")
    assert resp.status_code == 200, resp.text
    assert resp.content == payload
    assert "attachment" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_download_unknown_backup_id_404(client, respx_mock):
    host_id = await _create_host(client, respx_mock, "fw-bk3", "https://fw-bk3.test")

    resp = await client.get(f"/api/hosts/{host_id}/backups/does-not-exist.xml")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_backup_rejects_path_traversal(client, respx_mock):
    host_id = await _create_host(client, respx_mock, "fw-bk4", "https://fw-bk4.test")

    respx_mock.get("https://fw-bk4.test/api/core/backup/download/this").mock(
        return_value=httpx.Response(200, content=b"<opnsense/>")
    )
    await client.post(f"/api/hosts/{host_id}/backups")

    resp = await client.get(f"/api/hosts/{host_id}/backups/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_trigger_backup_host_unreachable_returns_502(client, respx_mock):
    host_id = await _create_host(client, respx_mock, "fw-bk5", "https://fw-bk5.test")

    respx_mock.get("https://fw-bk5.test/api/core/backup/download/this").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    resp = await client.post(f"/api/hosts/{host_id}/backups")
    assert resp.status_code == 502
