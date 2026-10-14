"""One-job canary isolation, budget reduction and server lifetime contracts."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest
from engagemate.domain.budgets import ActionBudget
from engagemate.domain.models import JobStatus
from engagemate.worker.canary import (
    CanaryContextLoader,
    CanaryError,
    CanaryScope,
    ExactJobRepository,
    _bind_mcp_socket,
    _serve_one,
)
from engagemate.worker.run_context import RunContextError

from tests.worker import test_canary_scripts as canary_fixtures
from tests.worker.test_runner import FakeLoader, claimed_job, run_context

database_url = canary_fixtures.database_url
world = canary_fixtures.world


@pytest.fixture
def reserved(world):
    scope = CanaryScope(
        world["user_id"], world["account_id"], world["job_id"], "canary-test"
    )
    with world["pool"].connection() as connection:
        connection.execute(
            "UPDATE jobs SET claimed_by=%s,lease_expires_at=now()+interval '2 minutes',heartbeat_at=now(),attempts=1 WHERE id=%s",
            (scope.worker_id, scope.job_id),
        )
    return world, scope, ExactJobRepository(world["pool"], scope, lease_seconds=120)


async def test_exact_job_yields_once_without_claiming_queued_jobs(reserved):
    world, scope, repository = reserved
    other = uuid4()
    with world["pool"].connection() as connection:
        connection.execute(
            "INSERT INTO jobs (id,user_id,ig_account_id,schedule_key,scheduled_for,status) VALUES (%s,%s,%s,%s,now()-interval '1 hour','queued')",
            (other, scope.user_id, scope.account_id, str(other)),
        )
    job = await repository.claim_next(scope.worker_id)
    assert job.id == scope.job_id and job.attempts == 1
    assert await repository.claim_next(scope.worker_id) is None
    assert await repository.heartbeat(scope.job_id, scope.worker_id)
    assert not await repository.heartbeat(other, scope.worker_id)
    assert not await repository.finish(scope.job_id, "other-worker", JobStatus.FAILED)
    assert await repository.finish(scope.job_id, scope.worker_id, JobStatus.COMPLETED)
    with world["pool"].connection() as connection:
        row = connection.execute(
            "SELECT status,claimed_by,attempts FROM jobs WHERE id=%s", (other,)
        ).fetchone()
        assert (
            row["status"] in ("queued", b"queued")
            and row["claimed_by"] is None
            and row["attempts"] == 0
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE jobs SET status='queued' WHERE id=%s",
        "UPDATE jobs SET claimed_by='other-worker' WHERE id=%s",
        "UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
        "UPDATE jobs SET status='completed' WHERE id=%s",
    ],
)
async def test_exact_job_refuses_unowned_expired_or_nonrunning_reservation(
    reserved, mutation
):
    world, scope, repository = reserved
    with world["pool"].connection() as connection:
        connection.execute(mutation, (scope.job_id,))
    with pytest.raises(CanaryError, match="reserved_job_unavailable"):
        await repository.claim_next(scope.worker_id)
    assert repository.yielded is False


async def test_exact_job_refuses_cross_tenant_scope(reserved):
    world, scope, _ = reserved
    wrong = replace(scope, user_id=world["other_user_id"])
    repository = ExactJobRepository(world["pool"], wrong, lease_seconds=120)
    with pytest.raises(CanaryError, match="reserved_job_unavailable"):
        await repository.claim_next(scope.worker_id)


@pytest.mark.parametrize(
    "original",
    [
        ActionBudget(0, 0, 0, 0, views=0),
        ActionBudget(5, 4, 3, 12, views=100),
        ActionBudget(5, 4, 3, 0, views=1),
        ActionBudget(0, 2, 2, 4, views=0),
    ],
)
async def test_canary_clamp_never_increases_saved_budget_or_changes_original_context(
    original,
):
    job = claimed_job()
    source = replace(run_context(), budget=original)
    scope = CanaryScope(job.user_id, job.ig_account_id, job.id, job.claimed_by)
    loaded = await CanaryContextLoader(FakeLoader(source), scope).load(job)
    assert loaded.budget.likes <= min(original.likes, original.plan_total, 1)
    assert loaded.budget.engagements <= min(original.engagements, 1)
    assert loaded.budget.views <= min(original.views, 1)
    assert (
        loaded.budget.replies
        == loaded.budget.dms
        == loaded.budget.private_messages
        == 0
    )
    assert loaded.cookies is source.cookies and loaded.targets is source.targets
    assert loaded.config_snapshot["canary"]["original_budget"] == asdict(original)
    assert loaded.config_snapshot["canary"]["effective_budget"] == asdict(loaded.budget)
    assert "canary" not in source.config_snapshot and source.budget is original


async def test_canary_loader_preserves_real_loader_failure_and_tenant_gate():
    job = claimed_job()
    scope = CanaryScope(job.user_id, job.ig_account_id, job.id, job.claimed_by)
    with pytest.raises(RunContextError, match="agent_disabled"):
        await CanaryContextLoader(
            FakeLoader(RunContextError("agent_disabled")), scope
        ).load(job)
    with pytest.raises(RunContextError, match="tenant_mismatch"):
        await CanaryContextLoader(FakeLoader(), scope).load(
            replace(job, user_id=uuid4())
        )


@pytest.mark.parametrize("reply_limit,private_limit,plan_limit,expected", [
    (4, 3, 10, 1), (0, 3, 10, 0), (4, 0, 10, 0), (4, 3, 0, 0),
])
async def test_reply_only_canary_preserves_all_saved_message_ceilings(
    reply_limit, private_limit, plan_limit, expected,
):
    job = claimed_job()
    original = ActionBudget(8, reply_limit, 4, plan_limit, views=100,
                            private_messages=private_limit)
    source = replace(run_context(), budget=original)
    scope = CanaryScope(job.user_id, job.ig_account_id, job.id, job.claimed_by,
                        reply_only=True)
    loaded = await CanaryContextLoader(FakeLoader(source), scope).load(job)
    assert loaded.budget.replies == loaded.budget.private_messages == expected
    assert loaded.budget.likes == loaded.budget.views == loaded.budget.dms == 0
    assert loaded.budget.engagements == expected
    assert loaded.config_snapshot["canary"]["reply_only"] is True
    assert source.budget is original and "canary" not in source.config_snapshot


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_reply_only_mode_requires_explicit_boolean(value):
    job = claimed_job()
    with pytest.raises(ValueError, match="must be boolean"):
        CanaryScope(job.user_id, job.ig_account_id, job.id, job.claimed_by,
                    reply_only=value)


def test_private_endpoint_collision_refuses_before_any_job_can_start():
    with (
        _bind_mcp_socket("127.0.0.1", 0) as bound,
        pytest.raises(CanaryError, match="canary_mcp_port_unavailable"),
    ):
        _bind_mcp_socket("127.0.0.1", bound.getsockname()[1])


class Server:
    def __init__(self, events, fail=False):
        self.events, self.fail = events, fail
        self.started = False
        self.should_exit = False

    async def serve(self, *, sockets):
        if self.fail:
            return
        self.events.append("mcp_ready")
        self.started = True
        try:
            while not self.should_exit:
                await asyncio.sleep(0.001)
        finally:
            self.events.append("mcp_closed")


async def test_one_job_runs_after_mcp_ready_and_server_closes_after_summary():
    events = []
    server = Server(events)
    summary = object()

    async def run_once(worker):
        events.append("job")
        return summary

    def abort(reason):
        raise AssertionError(reason)

    result = await _serve_one(
        server,
        object(),
        NS(run_once=run_once),
        NS(worker_id="canary"),
        timeout_seconds=1,
        abort=abort,
    )
    assert result is summary
    assert events == ["mcp_ready", "job", "mcp_closed"]


async def test_startup_failure_does_not_invoke_runner():
    events = []

    async def run_once(worker):
        events.append("job")

    def abort(reason):
        async def record():
            events.append(reason)

        return record()

    with pytest.raises(CanaryError, match="canary_mcp_start_failed"):
        await _serve_one(
            Server(events, fail=True),
            object(),
            NS(run_once=run_once),
            NS(worker_id="canary"),
            timeout_seconds=1,
            abort=abort,
        )
    assert "job" not in events


async def test_timeout_captures_audit_identity_then_cancels_runner_before_terminalizing():
    events = []

    async def run_once(worker):
        events.append("job")
        try:
            await asyncio.Future()
        finally:
            events.append("job_cancelled")

    def abort(reason):
        events.append("capture_identity")

        async def record():
            events.append(reason)

        return record()

    with pytest.raises(CanaryError, match="canary_timeout"):
        await _serve_one(
            Server(events),
            object(),
            NS(run_once=run_once),
            NS(worker_id="canary"),
            timeout_seconds=0.01,
            abort=abort,
        )
    assert events == [
        "mcp_ready",
        "job",
        "capture_identity",
        "job_cancelled",
        "canary_timeout",
        "mcp_closed",
    ]


async def test_real_runner_timeout_terminalizes_exact_job_and_cleans_real_hermes_client(
    reserved, monkeypatch
):
    from contextlib import asynccontextmanager

    import httpx
    import playwright.async_api
    import uvicorn
    from engagemate import config
    from engagemate.db import pool as pool_module
    from engagemate.worker import wiring
    from engagemate.worker.canary import run_canary
    from engagemate.worker.hermes_run import HermesRunClient
    from engagemate.worker.runner import WorkerRunner
    from pydantic import SecretStr

    from deploy import healthcheck
    from tests.worker.test_runner import FakeAccounts, FakeBrowserFactory, FakeSummaries

    world, scope, _ = reserved
    pool = world["pool"]
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM hermes_execution_runs WHERE job_id=%s", (scope.job_id,)
        )
    events = []
    settings = NS(
        execution_mcp_host="127.0.0.1",
        execution_mcp_port=0,
        execution_mcp_key=SecretStr("T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-"),
        database_url="test-pool",
        job_lease_seconds=120,
        hermes_agent_timeout_seconds=60,
        hermes_execution_timeout_seconds=1800,
        hermes_agent_url="http://127.0.0.1:8642/v1/chat/completions",
        hermes_agent_api_key=SecretStr("T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-"),
        hermes_agent_model="engagemate-agent",
        instagram_execution_mode="disabled",
    )
    monkeypatch.setattr(config, "Settings", lambda: settings)
    monkeypatch.setattr(healthcheck, "validate_worker_environment", lambda _: None)
    monkeypatch.setattr(pool_module, "create_pool", lambda *_, **__: pool)

    @asynccontextmanager
    async def playwright_context():
        yield object()

    monkeypatch.setattr(playwright.async_api, "async_playwright", playwright_context)

    class HTTP:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def post(self, *_, **__):
            events.append("hermes_post")
            await asyncio.Future()

        async def delete(self, *_, **__):
            events.append("hermes_session_deleted")
            return httpx.Response(200, json={"deleted": True})

    def http_client(**kwargs):
        assert kwargs["timeout"].read == 1
        assert kwargs["timeout"].connect == 15
        return HTTP()

    monkeypatch.setattr(httpx, "AsyncClient", http_client)
    monkeypatch.setattr(uvicorn, "Server", lambda _: Server(events))

    def build(settings_arg, pool_arg, playwright_arg, **kwargs):
        assert settings_arg.instagram_execution_mode == "disabled"
        assert kwargs["execution_mode"] == "canary"
        assert isinstance(kwargs["hermes"], HermesRunClient)
        context = replace(
            run_context(), user_id=scope.user_id, ig_account_id=scope.account_id
        )
        return WorkerRunner(
            jobs=kwargs["jobs"],
            loader=kwargs["context_loader_factory"](FakeLoader(context)),
            browser_factory=FakeBrowserFactory(),
            discovery_factory=lambda _: object(),
            executor_factory=lambda *_: object(),
            hermes=kwargs["hermes"],
            registry=kwargs["registry"],
            tool_repository=kwargs["tool_repository"],
            execution_mode=kwargs["execution_mode"],
            accounts=FakeAccounts(),
            summaries=FakeSummaries(),
        )

    monkeypatch.setattr(wiring, "build_runner", build)
    # Keep the fixture pool open for durable assertions after the entrypoint exits.
    real_close = pool.close
    monkeypatch.setattr(pool, "close", lambda: None)
    try:
        with pytest.raises(CanaryError, match="canary_timeout"):
            await run_canary(scope, timeout_seconds=1)
        assert "hermes_post" in events and "hermes_session_deleted" in events
        with pool.connection() as connection:
            job = connection.execute(
                "SELECT status,last_error FROM jobs WHERE id=%s", (scope.job_id,)
            ).fetchone()
            audit = connection.execute(
                "SELECT status,stop_reason,config_snapshot FROM hermes_execution_runs WHERE job_id=%s",
                (scope.job_id,),
            ).fetchone()
        assert job["status"] in ("failed", b"failed") and job["last_error"] in (
            "canary_timeout",
            b"canary_timeout",
        )
        assert audit["status"] in ("failed", b"failed") and audit["stop_reason"] in (
            "canary_timeout",
            b"canary_timeout",
        )
        assert audit["config_snapshot"]["canary"]["effective_budget"]["likes"] == 1
    finally:
        monkeypatch.setattr(pool, "close", real_close)
