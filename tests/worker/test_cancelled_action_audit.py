"""Own MCP tasks drain before unbinding; accepted receipts survive cancellation."""

import asyncio
from uuid import uuid4

import pytest

from engagemate.worker.active_run import ActiveRunRegistry
from engagemate.worker.execution_mcp_server import build_execution_mcp
from engagemate.worker.executor import ExecutionResult, ExecutionResultStatus
from tests.worker.test_execution_mcp_tools import KEY, _active_run, _opportunity


async def mutation(server, opaque):
    return await server.call_tool("instagram.like", {
        "opportunity_id":str(opaque),"idempotency_key":"cancel_audit_fixture_0001",
    })


@pytest.mark.parametrize("phase", ["pacing", "executor", "accepted"])
async def test_stop_drains_only_own_call_and_records_phase(phase):
    run,registry,entered = _active_run(),ActiveRunRegistry(),asyncio.Event()
    opaque = uuid4()
    run.opportunities[opaque] = _opportunity()
    server = build_execution_mcp(registry,KEY)
    unrelated = asyncio.create_task(asyncio.Event().wait())
    original_finish = run.tool_repository.finish_mutation

    async def finish(**values):
        await asyncio.sleep(0.01)
        await original_finish(**values)

    async def blocked(*args):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as error:
            if phase == "accepted":
                error._engagemate_story_result = ExecutionResult(
                    ExecutionResultStatus.SENT,uuid4(),action_evidence={"server_confirmed":True},
                )
            raise

    run.tool_repository.finish_mutation = finish
    if phase == "pacing":
        run.pace = blocked
    else:
        run.executor.execute = blocked
    try:
        with registry.activate(run):
            call = asyncio.create_task(mutation(server,opaque))
            await asyncio.wait_for(entered.wait(),1)
            assert call in run.tool_tasks
            drain = asyncio.create_task(run.stop_tools())
            await asyncio.sleep(0)
            drain.cancel()  # A repeated shutdown signal cannot abandon the ledger.
            await asyncio.gather(drain,return_exceptions=True)
            assert call.done() and not run.tool_tasks
            result = run.tool_repository.finished[0]
            assert result["status"] == {"pacing":"denied","executor":"uncertain","accepted":"confirmed"}[phase]
            if phase == "pacing":
                assert result["safe_result"]["phase"] == "reserved"
                assert run.executor.calls == [] and run.budget.likes == 1
            denied = await server.call_tool("instagram.discover",{})
            assert denied.structured_content["reason"] == "run_stopping"
            assert not unrelated.done()
    finally:
        unrelated.cancel()
        await asyncio.gather(unrelated,return_exceptions=True)


async def test_failed_phase_transition_cannot_enter_executor():
    run,registry = _active_run(),ActiveRunRegistry()
    opaque = uuid4()
    run.opportunities[opaque] = _opportunity()

    async def refuse(**values):
        assert values["worker_id"] == run.job.claimed_by
        return False

    run.tool_repository.mark_execution_started = refuse
    with registry.activate(run):
        result = await mutation(build_execution_mcp(registry,KEY),opaque)
    assert result.structured_content["reason"] == "execution_claim_unavailable"
    assert run.executor.calls == []
    assert run.budget.likes == 1


async def test_cancellation_after_sent_result_preserves_confirmed_tool_receipt():
    run,registry,entered,release = _active_run(),ActiveRunRegistry(),asyncio.Event(),asyncio.Event()
    opaque = uuid4()
    run.opportunities[opaque] = _opportunity()
    original = run.tool_repository.finish_mutation

    async def finish(**values):
        entered.set()
        await release.wait()
        await original(**values)

    run.tool_repository.finish_mutation = finish
    with registry.activate(run):
        call = asyncio.create_task(mutation(build_execution_mcp(registry,KEY),opaque))
        await entered.wait()
        call.cancel()
        release.set()
        result = await asyncio.gather(call,return_exceptions=True)
    assert isinstance(result[0],asyncio.CancelledError)
    assert run.tool_repository.finished[0]['status'] == 'confirmed'
