from __future__ import annotations

from uuid import UUID

import pytest
from mcp.client import Client
from mcp.server.mcpserver import MCPServer

from engagemate.mcp import control_tools
from engagemate.mcp.control_tools import register_control_tools
from engagemate.mcp.principal import McpPrincipal


USER_ID = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_ID = UUID("22222222-2222-4222-8222-222222222222")


class FakeService:
    def __init__(self) -> None:
        self.calls = []

    def growth(self, user_id, account_id, *, days):
        self.calls.append((user_id, account_id, days))
        return {"current_followers": None, "freshness": "missing"}


@pytest.mark.asyncio
async def test_protocol_call_uses_authenticated_subject_not_tool_arguments(monkeypatch) -> None:
    principal = McpPrincipal(USER_ID, "client-test", frozenset({"stats:read"}))
    monkeypatch.setattr(control_tools, "require_principal", lambda scope: principal)
    service = FakeService()
    server = MCPServer("control-protocol-test")
    register_control_tools(server, service)

    async with Client(server, mode="legacy") as client:
        tools = await client.list_tools()
        result = await client.call_tool(
            "stats.get_growth",
            {"account_id": str(ACCOUNT_ID), "days": 30},
        )

    assert "stats.get_growth" in {tool.name for tool in tools.tools}
    assert all(not tool.name.startswith("instagram.") for tool in tools.tools)
    assert service.calls == [(USER_ID, ACCOUNT_ID, 30)]
    assert result.structured_content == {
        "current_followers": None,
        "freshness": "missing",
    }
