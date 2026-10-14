"""Loopback-only private MCP server for one active Hermes execution run."""

from __future__ import annotations

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl

from engagemate.worker.active_run import ActiveRunRegistry
from engagemate.worker.execution_mcp_auth import ExecutionTokenVerifier
from engagemate.worker.execution_mcp_tools import register_execution_tools


def build_execution_mcp(registry: ActiveRunRegistry, key: str) -> MCPServer:
    server = MCPServer(
        "engagemate-execution",
        version="1.0.0",
        token_verifier=ExecutionTokenVerifier(key),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl("http://localhost"),
            resource_server_url=None,
            required_scopes=["instagram:execute"],
        ),
    )
    register_execution_tools(server, registry)
    return server


def build_execution_mcp_app(
    registry: ActiveRunRegistry,
    key: str,
    *,
    host: str = "127.0.0.1",
):
    if host != "127.0.0.1":
        raise ValueError("execution MCP server must bind to IPv4 loopback")
    app = build_execution_mcp(registry, key).streamable_http_app(
        streamable_http_path="/mcp",
        host=host,
        json_response=True,
    )

    async def finish_stream(scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "GET" or scope["path"] != "/mcp":
            return await app(scope, receive, send)
        started = finished = False

        async def track(message):
            nonlocal started, finished
            started |= message["type"] == "http.response.start"
            finished |= message["type"] == "http.response.body" and not message.get("more_body", False)
            await send(message)

        await app(scope, receive, track)
        # SDK stream shutdown can return before the terminal ASGI body. Finish
        # this GET only; POST tool cancellation and receipt ownership stay native.
        if started and not finished:
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    return finish_stream
