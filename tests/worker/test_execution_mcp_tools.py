from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from engagemate.domain.budgets import ActionBudget
from engagemate.intelligence.text_gate import TextGate
from engagemate.worker.active_run import ActiveRun, ActiveRunRegistry
from engagemate.worker.execution_mcp_server import build_execution_mcp
from engagemate.worker.execution_mcp_tools import _completion_response_counters
from engagemate.worker.executor import ExecutionResult, ExecutionResultStatus
from engagemate.worker.story_types import (
    StoryContent,
    StoryMedia,
    StoryMediaType,
    StoryOpportunity,
)


EXPECTED = {
    "instagram.discover",
    "instagram.view",
    "instagram.like",
    "instagram.reply",
    "instagram.complete_run",
}
USER_ID = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_ID = UUID("22222222-2222-4222-8222-222222222222")
TARGET_ID = UUID("33333333-3333-4333-8333-333333333333")
KEY = "T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-"


class _Discovery:
    def __init__(self, opportunity: StoryOpportunity) -> None:
        self.opportunity = opportunity

    async def find(self, context, targets, budget):
        return [self.opportunity]


class _Executor:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls = []
        self.events = events

    async def execute(self, opportunity, decision, context):
        if self.events is not None:
            self.events.append("execute")
        self.calls.append((opportunity, decision, context))
        return ExecutionResult(ExecutionResultStatus.SENT, uuid4())


class _ToolRepository:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.finished = []

    async def begin_mutation(self, **values):
        if not self.allowed:
            return {"allowed": False, "reason": "execution_disabled"}
        return {"allowed": True, "replay": False}

    async def mark_execution_started(self, **values):
        return True

    async def finish_mutation(self, **values):
        self.finished.append(values)

    async def complete_run(self, **values):
        return {"considered": 1, "confirmed": 1, "failed": 0, "denied": 0}


def _opportunity() -> StoryOpportunity:
    return StoryOpportunity(
        target_id=TARGET_ID,
        owner_id="owner-1",
        target_handle="approved.target",
        tier="vip",
        media_id="media-1",
        taken_at=datetime.now(UTC),
        fingerprint="a" * 64,
        content=StoryContent(
            StoryMediaType.IMAGE,
            "untrusted caption",
            None,
            StoryMedia("image/jpeg", b"small-image"),
            (),
        ),
    )


def _active_run(*, mode: str = "autonomous", repository=None) -> ActiveRun:
    opportunity = _opportunity()

    async def lease_is_live() -> bool:
        return True

    async def pace(action, selected) -> None:
        return None

    return ActiveRun(
        run_id=uuid4(),
        job=SimpleNamespace(
            id=uuid4(),
            user_id=USER_ID,
            ig_account_id=ACCOUNT_ID,
            claimed_by="worker-1",
        ),
        context=SimpleNamespace(
            user_id=USER_ID,
            ig_account_id=ACCOUNT_ID,
            targets=(),
            history=SimpleNamespace(
                recent_interaction_summary="No prior contact",
                sent_messages=(),
            ),
        ),
        config_version=1,
        config_snapshot={"version": 1},
        discovery_context=SimpleNamespace(),
        execution_context=SimpleNamespace(),
        budget=ActionBudget(1, 1, 1, plan_total=3, views=1),
        text_gate=TextGate(),
        lease_is_live=lease_is_live,
        discovery=_Discovery(opportunity),
        executor=_Executor(),
        pace=pace,
        opportunities={},
        tool_repository=repository or _ToolRepository(),
        execution_mode=mode,
    )


@pytest.mark.asyncio
async def test_execution_server_exposes_only_private_bounded_tools() -> None:
    server = build_execution_mcp(ActiveRunRegistry(), KEY)
    tools = await server.list_tools()
    assert {tool.name for tool in tools} == EXPECTED
    reply = next(tool for tool in tools if tool.name == "instagram.reply")
    assert "story_detail" in reply.input_schema["required"]
    assert "idempotency_key" in reply.input_schema["properties"]
    assert "idempotency_key" not in reply.input_schema["required"]

    forbidden = {"user_id", "ig_account_id", "cookies", "cookie", "proxy", "url", "sql"}
    for tool in tools:
        assert set(tool.input_schema.get("properties", {})).isdisjoint(forbidden)


@pytest.mark.asyncio
async def test_discovery_returns_an_opaque_bounded_opportunity() -> None:
    registry = ActiveRunRegistry()
    run = _active_run()
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        result = await server.call_tool("instagram.discover", {"limit": 10})

    payload = result.structured_content
    assert len(payload["opportunities"]) == 1
    item = payload["opportunities"][0]
    assert UUID(item["opportunity_id"])
    assert item["target"] == {"username": "approved.target", "tier": "vip"}
    assert item["allowed_actions"] == ["view", "like", "reply"]
    assert "owner_id" not in item
    assert "media_id" not in item
    assert "fingerprint" not in item


@pytest.mark.asyncio
async def test_view_lane_only_exposes_view_and_drains_unclaimed_story() -> None:
    registry = ActiveRunRegistry()
    repository = _ToolRepository()
    run = _active_run(repository=repository)
    run.view_only = True
    run.budget = ActionBudget(0, 0, 0, plan_total=0, views=1)
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        item = discovered.structured_content["opportunities"][0]
        assert item["allowed_actions"] == ["view"]
        denied = await server.call_tool(
            "instagram.like",
            {"opportunity_id": item["opportunity_id"], "idempotency_key": "like_view_lane_0001"},
        )
        assert denied.structured_content == {"status": "denied", "reason": "view_only_lane"}
        completed = await server.call_tool(
            "instagram.complete_run", {"reason": "source_exhausted"}
        )

    assert completed.structured_content["status"] == "completed"
    assert run.executor.calls[0][1].action == "view"
    assert run.budget.views == 0


@pytest.mark.asyncio
async def test_mutation_requires_policy_then_consumes_budget_once() -> None:
    registry = ActiveRunRegistry()
    repository = _ToolRepository()
    run = _active_run(repository=repository)
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0][
            "opportunity_id"
        ]
        result = await server.call_tool(
            "instagram.like",
            {"opportunity_id": opportunity_id, "idempotency_key": "like_fixture_0001"},
        )

    assert result.structured_content["status"] == "confirmed"
    assert run.budget.likes == 0
    assert len(run.executor.calls) == 1
    assert repository.finished[0]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_missing_idempotency_key_gets_stable_boundary_key() -> None:
    class RecordingRepository(_ToolRepository):
        def __init__(self):
            super().__init__()
            self.started = []

        async def begin_mutation(self, **values):
            self.started.append(values)
            return await super().begin_mutation(**values)

    registry = ActiveRunRegistry()
    repository = RecordingRepository()
    run = _active_run(repository=repository)
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0][
            "opportunity_id"
        ]
        like = await server.call_tool(
            "instagram.like", {"opportunity_id": opportunity_id}
        )
        reply = await server.call_tool(
            "instagram.reply",
            {
                "opportunity_id": opportunity_id,
                "generated_text": "The untrusted caption is a useful detail. What inspired it?",
                "story_detail": "untrusted caption",
            },
        )

    assert like.structured_content["status"] == "confirmed"
    assert reply.structured_content["status"] == "confirmed"
    assert [entry["idempotency_key"] for entry in repository.started] == [
        f"auto_like_{opportunity_id.replace('-', '')}",
        f"auto_reply_{opportunity_id.replace('-', '')}",
    ]


@pytest.mark.asyncio
async def test_mutation_paces_before_the_browser_action() -> None:
    events: list[str] = []
    registry = ActiveRunRegistry()
    run = _active_run()
    run.executor = _Executor(events)

    async def pace(action, selected) -> None:
        events.append("pace")

    run.pace = pace
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0][
            "opportunity_id"
        ]
        result = await server.call_tool(
            "instagram.like",
            {"opportunity_id": opportunity_id, "idempotency_key": "like_fixture_pacing"},
        )

    assert result.structured_content["status"] == "confirmed"
    assert events == ["pace", "execute"]


@pytest.mark.asyncio
async def test_lease_loss_after_pacing_blocks_the_browser_action() -> None:
    registry = ActiveRunRegistry()
    repository = _ToolRepository()
    run = _active_run(repository=repository)
    lease_checks = iter((True, False))
    paced = False

    async def lease_is_live() -> bool:
        return next(lease_checks)

    async def pace(action, selected) -> None:
        nonlocal paced
        paced = True

    run.lease_is_live = lease_is_live
    run.pace = pace
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0][
            "opportunity_id"
        ]
        result = await server.call_tool(
            "instagram.like",
            {"opportunity_id": opportunity_id, "idempotency_key": "like_fixture_lease"},
        )

    assert paced is True
    assert result.structured_content == {"status": "denied", "reason": "lease_lost"}
    assert run.executor.calls == []
    assert run.budget.likes == 1
    assert repository.finished[0]["status"] == "denied"
    assert repository.finished[0]["failure_class"] == "lease_lost"


@pytest.mark.asyncio
async def test_failed_execution_claim_restores_budget() -> None:
    class ClaimRefusingRepository(_ToolRepository):
        async def mark_execution_started(self, **values):
            return False

    registry = ActiveRunRegistry()
    repository = ClaimRefusingRepository()
    run = _active_run(repository=repository)
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0][
            "opportunity_id"
        ]
        result = await server.call_tool(
            "instagram.like",
            {"opportunity_id": opportunity_id, "idempotency_key": "claim_fixture_refuse"},
        )

    assert result.structured_content == {
        "status": "denied",
        "reason": "execution_claim_unavailable",
    }
    assert run.executor.calls == []
    assert run.budget.likes == 1


@pytest.mark.asyncio
async def test_deadline_after_execution_claim_keeps_budget_reserved() -> None:
    class ExpiredAfterClaim:
        def __init__(self):
            self.calls = 0

        def can_admit(self, action, seed):
            return True

        def can_execute(self):
            self.calls += 1
            return self.calls == 1

    registry = ActiveRunRegistry()
    run = _active_run()
    run.context.pacing_seed = "fixture"
    run.check_deadline = ExpiredAfterClaim()
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0][
            "opportunity_id"
        ]
        result = await server.call_tool(
            "instagram.like",
            {"opportunity_id": opportunity_id, "idempotency_key": "deadline_fixture_claim"},
        )

    assert result.structured_content == {
        "status": "denied",
        "reason": "production_check_time_limit",
    }
    assert run.budget.likes == 0


@pytest.mark.asyncio
async def test_text_gate_denial_is_audited_without_running_the_browser_action() -> None:
    registry = ActiveRunRegistry()
    repository = _ToolRepository()
    run = _active_run(repository=repository)
    run.text_gate = TextGate(banned_phrases=("buy now",))
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0][
            "opportunity_id"
        ]
        result = await server.call_tool(
            "instagram.reply",
            {
                "opportunity_id": opportunity_id,
                "idempotency_key": "reply_fixture_gate",
                "generated_text": "Buy now while supplies last",
                "story_detail": "untrusted caption",
            },
        )

    assert result.structured_content["status"] == "denied"
    assert result.structured_content["reason"].startswith("text_gate_")
    assert run.executor.calls == []
    assert repository.finished[0]["status"] == "denied"
    assert repository.finished[0]["failure_class"].startswith("text_gate_")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caption", "image", "reply", "detail", "expected"),
    [
        ("The blue studio setup is ready", None,
         "That blue studio setup looks fun. What are you recording?", "blue studio", "confirmed"),
        ("The blue studio setup is ready", None,
         "What's the context here?", "context here", "denied"),
        ("The blue studio setup is ready", None,
         "What's the context here with the studio?", "studio", "denied"),
        ("The blue studio setup is ready", None,
         "That sunset looks lovely. Where was it?", "sunset", "denied"),
        (None, StoryMedia("image/jpeg", b"image"),
         "That blue cut looks sharp. How long did it take?", "blue cut", "confirmed"),
        (None, None,
         "That blue cut looks sharp. How long did it take?", "blue cut", "denied"),
    ],
)
async def test_story_reply_requires_a_detail_visible_in_reply_and_story_evidence(
    caption, image, reply, detail, expected
) -> None:
    registry = ActiveRunRegistry()
    repository = _ToolRepository()
    run = _active_run(repository=repository)
    original = run.discovery.opportunity
    run.discovery.opportunity = StoryOpportunity(
        target_id=original.target_id,
        owner_id=original.owner_id,
        target_handle=original.target_handle,
        tier=original.tier,
        media_id=original.media_id,
        taken_at=original.taken_at,
        fingerprint=original.fingerprint,
        content=StoryContent(StoryMediaType.IMAGE, caption, None, image, ()),
    )
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0]["opportunity_id"]
        result = await server.call_tool(
            "instagram.reply",
            {
                "opportunity_id": opportunity_id,
                "idempotency_key": "reply_fixture_grounding",
                "generated_text": reply,
                "story_detail": detail,
            },
        )
    assert result.structured_content["status"] == expected
    assert len(run.executor.calls) == (1 if expected == "confirmed" else 0)
    if expected == "denied":
        assert result.structured_content["reason"] == "story_reply_ungrounded"
        assert repository.finished[0]["failure_class"] == "story_reply_ungrounded"


@pytest.mark.asyncio
async def test_disabled_mode_and_stale_opportunities_fail_closed() -> None:
    registry = ActiveRunRegistry()
    disabled = _active_run(mode="disabled", repository=_ToolRepository(allowed=False))
    server = build_execution_mcp(registry, KEY)

    with registry.activate(disabled):
        discovered = await server.call_tool("instagram.discover", {})
        stale_id = discovered.structured_content["opportunities"][0]["opportunity_id"]
        denied = await server.call_tool(
            "instagram.view",
            {"opportunity_id": stale_id, "idempotency_key": "view_fixture_0001"},
        )
    with registry.activate(_active_run()):
        stale = await server.call_tool(
            "instagram.view",
            {"opportunity_id": stale_id, "idempotency_key": "view_fixture_0002"},
        )

    assert denied.structured_content == {"status": "denied", "reason": "execution_disabled"}
    assert stale.structured_content == {"status": "denied", "reason": "unknown_opportunity"}
    assert disabled.executor.calls == []


@pytest.mark.asyncio
async def test_complete_run_marks_the_active_run() -> None:
    registry = ActiveRunRegistry()
    run = _active_run()
    server = build_execution_mcp(registry, KEY)

    with registry.activate(run):
        result = await server.call_tool(
            "instagram.complete_run",
            {"reason": "completed"},
        )

    assert run.completed is True
    assert result.structured_content["status"] == "completed"
    assert result.structured_content["counters"]["confirmed"] == 1


def test_completion_response_renames_internal_failed_counter() -> None:
    counters = {
        "considered": 2,
        "confirmed": 1,
        "failed": 0,
        "denied": 1,
        "gated": 0,
        "uncertain": 0,
    }

    response = _completion_response_counters(counters)

    assert response["action_errors"] == 0
    assert "failed" not in response
    assert '"failed"' not in json.dumps(response, sort_keys=True)
    assert counters["failed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "failure", "remaining"),
    [
        (ExecutionResultStatus.SKIPPED, None, 1),
        (ExecutionResultStatus.SKIPPED, "story_unavailable", 1),
        (ExecutionResultStatus.SKIPPED, "story_expired", 1),
        (ExecutionResultStatus.SKIPPED, "reply_follow_unconfirmed", 1),
        (ExecutionResultStatus.FAILED, "story_unavailable", 0),
        (ExecutionResultStatus.FAILED, "action_failed", 0),
        (ExecutionResultStatus.SENT, None, 0),
    ],
)
async def test_only_proven_no_write_skip_preserves_allocation(status, failure, remaining):
    registry = ActiveRunRegistry()
    run = _active_run()

    async def execute(*args):
        return ExecutionResult(status, uuid4(), failure)

    run.executor.execute = execute
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        opportunity_id = discovered.structured_content["opportunities"][0][
            "opportunity_id"
        ]
        await server.call_tool(
            "instagram.like",
            {"opportunity_id": opportunity_id, "idempotency_key": "allocation_fixture"},
        )
    assert run.budget.likes == remaining
    assert run.budget.plan_total == 2 + remaining


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_uncertain_action_persists_browser_evidence_without_retry(confirmed):
    registry = ActiveRunRegistry()
    run = _active_run()
    evidence = (
        "action=like,stage=receipt,request=forwarded,receipt=accepted,http=200,health=health_unavailable,cleanup=failed"
        if confirmed else
        "action=like,stage=receipt,request=forwarded,receipt=rejected,http=200,health=none"
    )
    calls = []

    async def execute(*args):
        calls.append(args)
        return ExecutionResult(ExecutionResultStatus.SENT if confirmed else ExecutionResultStatus.FAILED, uuid4(), None if confirmed else "action_failed", evidence)

    run.executor.execute = execute
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        result = await server.call_tool("instagram.like", {
            "opportunity_id": discovered.structured_content["opportunities"][0]["opportunity_id"],
            "idempotency_key": "evidence_fixture",
        })
    public_evidence = evidence.replace("failed", "error")
    expected = {"status": "confirmed", "evidence": public_evidence}
    if not confirmed:
        expected.update(status="uncertain", reason="action_error")
    assert result.structured_content == expected
    canonical = {"status": "confirmed", "evidence": evidence}
    if not confirmed:
        canonical.update(status="uncertain", reason="action_failed")
    assert run.tool_repository.finished[0]["safe_result"] == canonical
    assert "failed" not in json.dumps(result.structured_content)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_failed_mutation_is_gateway_safe_but_stays_canonical_in_the_ledger() -> None:
    registry = ActiveRunRegistry()
    run = _active_run()

    async def execute(*args):
        return ExecutionResult(
            ExecutionResultStatus.FAILED,
            uuid4(),
            "action_failed",
            "action=like,stage=receipt,cleanup=failed",
        )

    run.executor.execute = execute
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        discovered = await server.call_tool("instagram.discover", {})
        result = await server.call_tool("instagram.like", {
            "opportunity_id": discovered.structured_content["opportunities"][0]["opportunity_id"],
            "idempotency_key": "gateway_safe_fixture",
        })

    assert result.structured_content == {
        "status": "uncertain",
        "reason": "action_error",
        "evidence": "action=like,stage=receipt,cleanup=error",
    }
    assert run.tool_repository.finished[0]["safe_result"] == {
        "status": "uncertain",
        "reason": "action_failed",
        "evidence": "action=like,stage=receipt,cleanup=failed",
    }


@pytest.mark.asyncio
async def test_retired_dm_rejected_before_run_or_browser_access():
    from engagemate.worker.execution_mcp_tools import _mutate
    result = await _mutate(ActiveRunRegistry(), "unknown", "key", "dm", "hello")
    assert result == {"status": "denied", "reason": "standalone_dm_retired"}


@pytest.mark.asyncio
@pytest.mark.parametrize("carrier", [None, "malformed", ExecutionResult(ExecutionResultStatus.FAILED, uuid4())])
async def test_cancelled_unknown_write_never_becomes_confirmed(carrier):
    import asyncio
    from engagemate.worker.execution_mcp_tools import _mutate
    registry = ActiveRunRegistry()
    run = _active_run()
    opaque = uuid4()
    run.opportunities[opaque] = _opportunity()
    error = asyncio.CancelledError()
    error._engagemate_story_result = carrier
    async def execute(*args):
        raise error
    run.executor.execute = execute
    with registry.activate(run), pytest.raises(asyncio.CancelledError) as caught:
        await _mutate(registry, str(opaque), "cancelled_fixture", "like", None)
    assert caught.value is error
    assert len(run.tool_repository.finished) == 1
    assert run.tool_repository.finished[0]["status"] == "uncertain"
    assert run.tool_repository.finished[0]["failure_class"] == "execution_cancelled"
