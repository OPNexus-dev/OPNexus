"""Read-only MCP endpoint: protocol basics, tools, and that it cannot be used to escalate."""
import pytest

from tests.test_api_tokens import _bearer, _make


def _rpc(method, params=None, id_=1):
    message = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        message["id"] = id_
    if params is not None:
        message["params"] = params
    return message


async def _call(api, name, **arguments):
    resp = await api.post("/api/mcp", json=_rpc("tools/call", {"name": name, "arguments": arguments}))
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


@pytest.mark.asyncio
async def test_requires_authentication(anon_client):
    assert (await anon_client.post("/api/mcp", json=_rpc("ping"))).status_code == 401


@pytest.mark.asyncio
async def test_handshake_and_tool_list_with_a_read_token(client):
    token = await _make(client, "read")
    async with _bearer(token["token"]) as api:
        init = (await api.post("/api/mcp", json=_rpc("initialize", {"protocolVersion": "2025-06-18"}))).json()["result"]
        assert init["protocolVersion"] == "2025-06-18"
        assert init["serverInfo"]["name"] == "opnexus" and "tools" in init["capabilities"]
        # Unknown future version: answer with one we speak.
        other = (await api.post("/api/mcp", json=_rpc("initialize", {"protocolVersion": "2099-01-01"}))).json()["result"]
        assert other["protocolVersion"] == "2025-06-18"
        # A notification gets no body.
        note = await api.post("/api/mcp", json=_rpc("notifications/initialized", id_=None))
        assert note.status_code == 202 and note.content == b""
        tools = (await api.post("/api/mcp", json=_rpc("tools/list"))).json()["result"]["tools"]
        assert {"fleet_overview", "list_hosts", "recent_audit", "search", "api_get"} <= {t["name"] for t in tools}
        assert all("inputSchema" in t and "path" not in t for t in tools)
        assert (await api.post("/api/mcp", json=_rpc("ping"))).json()["result"] == {}
        unknown = (await api.post("/api/mcp", json=_rpc("resources/list"))).json()
        assert unknown["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_get_is_405_and_bad_json_is_a_parse_error(client):
    assert (await client.get("/api/mcp")).status_code == 405
    bad = await client.post("/api/mcp", content=b"{nope", headers={"Content-Type": "application/json"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_tools_read_through_the_callers_own_permissions(client):
    await client.post("/api/groups", json={"name": "findable-group"})
    token = await _make(client, "read")
    async with _bearer(token["token"]) as api:
        hosts = await _call(api, "list_hosts")
        assert hosts["isError"] is False and hosts["content"][0]["text"] == "[]"
        found = await _call(api, "search", q="findable")
        assert "findable-group" in found["content"][0]["text"]
        audit = await _call(api, "recent_audit", limit=5)
        assert audit["isError"] is False
        overview = await _call(api, "fleet_overview")
        assert "pending_actions" in overview["content"][0]["text"]


@pytest.mark.asyncio
async def test_generic_reader_cannot_reach_credentials_or_secrets_and_never_writes(client):
    token = await _make(client, "read")
    async with _bearer(token["token"]) as api:
        for path in ("/api/tokens", "/api/auth/me", "/api/mcp", "/etc/passwd", "/api/../x", "/api/hosts?x=1", "relative"):
            result = await _call(api, "api_get", path=path)
            assert result["isError"] is True, path
        # Secret-bearing endpoint: refused by the caller's own (read) scope, not by the tool.
        denied = await _call(api, "api_get", path="/api/notifications/settings")
        assert denied["isError"] is True and "API_TOKEN_SCOPE_FORBIDDEN" in denied["content"][0]["text"]
        # api_get is GET-only by construction: there is no way to name another method.
        tools = (await api.post("/api/mcp", json=_rpc("tools/list"))).json()["result"]["tools"]
        schema = next(t for t in tools if t["name"] == "api_get")["inputSchema"]
        assert set(schema["properties"]) == {"path", "query"}


@pytest.mark.asyncio
async def test_unknown_tool_and_bad_arguments_are_tool_errors(client):
    token = await _make(client, "read")
    async with _bearer(token["token"]) as api:
        assert (await _call(api, "delete_everything"))["isError"] is True
        assert (await _call(api, "host_resources"))["isError"] is True
        bad = await api.post("/api/mcp", json=_rpc("tools/call", {"arguments": {}}))
        assert bad.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_read_only_users_session_can_use_it_too(client):
    resp = await client.post("/api/users", json={"username": "mcpviewer", "password": "viewer-password-123", "role": "read_only"})
    assert resp.status_code == 201
    from tests.test_api_tokens import _login
    viewer = await _login("mcpviewer", "viewer-password-123")
    try:
        assert (await viewer.post("/api/mcp", json=_rpc("ping"))).status_code == 200
    finally:
        await viewer.aclose()
