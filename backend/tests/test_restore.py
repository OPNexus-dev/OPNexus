import uuid
import httpx
import pytest
from app.services import backup_store
from tests.test_rules import _create_host, _rule_payload

VALID_XML = (
    b"<opnsense><system><hostname>fw1</hostname><domain>test.local</domain>"
    b"<webgui><protocol>https</protocol><port>443</port></webgui>"
    b'<user><name>root</name><password>$2y$secrethash</password></user>'
    b"</system><interfaces><wan><if>igb0</if></wan></interfaces></opnsense>"
)


def mock_backup_download(respx_mock, base="https://fw1.test", content=VALID_XML):
    respx_mock.get(url__regex=r".*/getVipStatus").mock(return_value=httpx.Response(200, json={"rows": []}))
    respx_mock.get(f"{base}/api/core/backup/download/this").mock(
        return_value=httpx.Response(200, content=content)
    )


async def do_preview(client, h, backup_id):
    return await client.post(f"/api/hosts/{h['id']}/restore/preview", json={"backup_id": backup_id})


async def do_handoff(client, h, op_id, **kwargs):
    return await client.post(f"/api/hosts/{h['id']}/restore/{op_id}/handoff", json={"confirm_name": h["name"], **kwargs})


async def do_confirm(client, h, op_id, **kwargs):
    return await client.post(f"/api/hosts/{h['id']}/restore/{op_id}/confirm", json={"confirm_name": h["name"], **kwargs})


@pytest.mark.asyncio
async def test_preview_handoff_confirm_happy_path(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)

    p = await do_preview(client, h, backup_id)
    assert p.status_code == 200, p.text
    body = p.json()
    assert body["status"] == "preview"
    assert body["manifest"]["hostname"] == "fw1"
    assert body["manifest"]["secret_fields_redacted"] == 1
    assert "$2y$secrethash" not in str(body)

    op_id = body["id"]
    ho = await do_handoff(client, h, op_id)
    assert ho.status_code == 200, ho.text
    assert ho.json()["status"] == "handed_off"

    co = await do_confirm(client, h, op_id, message="Lief sauber.")
    assert co.status_code == 200, co.text
    assert co.json()["status"] == "verified"
    assert "Lief sauber." in co.json()["message"]


@pytest.mark.asyncio
async def test_oversized_backup_rejected(client, respx_mock, monkeypatch):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    monkeypatch.setattr("app.services.restore_service.MAX_BACKUP_BYTES", 10)
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)
    resp = await do_preview(client, h, backup_id)
    assert resp.status_code == 400
    assert resp.json()["detail"] == {"code": "BACKUP_TOO_LARGE", "params": {"limit": 0}}


@pytest.mark.asyncio
async def test_wrong_root_element_rejected(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), b"<html><body>not a backup</body></html>")
    resp = await do_preview(client, h, backup_id)
    assert resp.status_code == 400
    assert resp.json()["detail"] == {"code": "BACKUP_WRONG_ROOT", "params": {"root": "html"}}


@pytest.mark.asyncio
async def test_xxe_payload_rejected(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    xxe = (
        b'<?xml version="1.0"?><!DOCTYPE opnsense [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b"<opnsense>&xxe;</opnsense>"
    )
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), xxe)
    resp = await do_preview(client, h, backup_id)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_backup_returns_404(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    resp = await do_preview(client, h, "20200101T000000000000.xml")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_fingerprint_mismatch_blocks_handoff(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)
    op_id = (await do_preview(client, h, backup_id)).json()["id"]

    # Overwrite the same file after preview but before handoff.
    backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)  # different filename, doesn't touch backup_id...
    path = backup_store._host_dir(uuid.UUID(h["id"])) / backup_id
    path.write_bytes(VALID_XML + b"<!-- tampered -->")

    resp = await do_handoff(client, h, op_id)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"].startswith("SICHERUNG_WURDE_SEIT_DER_VORSCHAU_VERAENDERT")


@pytest.mark.asyncio
async def test_confirm_name_mismatch(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)
    op_id = (await do_preview(client, h, backup_id)).json()["id"]
    resp = await do_handoff(client, h, op_id, confirm_name="wrong-name")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_expired_preview_blocks_handoff(client, respx_mock):
    import app.routers.restore as restore_router
    from datetime import timedelta
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)
    original = restore_router.PREVIEW_LIFETIME
    restore_router.PREVIEW_LIFETIME = timedelta(seconds=-1)
    try:
        op_id = (await do_preview(client, h, backup_id)).json()["id"]
    finally:
        restore_router.PREVIEW_LIFETIME = original
    resp = await do_handoff(client, h, op_id)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "VORSCHAU_ABGELAUFEN_BITTE_ERNEUT_OEFFNEN"


@pytest.mark.asyncio
async def test_confirm_before_handoff_rejected(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)
    op_id = (await do_preview(client, h, backup_id)).json()["id"]
    resp = await do_confirm(client, h, op_id)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_cancel_from_preview_and_handed_off(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)

    op1 = (await do_preview(client, h, backup_id)).json()["id"]
    c1 = await client.post(f"/api/hosts/{h['id']}/restore/{op1}/cancel")
    assert c1.status_code == 200 and c1.json()["status"] == "cancelled"
    assert (await do_handoff(client, h, op1)).status_code == 409

    op2 = (await do_preview(client, h, backup_id)).json()["id"]
    await do_handoff(client, h, op2)
    c2 = await client.post(f"/api/hosts/{h['id']}/restore/{op2}/cancel")
    assert c2.status_code == 200 and c2.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_open_restore_blocks_rule_deploy_and_clears_on_confirm(client, respx_mock):
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)

    rule_id = (await client.post("/api/rules", json=_rule_payload([h["id"]]))).json()["id"]
    respx_mock.post("https://fw1.test/api/firewall/filter/addRule").mock(return_value=httpx.Response(200, json={"result": "saved", "uuid": "abc-123"}))
    respx_mock.post("https://fw1.test/api/firewall/filter/apply").mock(return_value=httpx.Response(200, json={"status": "ok"}))

    op_id = (await do_preview(client, h, backup_id)).json()["id"]
    await do_handoff(client, h, op_id)

    blocked = await client.post(f"/api/rules/{rule_id}/deploy")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "OPEN_RESTORE_BLOCKS_DEPLOY"

    await do_confirm(client, h, op_id)
    allowed = await client.post(f"/api/rules/{rule_id}/deploy")
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_open_restore_blocks_reboot_preview(client, respx_mock):
    from tests.test_maintenance import mock_reads
    mock_reads(respx_mock)
    mock_backup_download(respx_mock)
    h = await _create_host(client, "fw1", "https://fw1.test")
    backup_id = backup_store.save_snapshot(uuid.UUID(h["id"]), VALID_XML)

    op_id = (await do_preview(client, h, backup_id)).json()["id"]
    await do_handoff(client, h, op_id)

    resp = await client.post(f"/api/hosts/{h['id']}/maintenance/preview", json={"action": "reboot"})
    assert resp.status_code == 409

    await do_confirm(client, h, op_id)
    resp = await client.post(f"/api/hosts/{h['id']}/maintenance/preview", json={"action": "reboot"})
    assert resp.status_code == 200, resp.text
