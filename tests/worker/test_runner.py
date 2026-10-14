"""One claimed job becomes one browser-bound Hermes execution run."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from engagemate.browser.context import DecryptedCookieJar
from engagemate.browser.fingerprint import Fingerprint
from engagemate.domain.budgets import ActionBudget
from engagemate.domain.models import ClaimedJob, JobStatus
from engagemate.intelligence.schemas import IntelligenceHistory, Persona
from engagemate.proxy.base import Geo, ProxyEndpoint
from engagemate.worker.active_run import ActiveRunRegistry, NoActiveRun
from engagemate.worker.hermes_run import HermesRunResult
from engagemate.worker.run_context import RunContext, RunContextError
from engagemate.worker.runner import RunSummary, WorkerRunner
from engagemate.worker.story_types import DiscoveryTarget


USER = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000002")
TARGET = UUID("00000000-0000-0000-0000-000000000003")
JOB = UUID("00000000-0000-0000-0000-000000000005")
NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
WORKER = "worker-1"
GEO = Geo("US", "California", "San Francisco")


def claimed_job() -> ClaimedJob:
    return ClaimedJob(
        id=JOB,
        user_id=USER,
        ig_account_id=ACCOUNT,
        schedule_key="2026-07-31T08:00",
        scheduled_for=NOW,
        status=JobStatus.RUNNING,
        claimed_by=WORKER,
        lease_expires_at=NOW + timedelta(seconds=120),
        heartbeat_at=NOW,
        attempts=1,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
    )


def run_context() -> RunContext:
    fingerprint = Fingerprint(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        viewport_width=1512,
        viewport_height=982,
        timezone="America/Los_Angeles",
        locale="en-US",
        color_scheme="light",
        canvas_seed="aa" * 16,
        webgl_seed="bb" * 16,
        webgl_vendor="Google Inc. (Apple)",
        webgl_renderer="ANGLE (Apple, Apple M1 Pro, OpenGL 4.1)",
    )
    return RunContext(
        user_id=USER,
        ig_account_id=ACCOUNT,
        username="account.one",
        geo=GEO,
        endpoint=ProxyEndpoint(
            provider="iproyal",
            provider_reference="lease-1",
            proxy_type="isp",
            geo=GEO,
            host="proxy.example.com",
            port=8080,
            username="user",
            password="secret",
        ),
        fingerprint=fingerprint,
        cookies=DecryptedCookieJar([{"name": "sessionid", "value": "x"}]),
        targets=(
            DiscoveryTarget(
                id=TARGET,
                user_id=USER,
                ig_account_id=ACCOUNT,
                owner_id="owner-target.one",
                handle="target.one",
                tier="vip",
                enabled=True,
            ),
        ),
        budget=ActionBudget(3, 2, 1, plan_total=8, views=20),
        persona=Persona("A friendly founder who replies briefly."),
        history=IntelligenceHistory("no recent contact", ()),
        banned_phrases=("buy now",),
        expected_language="en",
        pacing_seed="cc" * 16,
        config_version=7,
        config_snapshot={"version": 7, "profile": {"tone": "warm"}},
    )


class FakeJobs:
    def __init__(self, *, heartbeats: list[bool] | None = None) -> None:
        self.jobs = [claimed_job()]
        self.heartbeats = list(heartbeats or [True])
        self.finished = []
        self.deferred = []

    async def claim_next(self, worker_id):
        return self.jobs.pop(0) if self.jobs else None

    async def heartbeat(self, job_id, worker_id):
        return self.heartbeats.pop(0) if self.heartbeats else True

    async def finish(self, job_id, worker_id, status, error=None):
        self.finished.append((job_id, worker_id, status, error))
        return True

    async def defer(self, job_id, worker_id, error, *, delay_seconds=300):
        self.deferred.append((job_id, worker_id, error, delay_seconds))
        return True


class FakeLoader:
    def __init__(self, value=None) -> None:
        self.value = value if value is not None else run_context()

    async def load(self, job):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeBrowserFactory:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.opened = []
        self.closed = False
        self.session = SimpleNamespace(context=SimpleNamespace())

    @asynccontextmanager
    async def open(self, browser_context):
        self.opened.append(browser_context)
        if self.error is not None:
            raise self.error
        try:
            yield self.session
        finally:
            self.closed = True


class FakeHermes:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        wait_for_cancel: bool = False,
        counters: dict[str, int] | None = None,
    ) -> None:
        self.error = error
        self.wait_for_cancel = wait_for_cancel
        self.calls = []
        self.cancelled = False
        self.counters = counters or {
            "considered": 2,
            "confirmed": 1,
            "failed": 0,
            "denied": 1,
        }

    def new_session_id(self):
        return "em_0123456789abcdefghijklmnopqrstuv"

    async def run(self, active, *, session_id=None):
        self.calls.append((active, session_id))
        if self.wait_for_cancel:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self.error is not None:
            raise self.error
        active.completed = True
        active.completion_reason = "completed"
        active.completion_counters = dict(self.counters)
        return HermesRunResult(session_id, "Run completed.", 40, 12)


class FakeRunRepository:
    def __init__(self, *, starts: bool = True, retry_safe: bool = False) -> None:
        self.starts = starts
        self.retry_safe = retry_safe
        self.started = []
        self.failed = []

    async def reconcile_abandoned_runs(self):
        return 0

    async def start_run(self, **values):
        self.started.append(values)
        return self.starts

    async def fail_run(self, **values):
        self.failed.append(values)
        return True

    async def can_retry_without_external_write(self, run_id):
        return self.retry_safe


class FakeAccounts:
    def __init__(self) -> None:
        self.paused = []

    async def pause(self, user_id, ig_account_id, reason):
        self.paused.append((user_id, ig_account_id, reason))


class FakeSummaries:
    def __init__(self) -> None:
        self.persisted = []

    async def persist(self, summary, started_at, completed_at):
        self.persisted.append((summary, started_at, completed_at))


def build_runner(**overrides):
    jobs = overrides.get("jobs", FakeJobs())
    loader = overrides.get("loader", FakeLoader())
    browser = overrides.get("browser", FakeBrowserFactory())
    hermes = overrides.get("hermes", FakeHermes())
    repository = overrides.get("repository", FakeRunRepository())
    registry = overrides.get("registry", ActiveRunRegistry())
    accounts = overrides.get("accounts", FakeAccounts())
    summaries = overrides.get("summaries", FakeSummaries())
    runner = WorkerRunner(
        jobs=jobs,
        loader=loader,
        browser_factory=browser,
        discovery_factory=lambda session: "discovery",
        executor_factory=lambda session, job: "executor",
        hermes=hermes,
        registry=registry,
        tool_repository=repository,
        execution_mode="disabled",
        accounts=accounts,
        summaries=summaries,
        clock=lambda: NOW,
        heartbeat_interval_seconds=overrides.get("heartbeat_interval_seconds", 30),
    )
    return runner, SimpleNamespace(
        jobs=jobs,
        loader=loader,
        browser=browser,
        hermes=hermes,
        repository=repository,
        registry=registry,
        accounts=accounts,
        summaries=summaries,
    )


async def test_one_claimed_job_starts_one_fresh_hermes_run() -> None:
    runner, parts = build_runner()

    summary = await runner.run_once(WORKER)

    assert isinstance(summary, RunSummary)
    assert len(parts.hermes.calls) == 1
    active, session_id = parts.hermes.calls[0]
    assert active.job.id == JOB
    assert session_id == "em_0123456789abcdefghijklmnopqrstuv"
    assert parts.repository.started[0]["config_version"] == 7
    assert parts.repository.started[0]["config_snapshot"] == {
        "version": 7,
        "profile": {"tone": "warm"},
    }
    assert parts.browser.closed is True
    with pytest.raises(NoActiveRun):
        parts.registry.require()


async def test_completed_hermes_counters_and_usage_feed_the_run_summary() -> None:
    runner, parts = build_runner()

    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.COMPLETED
    assert summary.stop_reason == "completed"
    assert summary.considered == 2
    assert summary.engaged == 1
    assert summary.skipped == 1
    assert summary.failed == 0
    assert summary.input_tokens == 40
    assert summary.output_tokens == 12
    assert parts.summaries.persisted[0][0] == summary


async def test_context_failure_fails_without_starting_hermes() -> None:
    loader = FakeLoader(RunContextError("tenant_mismatch"))
    runner, parts = build_runner(loader=loader)

    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.FAILED
    assert summary.stop_reason == "tenant_mismatch"
    assert parts.hermes.calls == []
    assert parts.browser.opened == []


async def test_transient_proxy_context_failure_is_requeued_without_pausing() -> None:
    accounts = FakeAccounts()
    loader = FakeLoader(RunContextError("proxy_transient"))
    runner, parts = build_runner(loader=loader, accounts=accounts)

    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.FAILED
    assert summary.stop_reason == "proxy_transient"
    assert parts.jobs.deferred == [(JOB, WORKER, "proxy_transient", 300)]
    assert parts.jobs.finished == []
    assert accounts.paused == []


async def test_expired_session_pauses_the_account() -> None:
    accounts = FakeAccounts()
    loader = FakeLoader(RunContextError("session_expired", pause_account=True))
    runner, _ = build_runner(loader=loader, accounts=accounts)

    await runner.run_once(WORKER)

    assert accounts.paused == [(USER, ACCOUNT, "session_expired")]


async def test_audit_start_denial_stops_before_hermes() -> None:
    repository = FakeRunRepository(starts=False)
    runner, parts = build_runner(repository=repository)

    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.FAILED
    assert summary.stop_reason == "lease_lost"
    assert parts.hermes.calls == []
    assert parts.browser.closed is True


async def test_hermes_failure_closes_the_audit_run_and_browser() -> None:
    hermes = FakeHermes(error=RuntimeError("gateway unavailable"))
    runner, parts = build_runner(hermes=hermes)

    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.FAILED
    assert summary.stop_reason == "hermes_failed"
    assert parts.repository.failed[0]["reason"] == "hermes_failed"
    assert parts.browser.closed is True


async def test_pre_action_hermes_failure_is_deferred_once_when_no_write_exists() -> None:
    hermes = FakeHermes(error=RuntimeError("gateway unavailable"))
    repository = FakeRunRepository(retry_safe=True)
    runner, parts = build_runner(hermes=hermes, repository=repository)

    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.FAILED
    assert summary.stop_reason == "hermes_failed"
    assert parts.jobs.deferred == [(JOB, WORKER, "hermes_failed", 300)]
    assert parts.jobs.finished == []


async def test_hermes_failure_with_external_receipt_is_not_requeued() -> None:
    hermes = FakeHermes(error=RuntimeError("gateway unavailable"))
    repository = FakeRunRepository(retry_safe=False)
    runner, parts = build_runner(hermes=hermes, repository=repository)

    summary = await runner.run_once(WORKER)

    assert summary.stop_reason == "hermes_failed"
    assert parts.jobs.deferred == []
    assert parts.jobs.finished[-1][2] is JobStatus.FAILED


async def test_hermes_failure_logs_the_failure_kind_without_payloads(caplog) -> None:
    import httpx

    refused = httpx.ConnectError("[Errno 111] Connection refused")
    cleanup = RuntimeError("Hermes session cleanup failed")
    cleanup.__cause__ = refused
    runner, _ = build_runner(hermes=FakeHermes(error=cleanup))

    with caplog.at_level("WARNING", logger="engagemate.worker.runner"):
        summary = await runner.run_once(WORKER)

    assert summary.stop_reason == "hermes_failed"
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError(Hermes session cleanup failed) <- ConnectError" in message
    assert "Errno" not in message


async def test_lost_lease_cancels_hermes_and_closes_the_browser() -> None:
    jobs = FakeJobs(heartbeats=[True, False])
    hermes = FakeHermes(wait_for_cancel=True)
    runner, parts = build_runner(
        jobs=jobs,
        hermes=hermes,
        heartbeat_interval_seconds=0.001,
    )

    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.FAILED
    assert summary.stop_reason == "lease_lost"
    assert hermes.cancelled is True
    assert parts.repository.failed[0]["reason"] == "lease_lost"
    assert parts.browser.closed is True


async def test_browser_launch_failure_fails_without_creating_an_audit_run() -> None:
    browser = FakeBrowserFactory(error=RuntimeError("launch failed"))
    runner, parts = build_runner(browser=browser)

    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.FAILED
    assert summary.stop_reason == "browser_failed"
    assert parts.repository.started == []
    assert parts.hermes.calls == []


async def test_returns_none_when_no_job_is_claimable() -> None:
    jobs = FakeJobs()
    jobs.jobs = []
    runner, parts = build_runner(jobs=jobs)

    assert await runner.run_once(WORKER) is None
    assert parts.hermes.calls == []


async def test_external_cancellation_cancels_hermes_and_closes_browser() -> None:
    hermes = FakeHermes(wait_for_cancel=True)
    runner, parts = build_runner(hermes=hermes)
    job = asyncio.create_task(runner.run_once(WORKER))
    async with asyncio.timeout(1):
        while not hermes.calls:
            await asyncio.sleep(0)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert hermes.cancelled
    assert parts.browser.closed
    with pytest.raises(NoActiveRun):
        parts.registry.require()


async def test_browser_cleanup_failure_fails_job_and_preserves_action_counts():
    class CloseFailure(FakeBrowserFactory):
        @asynccontextmanager
        async def open(self, browser_context):
            async with super().open(browser_context) as session:
                yield session
            raise RuntimeError("browser context close failed")

    clean_runner, _ = build_runner()
    clean = await clean_runner.run_once(WORKER)
    runner, parts = build_runner(browser=CloseFailure())
    summary = await runner.run_once(WORKER)

    assert summary.status is JobStatus.FAILED
    assert summary.stop_reason == "browser_cleanup_failed"
    assert summary.engaged == clean.engaged
    assert summary.considered == clean.considered
    assert summary.input_tokens == clean.input_tokens
    assert parts.jobs.finished[-1][2] is JobStatus.FAILED
    assert parts.browser.closed


async def test_worker_retains_browser_until_real_hermes_client_drains_cleanup():
    import httpx
    from pydantic import SecretStr
    from engagemate.worker.hermes_run import HermesRunClient

    post_started = asyncio.Event()
    deleting = asyncio.Event()
    release_delete = asyncio.Event()
    deleted = False
    requests = []

    async def handler(request):
        nonlocal deleted
        requests.append(request)
        if request.method == "POST":
            post_started.set()
            await asyncio.Event().wait()
        deleting.set()
        await release_delete.wait()
        deleted = True
        return httpx.Response(200, json={"deleted": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        hermes = HermesRunClient(http=http,
                                endpoint="http://127.0.0.1:8657/v1/chat/completions",
                                api_key=SecretStr("h" * 48))
        runner, parts = build_runner(hermes=hermes)
        task = asyncio.create_task(runner.run_once(WORKER))
        try:
            async with asyncio.timeout(1):
                await post_started.wait()
                task.cancel()
                await deleting.wait()
                task.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not task.done()
                assert not parts.browser.closed
                browser_input = parts.browser.opened[0]
                assert browser_input.ig_account_id == ACCOUNT
                assert browser_input.endpoint.provider == "iproyal"
                assert browser_input.endpoint.provider_reference == "lease-1"
                payload = requests[0].content.decode()
                assert "sessionid" not in payload
                assert "proxy.example.com" not in payload
                release_delete.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert deleted and parts.browser.closed
                with pytest.raises(NoActiveRun):
                    parts.registry.require()
                assert parts.jobs.finished == []
                assert [request.method for request in requests] == ["POST", "DELETE"]
        finally:
            release_delete.set()
            await asyncio.gather(task, return_exceptions=True)
