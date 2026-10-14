from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from engagemate.domain.budgets import ActionBudget
from engagemate.intelligence.schemas import Persona
from engagemate.intelligence.text_gate import TextGate
from engagemate.worker.active_run import ActiveRunRegistry
from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository
from engagemate.worker.execution_mcp_server import build_execution_mcp
from engagemate.worker.hermes_run import HermesRunClient
from engagemate.worker.story_types import (
    StoryContent,
    StoryMediaType,
    StoryOpportunity,
)
from mcp.client import Client
from pydantic import SecretStr

from tests.worker import test_execution_mcp_repository_postgres as postgres_fixtures

pool = postgres_fixtures.pool
world = postgres_fixtures.world


KEY = "T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-"
SESSION_ID = "em_0123456789abcdefghijklmnopqrstuv"
EXPECTED_TOOLS = {
    "instagram.discover",
    "instagram.view",
    "instagram.like",
    "instagram.reply",
    "instagram.complete_run",
}


class _FixtureDiscovery:
    def __init__(self, world) -> None:
        self._world = world

    async def find(self, context, targets, budget):
        return [
            StoryOpportunity(
                target_id=self._world["target_id"],
                owner_id="fixture-owner",
                target_handle="fixture.target",
                tier="vip",
                media_id="fixture-media",
                taken_at=datetime.now(UTC),
                fingerprint="ab" * 32,
                content=StoryContent(
                    StoryMediaType.IMAGE,
                    "fixture story",
                    None,
                    None,
                    (),
                ),
            )
        ]


class _NoNetworkExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, opportunity, decision, context):
        self.calls += 1
        raise AssertionError("disabled execution reached the Instagram adapter")


@pytest.mark.asyncio
async def test_fixture_gateway_drives_a_disabled_audited_execution_loop(
    pool, world
) -> None:
    repository = ExecutionMcpRepository(pool)
    registry = ActiveRunRegistry()
    executor = _NoNetworkExecutor()

    async def lease_is_live() -> bool:
        return True

    async def pace(action, opportunity) -> None:
        return None

    active = SimpleNamespace(
        completed=False,
        stopping=False,
        tool_tasks=set(),
        check_deadline=None,
        config_snapshot={},
        completion_reason=None,
        completion_counters={},
        run_id=world["run_id"],
        job=SimpleNamespace(id=world["job_id"], claimed_by="worker-1"),
        context=SimpleNamespace(
            user_id=world["user_id"],
            ig_account_id=world["account_id"],
            username="fixture.account",
            persona=Persona("A concise founder."),
            targets=(),
            history=SimpleNamespace(
                recent_interaction_summary="No prior contact",
                sent_messages=(),
            ),
            banned_phrases=(),
            expected_language="en",
        ),
        budget=ActionBudget(1, 1, 1, plan_total=3, views=1),
        text_gate=TextGate(),
        discovery_context=SimpleNamespace(),
        execution_context=SimpleNamespace(),
        discovery=_FixtureDiscovery(world),
        executor=executor,
        lease_is_live=lease_is_live,
        pace=pace,
        opportunities={},
        tool_repository=repository,
        execution_mode="disabled",
    )
    execution_server = build_execution_mcp(registry, KEY)
    observed: dict[str, object] = {}
    session_deleted = False

    async def fixture_gateway(request: httpx.Request) -> httpx.Response:
        nonlocal session_deleted
        if request.method == "DELETE":
            session_deleted = True
            return httpx.Response(200, json={"deleted": True})

        async with Client(execution_server, mode="legacy") as client:
            tools = await client.list_tools()
            discovered = await client.call_tool("instagram.discover", {"limit": 1})
            opportunity_id = discovered.structured_content["opportunities"][0][
                "opportunity_id"
            ]
            mutation = await client.call_tool(
                "instagram.like",
                {
                    "opportunity_id": opportunity_id,
                    "idempotency_key": "fixture_disabled_0001",
                },
            )
            completion = await client.call_tool(
                "instagram.complete_run", {"reason": "completed"}
            )
        observed["tools"] = {tool.name for tool in tools.tools}
        observed["mutation"] = mutation.structured_content
        observed["completion"] = completion.structured_content
        return httpx.Response(
            200,
            headers={"X-Hermes-Session-Id": request.headers["X-Hermes-Session-Id"]},
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Run completed."},
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(fixture_gateway)
    ) as http:
        with registry.activate(active):
            result = await HermesRunClient(
                http=http,
                endpoint="http://127.0.0.1:8643/v1/chat/completions",
                api_key=SecretStr("h" * 48),
            ).run(active, session_id=SESSION_ID)

    with pool.connection() as connection:
        run_row = connection.execute(
            "SELECT status, stop_reason FROM hermes_execution_runs WHERE id = %s",
            (world["run_id"],),
        ).fetchone()
        tool_row = connection.execute(
            """
            SELECT status, failure_class
            FROM hermes_tool_calls
            WHERE run_id = %s
            """,
            (world["run_id"],),
        ).fetchone()

    assert observed["tools"] == EXPECTED_TOOLS
    assert observed["mutation"] == {
        "status": "denied",
        "reason": "execution_disabled",
    }
    assert observed["completion"]["counters"]["denied"] == 1
    assert executor.calls == 0
    assert session_deleted is True
    assert result.session_id == SESSION_ID
    assert run_row == {"status": "completed", "stop_reason": "completed"}
    assert tool_row == {"status": "denied", "failure_class": "execution_disabled"}
