import httpx
import pytest

from tests.test_users import _login_as

from app.config import settings
from app.services import update_check_service
from app.version import APP_VERSION

FEED = "https://feed.test/api/v1/repos/o/r/releases/latest"
UPDATER = "http://updater.test:8080"


def _bump(minor=0, patch=0, major=0):
    a, b, c = (int(x) for x in APP_VERSION.split("."))
    return f"{a + major}.{b + minor}.{c + patch}"


@pytest.fixture(autouse=True)
def _setup(monkeypatch, tmp_path):
    token = tmp_path / "token"
    token.write_text("t" * 40)
    monkeypatch.setattr(settings, "update_check_url", FEED)
    monkeypatch.setattr(settings, "updater_url", UPDATER)
    monkeypatch.setattr(settings, "updater_token_file", str(token))
    update_check_service.reset_cache()
    yield
    update_check_service.reset_cache()


def _feed(respx_mock, version):
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, json={"tag_name": f"v{version}"}))


def _updater_status(respx_mock, **over):
    body = {"mode": "release", "running_version": APP_VERSION, "job": None, "phases": [], "log": [],
            "services": {"backend": "healthy"}, "rollback_available": False, "rollback_loses_data": False,
            "last_update": None}
    body.update(over)
    return respx_mock.get(f"{UPDATER}/status").mock(return_value=httpx.Response(200, json=body))


@pytest.mark.asyncio
async def test_status_reports_update_and_can_update(client, respx_mock):
    _feed(respx_mock, _bump(minor=1))
    _updater_status(respx_mock)
    body = (await client.get("/api/system/updater")).json()
    assert body["available"] is True and body["can_update"] is True
    assert body["latest"] == _bump(minor=1) and body["major_change"] is False


@pytest.mark.asyncio
async def test_status_without_token_is_unavailable_not_an_error(client, respx_mock, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "updater_token_file", str(tmp_path / "missing"))
    _feed(respx_mock, _bump(minor=1))
    body = (await client.get("/api/system/updater")).json()
    assert body["available"] is False and body["reason"] == "not_enabled" and body["can_update"] is False
    assert body["update_available"] is True  # der Hinweis bleibt trotzdem


@pytest.mark.asyncio
async def test_status_unreachable_updater(client, respx_mock):
    _feed(respx_mock, _bump(minor=1))
    respx_mock.get(f"{UPDATER}/status").mock(side_effect=httpx.ConnectError("down"))
    body = (await client.get("/api/system/updater")).json()
    assert body["available"] is False and body["reason"] == "unreachable"


@pytest.mark.asyncio
async def test_major_change_is_not_offered_for_one_click(client, respx_mock):
    _feed(respx_mock, _bump(major=1))
    _updater_status(respx_mock)
    body = (await client.get("/api/system/updater")).json()
    assert body["major_change"] is True and body["can_update"] is False
    resp = await client.post("/api/system/updater/update", json={"version": _bump(major=1)})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "UPDATE_MAJOR_CHANGE"


@pytest.mark.asyncio
async def test_local_build_install_cannot_update(client, respx_mock):
    _feed(respx_mock, _bump(minor=1))
    _updater_status(respx_mock, mode="local")
    body = (await client.get("/api/system/updater")).json()
    assert body["can_update"] is False and body["mode"] == "local"


@pytest.mark.asyncio
async def test_update_forwards_only_the_feed_version(client, respx_mock):
    newer = _bump(patch=1)
    _feed(respx_mock, newer)
    route = respx_mock.post(f"{UPDATER}/update").mock(return_value=httpx.Response(202, json={"job": {"id": "x"}}))
    resp = await client.post("/api/system/updater/update", json={"version": newer})
    assert resp.status_code == 202
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer " + "t" * 40
    assert b'"version": "%s"' % newer.encode() in sent.content or newer.encode() in sent.content


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["0.0.1", "99.99.99", "1.0.0-rc1", "latest", "../../x"])
async def test_update_rejects_any_other_version(client, respx_mock, version):
    _feed(respx_mock, _bump(patch=1))
    route = respx_mock.post(f"{UPDATER}/update").mock(return_value=httpx.Response(202, json={}))
    resp = await client.post("/api/system/updater/update", json={"version": version})
    assert resp.status_code in (409, 422)
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_update_when_no_update_available(client, respx_mock):
    _feed(respx_mock, APP_VERSION)
    resp = await client.post("/api/system/updater/update", json={"version": APP_VERSION})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "UPDATE_NOT_OFFERED"


@pytest.mark.asyncio
async def test_busy_and_rejected_map_to_error_codes(client, respx_mock):
    newer = _bump(patch=1)
    _feed(respx_mock, newer)
    route = respx_mock.post(f"{UPDATER}/update")
    route.mock(return_value=httpx.Response(409, json={"error": "busy"}))
    resp = await client.post("/api/system/updater/update", json={"version": newer})
    assert resp.json()["detail"]["code"] == "UPDATER_BUSY"
    route.mock(return_value=httpx.Response(409, json={"error": "not_release_mode"}))
    resp = await client.post("/api/system/updater/update", json={"version": newer})
    assert resp.json()["detail"] == {"code": "UPDATER_REJECTED", "params": {"reason": "not_release_mode"}}


@pytest.mark.asyncio
async def test_update_without_updater_is_503(client, respx_mock, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "updater_token_file", str(tmp_path / "missing"))
    newer = _bump(patch=1)
    _feed(respx_mock, newer)
    resp = await client.post("/api/system/updater/update", json={"version": newer})
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "UPDATER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_rollback_is_forwarded(client, respx_mock):
    route = respx_mock.post(f"{UPDATER}/rollback").mock(return_value=httpx.Response(202, json={"job": {"id": "r"}}))
    resp = await client.post("/api/system/updater/rollback")
    assert resp.status_code == 202 and route.call_count == 1



@pytest.mark.asyncio
async def test_read_only_user_cannot_read_or_start_updates(client, respx_mock):
    newer = _bump(patch=1)
    _feed(respx_mock, newer)
    status = _updater_status(respx_mock)
    route = respx_mock.post(f"{UPDATER}/update").mock(return_value=httpx.Response(202, json={}))
    await client.post("/api/users", json={"username": "viewer-upd", "password": "password-123456", "role": "read_only"})
    viewer = await _login_as("viewer-upd", "password-123456")
    try:
        assert (await viewer.get("/api/system/updater")).status_code == 403
        assert (await viewer.post("/api/system/updater/update", json={"version": newer})).status_code == 403
        assert (await viewer.post("/api/system/updater/rollback")).status_code == 403
    finally:
        await viewer.aclose()
    assert status.call_count == 0 and route.call_count == 0


@pytest.mark.asyncio
async def test_anonymous_is_rejected(anon_client):
    assert (await anon_client.get("/api/system/updater")).status_code == 401
    assert (await anon_client.post("/api/system/updater/rollback")).status_code == 401


@pytest.mark.asyncio
async def test_update_request_is_audited(client, respx_mock):
    newer = _bump(patch=1)
    _feed(respx_mock, newer)
    respx_mock.post(f"{UPDATER}/update").mock(return_value=httpx.Response(202, json={"job": {"id": "x"}}))
    assert (await client.post("/api/system/updater/update", json={"version": newer})).status_code == 202
    events = (await client.get("/api/audit?kind=system")).json()["items"]
    assert {(e["action"], e["status"]) for e in events} >= {("update", "requested"), ("update", "accepted")}
