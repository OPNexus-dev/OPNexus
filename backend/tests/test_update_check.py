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
