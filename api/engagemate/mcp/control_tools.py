"""Customer-facing control tools. Instagram execution is intentionally absent."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from engagemate.mcp.control_service import (
    AudienceTargetInput,
    LimitsPatch,
    PersonaPatch,
    SchedulePatch,
    SourceInput,
)
from engagemate.mcp.principal import require_principal


Days = Annotated[int, Field(ge=1, le=90)]


def register_control_tools(server: MCPServer, service: Any) -> None:
    @server.tool(name="accounts.list")
    async def accounts_list() -> dict[str, Any]:
        return service.list_accounts(require_principal("accounts:read"))

    @server.tool(name="accounts.get_status")
    async def accounts_get_status(account_id: str) -> dict[str, Any]:
        principal = require_principal("accounts:read")
        return service.account_status(principal, UUID(account_id))

    @server.tool(name="stats.get_overview")
    async def stats_get_overview(account_id: str, days: Days = 7) -> dict[str, Any]:
        principal = require_principal("stats:read")
        return service.overview(principal, UUID(account_id), days=days)

    @server.tool(name="stats.get_growth")
    async def stats_get_growth(account_id: str, days: Days = 30) -> dict[str, Any]:
        principal = require_principal("stats:read")
        return service.growth(principal.user_id, UUID(account_id), days=days)

    @server.tool(name="stats.get_activity")
    async def stats_get_activity(account_id: str, days: Days = 7) -> dict[str, Any]:
        principal = require_principal("stats:read")
        return service.activity(principal, UUID(account_id), days=days)

    @server.tool(name="persona.get")
    async def persona_get(account_id: str) -> dict[str, Any]:
        principal = require_principal("config:read")
        return service.persona(principal, UUID(account_id))

    @server.tool(name="persona.update")
    async def persona_update(account_id: str, patch: PersonaPatch) -> dict[str, Any]:
        principal = require_principal("config:write")
        return service.update_persona(principal, UUID(account_id), patch)

    @server.tool(name="audience.list")
    async def audience_list(account_id: str) -> dict[str, Any]:
        principal = require_principal("config:read")
        return service.audience(principal, UUID(account_id))

    @server.tool(name="audience.add")
    async def audience_add(account_id: str, target: AudienceTargetInput) -> dict[str, Any]:
        principal = require_principal("config:write")
        return service.add_audience(principal, UUID(account_id), target)

    @server.tool(name="audience.remove")
    async def audience_remove(account_id: str, target_id: str) -> dict[str, Any]:
        principal = require_principal("config:write")
        return service.remove_audience(principal, UUID(account_id), UUID(target_id))

    @server.tool(name="sources.list")
    async def sources_list(account_id: str) -> dict[str, Any]:
        principal = require_principal("config:read")
        return service.sources(principal, UUID(account_id))

    @server.tool(name="sources.add")
    async def sources_add(account_id: str, source: SourceInput) -> dict[str, Any]:
        principal = require_principal("config:write")
        return service.add_source(principal, UUID(account_id), source)

    @server.tool(name="sources.remove")
    async def sources_remove(account_id: str, source_id: str) -> dict[str, Any]:
        principal = require_principal("config:write")
        return service.remove_source(principal, UUID(account_id), UUID(source_id))

    @server.tool(name="schedule.get")
    async def schedule_get(account_id: str) -> dict[str, Any]:
        principal = require_principal("config:read")
        return service.schedule(principal, UUID(account_id))

    @server.tool(name="schedule.update")
    async def schedule_update(account_id: str, patch: SchedulePatch) -> dict[str, Any]:
        principal = require_principal("config:write")
        return service.update_schedule(principal, UUID(account_id), patch)

    @server.tool(name="limits.get")
    async def limits_get(account_id: str) -> dict[str, Any]:
        principal = require_principal("config:read")
        return service.limits(principal, UUID(account_id))

    @server.tool(name="limits.update")
    async def limits_update(account_id: str, patch: LimitsPatch) -> dict[str, Any]:
        principal = require_principal("config:write")
        return service.update_limits(principal, UUID(account_id), patch)

    @server.tool(name="changes.confirm")
    async def changes_confirm(confirmation_id: str) -> dict[str, Any]:
        principal = require_principal("config:write")
        return service.confirm_change(principal, UUID(confirmation_id))

    @server.tool(name="automation.pause")
    async def automation_pause(account_id: str) -> dict[str, Any]:
        principal = require_principal("automation:write")
        return service.pause(principal, UUID(account_id))

    @server.tool(name="automation.resume")
    async def automation_resume(account_id: str) -> dict[str, Any]:
        principal = require_principal("automation:write")
        return service.resume(principal, UUID(account_id))

    @server.tool(name="connection.get_status")
    async def connection_get_status(account_id: str) -> dict[str, Any]:
        principal = require_principal("accounts:read")
        return service.connection_status(principal, UUID(account_id))

    @server.tool(name="connection.start_desktop_setup")
    async def connection_start_desktop_setup() -> dict[str, Any]:
        principal = require_principal("accounts:read")
        return service.start_desktop_setup(principal)
