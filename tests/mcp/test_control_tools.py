from __future__ import annotations

from types import SimpleNamespace

import pytest

from engagemate.mcp.control_server import build_control_mcp


EXPECTED = {
    "accounts.list",
    "accounts.get_status",
    "stats.get_overview",
    "stats.get_growth",
    "stats.get_activity",
    "persona.get",
    "persona.update",
    "audience.list",
    "audience.add",
    "audience.remove",
    "sources.list",
    "sources.add",
    "sources.remove",
    "schedule.get",
    "schedule.update",
    "limits.get",
    "limits.update",
    "changes.confirm",
    "automation.pause",
    "automation.resume",
    "connection.get_status",
    "connection.start_desktop_setup",
}


@pytest.fixture()
def server():
    settings = SimpleNamespace(
        mcp_issuer="https://mcp.example.com",
        mcp_resource_url="https://mcp.example.com/mcp",
        mcp_consent_url="https://app.example.com/mcp/authorize",
        mcp_redirect_hosts=("agent.example", "127.0.0.1", "localhost", "::1"),
        public_app_url="https://app.example.com",
    )
    return build_control_mcp(settings, SimpleNamespace())


@pytest.mark.asyncio
async def test_control_server_exposes_only_expected_tools(server) -> None:
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == EXPECTED
    assert all(not tool.name.startswith("instagram.") for tool in tools)


@pytest.mark.asyncio
async def test_public_tool_arguments_exclude_tenant_secrets_and_arbitrary_urls(server) -> None:
    forbidden = {"user_id", "cookies", "cookie", "proxy", "url", "sql"}
    for tool in await server.list_tools():
        properties = set(tool.input_schema.get("properties", {}))
        assert properties.isdisjoint(forbidden), tool.name

    confirm = next(tool for tool in await server.list_tools() if tool.name == "changes.confirm")
    assert set(confirm.input_schema["properties"]) == {"confirmation_id"}


@pytest.mark.asyncio
async def test_stats_days_are_bounded_in_the_protocol_schema(server) -> None:
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in ("stats.get_overview", "stats.get_growth", "stats.get_activity"):
        days = tools[name].input_schema["properties"]["days"]
        assert days["minimum"] == 1
        assert days["maximum"] == 90
