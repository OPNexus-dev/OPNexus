import httpx
import pytest

from app.config import settings
from app.services import update_check_service
from app.version import APP_VERSION

FEED = "https://feed.test/api/v1/repos/o/r/releases/latest"


@pytest.fixture(autouse=True)
def _feed(monkeypatch):
    monkeypatch.setattr(settings, "update_check_url", FEED)
    update_check_service.reset_cache()
    yield
    update_check_service.reset_cache()


def _bump(version: str, major: int = 0, minor: int = 0, patch: int = 0) -> str:
    a, b, c = (int(x) for x in version.split("."))
    return f"{a + major}.{b + minor}.{c + patch}"


@pytest.mark.asyncio
async def test_disabled_without_url_makes_no_request(client, respx_mock, monkeypatch):
    monkeypatch.setattr(settings, "update_check_url", "")
    resp = await client.get("/api/system/version-check")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False and body["update_available"] is False
    assert body["current"] == APP_VERSION
    assert len(respx_mock.calls) == 0


@pytest.mark.asyncio
async def test_newer_release_is_reported(client, respx_mock):
    newer = _bump(APP_VERSION, minor=1)
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, json={
        "tag_name": f"v{newer}", "html_url": "https://git.test/o/r/releases/tag/v" + newer,
    }))
    body = (await client.get("/api/system/version-check")).json()
    assert body["enabled"] is True
    assert body["latest"] == newer and body["update_available"] is True
    assert body["release_url"] == "https://git.test/o/r/releases/tag/v" + newer
    assert body["error"] is None and body["checked_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", [APP_VERSION, "v" + APP_VERSION, "0.0.1"])
async def test_same_or_older_release_is_not_an_update(client, respx_mock, tag):
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, json={"tag_name": tag}))
    body = (await client.get("/api/system/version-check")).json()
    assert body["update_available"] is False and body["error"] is None


@pytest.mark.asyncio
async def test_prerelease_tag_is_never_offered(client, respx_mock):
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, json={"tag_name": "v99.0.0-rc1"}))
    body = (await client.get("/api/system/version-check")).json()
    assert body["update_available"] is False and body["latest"] is None


@pytest.mark.asyncio
async def test_non_http_release_url_is_dropped(client, respx_mock):
    newer = _bump(APP_VERSION, patch=1)
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, json={
        "tag_name": f"v{newer}", "html_url": "javascript:alert(1)",
    }))
    body = (await client.get("/api/system/version-check")).json()
    assert body["update_available"] is True and body["release_url"] is None


@pytest.mark.asyncio
async def test_result_is_cached_between_requests(client, respx_mock):
    route = respx_mock.get(FEED).mock(return_value=httpx.Response(200, json={"tag_name": "v0.0.1"}))
    await client.get("/api/system/version-check")
    await client.get("/api/system/version-check")
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    httpx.Response(500),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"no_tag": True}),
    httpx.Response(200, json={"tag_name": 5}),
])
async def test_broken_feed_degrades_quietly_and_backs_off(client, respx_mock, response):
    route = respx_mock.get(FEED).mock(return_value=response)
    first = await client.get("/api/system/version-check")
    assert first.status_code == 200
    body = first.json()
    assert body["enabled"] is True and body["update_available"] is False and body["error"] == "unavailable"
    await client.get("/api/system/version-check")
    assert route.call_count == 1  # kein Hämmern auf einen kaputten Feed


@pytest.mark.asyncio
async def test_unreachable_feed_keeps_last_known_result(client, respx_mock, monkeypatch):
    newer = _bump(APP_VERSION, minor=1)
    respx_mock.get(FEED).mock(return_value=httpx.Response(200, json={"tag_name": f"v{newer}"}))
    assert (await client.get("/api/system/version-check")).json()["update_available"] is True

    # Cache ablaufen lassen, Feed fällt aus: der letzte bekannte Stand bleibt.
    update_check_service._state.checked_at = update_check_service._state.checked_at.replace(year=2000)
    respx_mock.get(FEED).mock(side_effect=httpx.ConnectError("down"))
    body = (await client.get("/api/system/version-check")).json()
    assert body["update_available"] is True and body["latest"] == newer
    assert body["error"] == "unavailable"


@pytest.mark.asyncio
async def test_requires_login(anon_client):
    assert (await anon_client.get("/api/system/version-check")).status_code == 401


# ── Einstellung im Webinterface (statt .env) ────────────────────────────

WEB_FEED = "https://mirror.test/latest.json"


@pytest.fixture
def _env_unset(monkeypatch):
    monkeypatch.setattr(settings, "update_check_url", "")


async def _reset_row(client):
    await client.put("/api/system/update-check", json={"enabled": False, "feed_url": "https://updates.opnexus.dev/latest.json"})


@pytest.mark.asyncio
async def test_check_is_off_by_default_and_can_be_switched_on_in_the_ui(client, respx_mock, _env_unset):
    await _reset_row(client)
    body = (await client.get("/api/system/update-check")).json()
    assert body == {"enabled": False, "feed_url": "https://updates.opnexus.dev/latest.json", "managed_by_server": False}
    assert (await client.get("/api/system/version-check")).json()["enabled"] is False
    assert len(respx_mock.calls) == 0

    newer = _bump(APP_VERSION, minor=1)
    respx_mock.get(WEB_FEED).mock(return_value=httpx.Response(200, json={"tag_name": f"v{newer}"}))
    resp = await client.put("/api/system/update-check", json={"enabled": True, "feed_url": f" {WEB_FEED} "})
    assert resp.status_code == 200 and resp.json()["feed_url"] == WEB_FEED and resp.json()["enabled"] is True
    check = (await client.get("/api/system/version-check")).json()
    assert check["enabled"] is True and check["latest"] == newer and check["update_available"] is True
    await _reset_row(client)


@pytest.mark.asyncio
async def test_switching_the_check_off_again_stops_requests(client, respx_mock, _env_unset):
    respx_mock.get(WEB_FEED).mock(return_value=httpx.Response(200, json={"tag_name": f"v{_bump(APP_VERSION, minor=1)}"}))
    await client.put("/api/system/update-check", json={"enabled": True, "feed_url": WEB_FEED})
    assert (await client.get("/api/system/version-check")).json()["latest"]
    await client.put("/api/system/update-check", json={"enabled": False})
    after = (await client.get("/api/system/version-check")).json()
    assert after["enabled"] is False and after["latest"] is None
    await _reset_row(client)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["", "ftp://x.test/f", "javascript:alert(1)", "https://a b.test/", "https://x.test/" + "a" * 500])
async def test_invalid_feed_urls_are_rejected(client, _env_unset, url):
    resp = await client.put("/api/system/update-check", json={"feed_url": url})
    assert resp.status_code in (422, 400)
    if resp.status_code == 422 and isinstance(resp.json().get("detail"), dict):
        assert resp.json()["detail"]["code"] == "UPDATE_CHECK_URL_INVALID"


@pytest.mark.asyncio
async def test_env_setting_wins_and_locks_the_ui(client, respx_mock):
    # the autouse fixture sets UPDATE_CHECK_URL = FEED, i.e. the server decides
    body = (await client.get("/api/system/update-check")).json()
    assert body == {"enabled": True, "feed_url": FEED, "managed_by_server": True}
    resp = await client.put("/api/system/update-check", json={"enabled": False})
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "UPDATE_CHECK_MANAGED_BY_SERVER"


@pytest.mark.asyncio
async def test_only_admins_can_read_or_change_the_setting(client, _env_unset):
    from tests.test_users import _login_as
    await client.post("/api/users", json={"username": "viewer-upc", "password": "password-123456", "role": "read_only"})
    viewer = await _login_as("viewer-upc", "password-123456")
    try:
        assert (await viewer.get("/api/system/update-check")).status_code == 403
        assert (await viewer.put("/api/system/update-check", json={"enabled": True})).status_code == 403
    finally:
        await viewer.aclose()
