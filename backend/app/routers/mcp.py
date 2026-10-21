"""Read-only MCP server (Model Context Protocol, streamable HTTP transport).

Lets an AI assistant (Claude Code, Claude Desktop, ...) look at the fleet:

    claude mcp add --transport http opnexus https://<opnexus>/api/mcp \
        --header "Authorization: Bearer opx_..."

Stateless JSON-RPC over POST (the spec allows plain ``application/json``
replies; there is no server-initiated stream, GET answers 405). Every tool is a
GET against OPNexus' own API, sent through the app itself with the caller's own
credentials, so exactly the caller's permissions apply: a ``read`` token sees
what a ``read`` token sees, secret-bearing endpoints stay closed. Nothing here
can change state.
"""
import json
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.version import APP_VERSION

router = APIRouter(tags=["mcp"])

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_RESULT_CHARS = 60_000
# Not reachable through the generic reader (credentials management, the MCP endpoint itself).
_BLOCKED_PREFIXES = ("/api/tokens", "/api/auth", "/api/mcp")

TOOLS = [
    {
        "name": "fleet_overview",
        "description": "Fleet status at a glance: every firewall with online state and CARP role, items that need "
                       "attention (failed or unconfirmed deployments, stuck maintenance) and the latest activity.",
        "inputSchema": {"type": "object", "properties": {}},
        "path": "/api/dashboard",
    },
    {
        "name": "list_hosts",
        "description": "All managed firewalls with name, URL, cluster and whether monitoring is enabled.",
        "inputSchema": {"type": "object", "properties": {}},
        "path": "/api/hosts",
    },
    {
        "name": "host_resources",
        "description": "Recent CPU load, memory and disk usage samples of one firewall (a sample per minute).",
        "inputSchema": {
            "type": "object", "required": ["host_id"],
            "properties": {
                "host_id": {"type": "string", "description": "Firewall id from list_hosts"},
                "since_hours": {"type": "integer", "minimum": 1, "maximum": 720, "default": 1},
            },
        },
        "path": "/api/hosts/{host_id}/resource-samples",
        "query": ("since_hours",),
        # The raw per-sample payload (every interface counter) would drown the useful numbers.
        "drop": ("raw",),
    },
    {
        "name": "carp_events",
        "description": "History of CARP role changes (MASTER/BACKUP) across the clusters.",
        "inputSchema": {"type": "object", "properties": {}},
        "path": "/api/cluster/carp-events",
    },
    {
        "name": "recent_audit",
        "description": "The audit trail: who changed or deployed what, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 25},
                "kind": {"type": "string", "description": "e.g. rule, alias, host, user"},
                "status": {"type": "string", "description": "e.g. accepted, rejected, denied"},
                "q": {"type": "string", "description": "free-text filter"},
            },
        },
        "path": "/api/audit",
        "query": ("limit", "kind", "status", "q"),
    },
    {
        "name": "search",
        "description": "Search rules, aliases, NAT, VPN, certificates, groups and hosts by text.",
        "inputSchema": {
            "type": "object", "required": ["q"],
            "properties": {"q": {"type": "string", "description": "at least 2 characters"}},
        },
        "path": "/api/search",
        "query": ("q",),
    },
    {
        "name": "rule_match",
        "description": "Which firewall rule decides a packet (first packet of a new connection), per firewall, "
                       "with the verdict (pass/block/reject), the deciding rule and an honest confidence "
                       "(sure/probable/unknown plus reason codes). Reads only; nothing is changed.",
        "inputSchema": {
            "type": "object", "required": ["src", "dst"],
            "properties": {
                "src": {"type": "string", "description": "source IP address"},
                "dst": {"type": "string", "description": "destination IP address"},
                "proto": {"type": "string", "description": "tcp, udp, icmp, ipv6-icmp or a protocol number", "default": "tcp"},
                "dport": {"type": "integer", "minimum": 0, "maximum": 65535},
                "sport": {"type": "integer", "minimum": 0, "maximum": 65535, "description": "default: any ephemeral port"},
                "icmptype": {"type": "string", "description": "e.g. echoreq or 8"},
                "iface": {"type": "string", "description": "ingress interface identifier (e.g. lan); default: guessed per firewall"},
                "egress": {"type": "string", "description": "also check the outgoing side: auto or an interface identifier"},
                "host_ids": {"type": "string", "description": "comma separated firewall ids (default: all)"},
                "group_id": {"type": "string", "description": "device group id instead of host_ids"},
            },
        },
        "path": "/api/rule-analysis/match",
        "query": ("src", "dst", "proto", "dport", "sport", "icmptype", "iface", "egress", "host_ids", "group_id"),
        # The per-rule trace is for the UI; the verdict, deciding rule and reasons are what matters here.
        "drop": ("trace",),
    },
    {
        "name": "api_get",
        "description": "Read any other OPNexus API endpoint (GET only), e.g. /api/rules, /api/aliases, /api/logs. "
                       "The schema of all endpoints is at /api/openapi.json.",
        "inputSchema": {
            "type": "object", "required": ["path"],
            "properties": {
                "path": {"type": "string", "description": "Starts with /api/"},
                "query": {"type": "object", "description": "Query parameters", "additionalProperties": {"type": "string"}},
            },
        },
    },
]


def _tool_list() -> list[dict]:
    return [{k: v for k, v in tool.items() if k in ("name", "description", "inputSchema")} for tool in TOOLS]


def _text(text: str, is_error: bool = False) -> dict:
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n… truncated ({len(text)} characters in total); narrow the request."
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


async def _get(request: Request, path: str, query: dict, drop: tuple = ()) -> dict:
    if not path.startswith("/api/") or ".." in path or "?" in path or "#" in path or path.startswith(_BLOCKED_PREFIXES):
        return _text("This path cannot be read through MCP.", True)
    headers = {k: v for k in ("authorization", "cookie") if (v := request.headers.get(k))}
    url = path + (("?" + urlencode(query)) if query else "")
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://opnexus", headers=headers, timeout=30) as client:
        response = await client.get(url)
    body = response.text
    try:
        data = response.json()
        if drop and isinstance(data, list):
            data = [{k: v for k, v in item.items() if k not in drop} if isinstance(item, dict) else item for item in data]
        body = json.dumps(data, ensure_ascii=False, indent=1)
    except ValueError:
        pass
    return _text(body, response.status_code >= 400)


async def _call_tool(request: Request, name: str, arguments: dict) -> dict:
    tool = next((t for t in TOOLS if t["name"] == name), None)
    if tool is None:
        return _text(f"Unknown tool: {name}", True)
    if name == "api_get":
        query = arguments.get("query") or {}
        if not isinstance(arguments.get("path"), str) or not isinstance(query, dict):
            return _text("path (string) is required.", True)
        return await _get(request, arguments["path"], {str(k): str(v) for k, v in query.items()})
    path = tool["path"]
    if "{host_id}" in path:
        host_id = str(arguments.get("host_id", ""))
        if not host_id.replace("-", "").isalnum():
            return _text("host_id is required (see list_hosts).", True)
        path = path.format(host_id=host_id)
    query = {key: arguments[key] for key in tool.get("query", ()) if arguments.get(key) not in (None, "")}
    return await _get(request, path, query, tool.get("drop", ()))


def _error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def _handle(request: Request, message: dict) -> dict | None:
    """The JSON-RPC response for one message, None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid request")
    method, request_id = message["method"], message.get("id")
    params = message.get("params") or {}
    if "id" not in message:  # notification (e.g. notifications/initialized): nothing to answer
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "opnexus", "version": APP_VERSION},
            "instructions": "Read-only access to an OPNsense fleet managed by OPNexus.",
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _tool_list()}}
    if method == "tools/call":
        arguments = params.get("arguments") or {}
        if not isinstance(params.get("name"), str) or not isinstance(arguments, dict):
            return _error(request_id, -32602, "name (string) and arguments (object) are required")
        return {"jsonrpc": "2.0", "id": request_id, "result": await _call_tool(request, params["name"], arguments)}
    return _error(request_id, -32601, f"Method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)
    if isinstance(payload, list):
        replies = [reply for message in payload if (reply := await _handle(request, message)) is not None]
        return JSONResponse(replies) if replies else Response(status_code=202)
    reply = await _handle(request, payload)
    return JSONResponse(reply) if reply is not None else Response(status_code=202)


@router.get("/mcp")
async def mcp_no_stream():
    # No server-initiated messages: tell clients not to expect an SSE stream.
    return Response(status_code=405, headers={"Allow": "POST"})
