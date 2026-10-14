"""Follower mode preserves policy gates across discovery and action pacing."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest
from engagemate.worker.active_run import ActiveRunRegistry
from engagemate.worker.execution_mcp_server import build_execution_mcp

from tests.worker.test_execution_mcp_tools import (
    KEY,
    _active_run,
    _opportunity,
    _ToolRepository,
)


class FollowerRepository(_ToolRepository):
    def __init__(self):
        super().__init__()
        self.reason = None
        self.reports = []
        self.begun = []

    async def discovery_stop_reason(self, run, *, action=None):
        return self.reason

    async def record_discovery(self, **values):
        self.reports.append(values)

    async def begin_mutation(self, **values):
        self.begun.append(values)
        # Force another task to run between budget computation and consumption.
        await asyncio.sleep(0)
        return await super().begin_mutation(**values)


class FollowerDiscoveryStub:
    def __init__(self):
        self.calls = 0
        self.prospect_reason = None
        self.rechecks = 0

    async def find_followers(self, run, limit):
        self.calls += 1
        return [replace(_opportunity(), fingerprint=str(self.calls) * 64)]

    def follower_report(self):
        return {
            "stop_reason": None,
            "counters": {"checked": self.calls},
            "can_continue": True,
        }

    async def recheck_prospect(self, run, opportunity):
        self.rechecks += 1
        return self.prospect_reason


def follower_run():
    repository = FollowerRepository()
    run = _active_run(repository=repository)
    run.config_snapshot = {
        "follower_sources": [{"id": str(uuid4()), "username": "source"}]
    }
    run.discovery = FollowerDiscoveryStub()
    return run


async def test_repeated_follower_discovery_persists_tenant_scoped_progress():
    registry = ActiveRunRegistry()
    run = follower_run()
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        first = (
            await server.call_tool("instagram.discover", {"limit": 1})
        ).structured_content
        second = (
            await server.call_tool("instagram.discover", {"limit": 1})
        ).structured_content
    assert (
        first["opportunities"][0]["opportunity_id"]
        != second["opportunities"][0]["opportunity_id"]
    )
    assert second["discovery"]["counters"] == {"checked": 2}
    assert run.tool_repository.reports[-1] == {
        "run_id": run.run_id,
        "user_id": run.context.user_id,
        "ig_account_id": run.context.ig_account_id,
        "report": second["discovery"],
    }


@pytest.mark.parametrize("revoke", ["approval", "relationship"])
async def test_follower_action_rechecks_after_pacing(revoke):
    registry = ActiveRunRegistry()
    run = follower_run()

    async def pace(*_):
        if revoke == "approval":
            run.tool_repository.reason = "source_changed"
        else:
            run.discovery.prospect_reason = "excluded"

    run.pace = pace
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        discovered = (
            await server.call_tool("instagram.discover", {})
        ).structured_content
        result = (
            await server.call_tool(
                "instagram.like",
                {
                    "opportunity_id": discovered["opportunities"][0]["opportunity_id"],
                    "idempotency_key": "follower_pacing_revoke",
                },
            )
        ).structured_content
    assert result == {
        "status": "denied",
        "reason": "source_changed" if revoke == "approval" else "excluded",
    }
    assert run.executor.calls == []
    assert run.tool_repository.finished[-1]["safe_result"] == result


async def test_lost_lease_blocks_follower_profile_recheck():
    registry = ActiveRunRegistry()
    run = follower_run()

    async def lost():
        return False

    run.lease_is_live = lost
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        discovered = (
            await server.call_tool("instagram.discover", {})
        ).structured_content
        result = (
            await server.call_tool(
                "instagram.like",
                {
                    "opportunity_id": discovered["opportunities"][0]["opportunity_id"],
                    "idempotency_key": "follower_lost_lease",
                },
            )
        ).structured_content
    assert result == {"status": "denied", "reason": "lease_lost"}
    assert run.discovery.rechecks == 0
    assert run.executor.calls == []


async def test_concurrent_mutations_cannot_overspend_the_shared_budget():
    registry = ActiveRunRegistry()
    run = follower_run()
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        discovered = (
            await server.call_tool("instagram.discover", {})
        ).structured_content
        opportunity_id = discovered["opportunities"][0]["opportunity_id"]
        results = await asyncio.gather(
            *[
                server.call_tool(
                    "instagram.like",
                    {
                        "opportunity_id": opportunity_id,
                        "idempotency_key": f"concurrent_follower_{i}",
                    },
                )
                for i in range(2)
            ]
        )
    assert sorted(result.structured_content["status"] for result in results) == [
        "confirmed",
        "denied",
    ]
    assert len(run.executor.calls) == 1
    assert run.budget.likes == 0
    assert len(run.tool_repository.begun) == 1


async def test_profile_navigation_cannot_outlive_the_job_lease():
    registry = ActiveRunRegistry()
    run = follower_run()
    lease_live = True

    async def lease():
        return lease_live

    async def recheck(*_):
        nonlocal lease_live
        lease_live = False

    run.lease_is_live = lease
    run.discovery.recheck_prospect = recheck
    server = build_execution_mcp(registry, KEY)
    with registry.activate(run):
        discovered = (
            await server.call_tool("instagram.discover", {})
        ).structured_content
        result = (
            await server.call_tool(
                "instagram.like",
                {
                    "opportunity_id": discovered["opportunities"][0]["opportunity_id"],
                    "idempotency_key": "follower_navigation_lease",
                },
            )
        ).structured_content
    assert result == {"status": "denied", "reason": "lease_lost"}
    assert run.executor.calls == []
    assert run.tool_repository.begun == []


@pytest.mark.parametrize("reason", ["authentication_required", "checkpoint_required", "rate_limited"])
@pytest.mark.parametrize("when", ["before_claim", "after_pacing"])
async def test_profile_restriction_records_pause_without_executing(reason, when, caplog):
    run = follower_run()
    registry = ActiveRunRegistry()
    server = build_execution_mcp(registry, KEY)
    budget = run.budget
    ready = when == "before_claim"

    async def recheck(*_):
        if ready:
            error = RuntimeError("private browser detail")
            error.reason = reason
            raise error

    async def pace(*_):
        nonlocal ready
        ready = True

    run.discovery.recheck_prospect = recheck
    run.pace = pace
    opaque_id = uuid4()
    run.opportunities[opaque_id] = _opportunity()
    with registry.activate(run):
        result = (await server.call_tool("instagram.like", {
            "opportunity_id": str(opaque_id), "idempotency_key": "restriction_before_send",
        })).structured_content
    assert result == {"status": "denied", "reason": reason}
    assert run.executor.calls == []
    assert "follower policy unavailable" not in caplog.text
    assert run.tool_repository.reports == [{
        "run_id": run.run_id, "user_id": run.context.user_id,
        "ig_account_id": run.context.ig_account_id,
        "report": {"counters": {"checked": 0}, "stop_reason": reason, "can_continue": False},
    }]
    if when == "before_claim":
        assert run.tool_repository.begun == []
        assert run.tool_repository.finished == []
        assert run.budget == budget
    else:
        assert len(run.tool_repository.begun) == 1
        assert run.tool_repository.finished[-1]["safe_result"] == result


@pytest.mark.parametrize("failure", ["unknown_browser_error", "pause_write_failed", "profile_unreadable"])
@pytest.mark.parametrize("when", ["before_claim", "after_pacing"])
async def test_policy_failure_logs_safe_stage(failure, when, caplog):
    run = follower_run()
    registry = ActiveRunRegistry()
    server = build_execution_mcp(registry, KEY)

    ready = when == "before_claim"

    async def recheck(*_):
        if not ready:
            return None
        error = RuntimeError("private browser detail")
        error.reason = {"pause_write_failed": "rate_limited", "profile_unreadable": "profile_unreadable"}.get(failure, "private browser detail")
        raise error

    async def pace(*_):
        nonlocal ready
        ready = True

    run.pace = pace

    async def record(**_):
        raise RuntimeError("private database detail")

    run.discovery.recheck_prospect = recheck
    if failure == "pause_write_failed":
        run.tool_repository.record_discovery = record
    opaque_id = uuid4()
    run.opportunities[opaque_id] = _opportunity()
    with registry.activate(run):
        result = (await server.call_tool("instagram.like", {
            "opportunity_id": str(opaque_id), "idempotency_key": "restriction_failure_case",
        })).structured_content
    assert result == {"status": "denied", "reason": "policy_unavailable"}
    assert run.executor.calls == []
    if when == "before_claim":
        assert run.tool_repository.begun == []
    else:
        assert run.tool_repository.finished[-1]["safe_result"] == result
    record = next(r for r in caplog.records if hasattr(r, "policy_stage"))
    assert record.policy_stage == ("restriction_record" if failure == "pause_write_failed" else "prospect_recheck")
    assert record.exception_class == "RuntimeError"
    assert record.canonical_reason == ("profile_unreadable" if failure == "profile_unreadable" else "unknown")
    assert record.exc_info is None
    assert "private" not in caplog.text
