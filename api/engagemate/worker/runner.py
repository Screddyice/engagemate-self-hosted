"""Run one claimed job as one browser-bound Hermes tool-calling session."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID, uuid4

from engagemate.browser.context import BrowserRunContext
from engagemate.domain.models import Action, ClaimedJob, JobStatus
from engagemate.domain.pacing import PacingPolicy
from engagemate.intelligence.text_gate import TextGate
from engagemate.worker.active_run import ActiveRun, ActiveRunRegistry
from engagemate.worker.check_deadline import CheckDeadline
from engagemate.worker.executor import ExecutionContext
from engagemate.worker.hermes_run import HermesRunResult
from engagemate.worker.run_context import RunContext, RunContextError
from engagemate.worker.story_types import DiscoveryContext, StoryOpportunity

_DEFAULT_HEARTBEAT_SECONDS = 30.0
_MAX_PROXY_TRANSIENT_ATTEMPTS = 3
_PROXY_TRANSIENT_RETRY_SECONDS = 300
_MAX_HERMES_NO_WRITE_ATTEMPTS = 2
_HERMES_NO_WRITE_RETRY_SECONDS = 300


@dataclass(frozen=True, slots=True)
class RunSummary:
    """A credential-free account of what one run did."""

    job_id: UUID
    user_id: UUID
    ig_account_id: UUID
    status: JobStatus
    considered: int
    engaged: int
    skipped: int
    gated: int
    failed: int
    stop_reason: str | None
    input_tokens: int = 0
    output_tokens: int = 0
    images_analyzed: int = 0
    video_frames_analyzed: int = 0
    retries: int = 0
    runtime_ms: int = 0
    job_persisted: bool = True


@dataclass(frozen=True, slots=True)
class _Outcome:
    considered: int = 0
    engaged: int = 0
    skipped: int = 0
    gated: int = 0
    failed: int = 0
    stop_reason: str | None = None
    failure: bool = False
    pause_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    requeue_seconds: int | None = None


class _LeaseLost(RuntimeError):
    pass


class JobsPort(Protocol):
    async def claim_next(self, worker_id: str) -> ClaimedJob | None: ...

    async def heartbeat(self, job_id: UUID, worker_id: str) -> bool: ...

    async def finish(
        self,
        job_id: UUID,
        worker_id: str,
        status: JobStatus,
        error: str | None = None,
    ) -> bool: ...

    async def defer(
        self,
        job_id: UUID,
        worker_id: str,
        error: str,
        *,
        delay_seconds: int = 300,
    ) -> bool: ...


class ContextLoaderPort(Protocol):
    async def load(self, job: ClaimedJob) -> RunContext: ...


class BrowserFactoryPort(Protocol):
    def open(self, run_context: BrowserRunContext) -> AbstractAsyncContextManager[Any]: ...


logger = logging.getLogger(__name__)


class HermesRunPort(Protocol):
    def new_session_id(self) -> str: ...

    async def run(
        self, active: ActiveRun, *, session_id: str | None = None
    ) -> HermesRunResult: ...


class RunRepositoryPort(Protocol):
    async def reconcile_abandoned_runs(self) -> int: ...

    async def start_run(self, **values: Any) -> bool: ...

    async def fail_run(self, **values: Any) -> bool: ...

    async def can_retry_without_external_write(self, run_id: UUID) -> bool: ...


class AccountsPort(Protocol):
    async def pause(
        self, user_id: UUID, ig_account_id: UUID, reason: str
    ) -> None: ...


class RunSummariesPort(Protocol):
    async def persist(
        self, summary: RunSummary, started_at: datetime, completed_at: datetime
    ) -> None: ...


class WorkerRunner:
    """Own one job lease, browser, active MCP binding, and Hermes session."""

    def __init__(
        self,
        *,
        jobs: JobsPort,
        loader: ContextLoaderPort,
        browser_factory: BrowserFactoryPort,
        discovery_factory: Callable[[Any], Any],
        executor_factory: Callable[[Any, ClaimedJob], Any],
        hermes: HermesRunPort,
        registry: ActiveRunRegistry,
        tool_repository: RunRepositoryPort,
        execution_mode: str,
        accounts: AccountsPort,
        summaries: RunSummariesPort | None = None,
        check_deadline: CheckDeadline | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        heartbeat_interval_seconds: float = _DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if execution_mode not in {"disabled", "canary", "autonomous"}:
            raise ValueError("execution_mode is invalid")
        self._jobs = jobs
        self._loader = loader
        self._browser_factory = browser_factory
        self._discovery_factory = discovery_factory
        self._executor_factory = executor_factory
        self._hermes = hermes
        self._registry = registry
        self._tool_repository = tool_repository
        self._execution_mode = execution_mode
        self._accounts = accounts
        self._summaries = summaries
        self._check_deadline = check_deadline
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or asyncio.sleep
        self._heartbeat_interval = heartbeat_interval_seconds

    def __repr__(self) -> str:
        return "WorkerRunner(jobs=<configured>, hermes=<configured>)"

    async def run_once(self, worker_id: str) -> RunSummary | None:
        while recovered := await self._tool_repository.reconcile_abandoned_runs():
            logger.warning("reconciled abandoned execution runs count=%d", recovered)
        job = await self._jobs.claim_next(worker_id)
        if job is None:
            return None
        started_at = self._now()
        try:
            context = await self._loader.load(job)
        except RunContextError as error:
            if error.reason == "proxy_transient" and job.attempts < _MAX_PROXY_TRANSIENT_ATTEMPTS:
                return await self._finish(
                    job,
                    JobStatus.FAILED,
                    error.reason,
                    started_at=started_at,
                    requeue_seconds=_PROXY_TRANSIENT_RETRY_SECONDS,
                )
            if error.pause_account:
                await self._pause(job, error.reason)
            elif error.reason == "proxy_transient":
                await self._pause(job, "proxy_unhealthy")
            return await self._finish(
                job, JobStatus.FAILED, error.reason, started_at=started_at
            )
        except Exception:  # noqa: BLE001 - loader ports have no common base
            return await self._finish(
                job, JobStatus.FAILED, "context_failed", started_at=started_at,
                requeue_seconds=self._pre_action_retry_seconds(job),
            )

        lease_lost = asyncio.Event()
        if not await self._renew(job, worker_id):
            return await self._finish(
                job, JobStatus.FAILED, "lease_lost", started_at=started_at
            )

        heartbeat = asyncio.create_task(self._heartbeat(job, worker_id, lease_lost))
        try:
            outcome = await self._run_in_browser(
                job, worker_id, context, lease_lost
            )
        finally:
            heartbeat.cancel()
            await _await_cancelled(heartbeat)

        if outcome.pause_reason is not None:
            await self._pause(job, outcome.pause_reason)
        return await self._finish(
            job,
            JobStatus.FAILED if outcome.failure else JobStatus.COMPLETED,
            outcome.stop_reason,
            started_at=started_at,
            considered=outcome.considered,
            engaged=outcome.engaged,
            skipped=outcome.skipped,
            gated=outcome.gated,
            failed=outcome.failed,
            input_tokens=outcome.input_tokens,
            output_tokens=outcome.output_tokens,
            requeue_seconds=outcome.requeue_seconds,
        )

    async def _run_in_browser(
        self,
        job: ClaimedJob,
        worker_id: str,
        context: RunContext,
        lease_lost: asyncio.Event,
    ) -> _Outcome:
        browser_context = BrowserRunContext(
            ig_account_id=context.ig_account_id,
            endpoint=context.endpoint,
            fingerprint=context.fingerprint,
            cookies=context.cookies,
        )
        entered = False
        outcome: _Outcome | None = None
        try:
            async with self._browser_factory.open(browser_context) as session:
                entered = True
                outcome = await self._run_active(
                    job, worker_id, context, session, lease_lost
                )
        except Exception:  # noqa: BLE001 - browser adapters share no base
            if not entered:
                return _failure(
                    "browser_failed",
                    requeue_seconds=self._pre_action_retry_seconds(job),
                )
            if outcome is None:
                return _failure("run_failed")
            return replace(outcome, failure=True, stop_reason="browser_cleanup_failed")
        return outcome if outcome is not None else _failure("browser_failed")

    async def _run_active(
        self,
        job: ClaimedJob,
        worker_id: str,
        context: RunContext,
        session: Any,
        lease_lost: asyncio.Event,
    ) -> _Outcome:
        run_id = uuid4()
        try:
            session_id = self._hermes.new_session_id()
        except Exception:  # noqa: BLE001 - adapter failure stays bounded
            return _failure(
                "hermes_failed",
                requeue_seconds=self._pre_action_retry_seconds(job),
            )
        now = self._now()
        browser = getattr(session, "context", session)

        async def lease_is_live() -> bool:
            if lease_lost.is_set() or active.stopping:
                return False
            alive = await self._renew(job, worker_id)
            if not alive:
                lease_lost.set()
            return alive

        async def pace(action: Action, opportunity: StoryOpportunity) -> None:
            delay = PacingPolicy.delay(
                action, f"{context.pacing_seed}:{opportunity.fingerprint}"
            )
            await self._sleep(delay.total_seconds())

        # Build discovery before the executor.  The production wiring binds
        # both objects to this session and uses that order to avoid consuming
        # a not-yet-created discovery binding.
        discovery = self._discovery_factory(session)
        executor = self._executor_factory(session, job)

        active = ActiveRun(
            run_id=run_id,
            job=job,
            context=context,
            config_version=context.config_version,
            config_snapshot=dict(context.config_snapshot),
            discovery_context=DiscoveryContext(
                context.user_id, context.ig_account_id, now
            ),
            execution_context=ExecutionContext(
                context.user_id, context.ig_account_id, now, browser
            ),
            budget=context.budget,
            text_gate=TextGate(
                banned_phrases=context.banned_phrases,
                expected_language=context.expected_language,
            ),
            lease_is_live=lease_is_live,
            discovery=discovery,
            executor=executor,
            pace=pace,
            tool_repository=self._tool_repository,
            execution_mode=self._execution_mode,
            view_only=context.view_only,
            hermes_session_id=session_id,
            check_deadline=self._check_deadline,
        )
        try:
            started = await self._tool_repository.start_run(
                run_id=run_id,
                job_id=job.id,
                user_id=context.user_id,
                ig_account_id=context.ig_account_id,
                worker_id=worker_id,
                hermes_session_id=session_id,
                config_version=context.config_version,
                config_snapshot=dict(context.config_snapshot),
                started_at=now,
            )
        except Exception:  # noqa: BLE001 - persistence fails closed
            return _failure("run_audit_failed")
        if not started:
            return _failure("lease_lost")

        try:
            with self._registry.activate(active):
                try:
                    result = await self._run_until_complete(
                        active, session_id, lease_lost
                    )
                finally:
                    await active.stop_tools()
        except _LeaseLost:
            await self._fail_run(run_id, "lease_lost")
            return _failure("lease_lost")
        except Exception as error:  # noqa: BLE001 - Hermes errors stay bounded
            # The job keeps the one contract code, but the log names the kind of
            # failure. On 2026-09-16 the only record was reason=hermes_failed,
            # which could not tell a refused connection from a rejected key.
            logger.warning(
                "hermes run failed job=%s %s", job.id, _hermes_failure_kind(error)
            )
            retry_seconds = None
            if job.attempts < _MAX_HERMES_NO_WRITE_ATTEMPTS:
                try:
                    if await self._tool_repository.can_retry_without_external_write(run_id):
                        retry_seconds = _HERMES_NO_WRITE_RETRY_SECONDS
                except Exception:  # noqa: BLE001 - uncertain audit state blocks replay
                    logger.exception("hermes retry safety check failed job=%s", job.id)
            await self._fail_run(run_id, "hermes_failed")
            return _failure("hermes_failed", requeue_seconds=retry_seconds)

        counters = active.completion_counters
        reason = active.completion_reason or "completed"
        failure = reason in {"safety_stop", "account_unhealthy"}
        return _Outcome(
            considered=_counter(counters, "considered"),
            engaged=_counter(counters, "confirmed"),
            skipped=_counter(counters, "denied"),
            gated=_counter(counters, "gated"),
            failed=_counter(counters, "failed") + _counter(counters, "uncertain"),
            stop_reason=reason,
            failure=failure,
            pause_reason="account_unhealthy" if reason == "account_unhealthy" else None,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    async def _run_until_complete(
        self,
        active: ActiveRun,
        session_id: str,
        lease_lost: asyncio.Event,
    ) -> HermesRunResult:
        hermes = asyncio.create_task(
            self._hermes.run(active, session_id=session_id)
        )
        lost = asyncio.create_task(lease_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {hermes, lost}, return_when=asyncio.FIRST_COMPLETED
            )
            if hermes in done:
                return await hermes
            raise _LeaseLost("job lease was lost")
        finally:
            for task in (hermes, lost):
                if not task.done():
                    task.cancel()
            await asyncio.gather(hermes, lost, return_exceptions=True)

    async def _fail_run(self, run_id: UUID, reason: str) -> None:
        try:
            persisted = await self._tool_repository.fail_run(
                run_id=run_id,
                reason=reason,
                completed_at=self._now(),
            )
            if persisted is not True:
                logger.error(
                    "execution run failure was not persisted run=%s reason=%s",
                    run_id, reason,
                )
        except Exception:  # noqa: BLE001 - preserve the primary failure
            logger.exception(
                "execution run failure persistence failed run=%s reason=%s",
                run_id, reason,
            )

    async def _heartbeat(
        self, job: ClaimedJob, worker_id: str, lease_lost: asyncio.Event
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval)
            if not await self._renew(job, worker_id):
                lease_lost.set()
                return

    async def _renew(self, job: ClaimedJob, worker_id: str) -> bool:
        try:
            return bool(await self._jobs.heartbeat(job.id, worker_id))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - an unverifiable lease is lost
            return False

    async def _pause(self, job: ClaimedJob, reason: str) -> None:
        try:
            await self._accounts.pause(job.user_id, job.ig_account_id, reason)
        except Exception:  # noqa: BLE001 - job outcome still needs persistence
            return

    async def _finish(
        self,
        job: ClaimedJob,
        status: JobStatus,
        stop_reason: str | None,
        *,
        started_at: datetime,
        considered: int = 0,
        engaged: int = 0,
        skipped: int = 0,
        gated: int = 0,
        failed: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        requeue_seconds: int | None = None,
    ) -> RunSummary:
        finished = False
        try:
            if requeue_seconds is None:
                finished = bool(
                    await self._jobs.finish(
                        job.id, job.claimed_by, status, stop_reason
                    )
                )
            else:
                finished = bool(
                    await self._jobs.defer(
                        job.id,
                        job.claimed_by,
                        stop_reason or "deferred",
                        delay_seconds=requeue_seconds,
                    )
                )
        except Exception:  # noqa: BLE001 - expose persistence failure without hiding the run
            logger.exception(
                "job state persistence failed job=%s status=%s requeue=%s",
                job.id, status.value, requeue_seconds,
            )
        if not finished:
            logger.error(
                "job state was not persisted job=%s status=%s requeue=%s",
                job.id, status.value, requeue_seconds,
            )
        completed_at = self._now()
        summary = RunSummary(
            job_id=job.id,
            user_id=job.user_id,
            ig_account_id=job.ig_account_id,
            status=status,
            considered=considered,
            engaged=engaged,
            skipped=skipped,
            gated=gated,
            failed=failed,
            stop_reason=stop_reason,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            retries=max(0, job.attempts - 1),
            runtime_ms=max(
                0, int((completed_at - started_at).total_seconds() * 1_000)
            ),
            job_persisted=finished,
        )
        if self._summaries is not None and finished:
            try:
                await self._summaries.persist(summary, started_at, completed_at)
            except Exception:  # noqa: BLE001 - telemetry cannot alter actions
                pass
        return summary

    @staticmethod
    def _pre_action_retry_seconds(job: ClaimedJob) -> int | None:
        """Retry one failure that happened before a run could write externally."""

        return (
            _HERMES_NO_WRITE_RETRY_SECONDS
            if job.attempts < _MAX_HERMES_NO_WRITE_ATTEMPTS else None
        )

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise RunContextError("invalid_clock")
        return now.astimezone(timezone.utc)


def _counter(values: dict[str, int], key: str) -> int:
    value = values.get(key, 0)
    return value if type(value) is int and value >= 0 else 0


def _hermes_failure_kind(error: BaseException) -> str:
    """Exception classes, and the client's own fixed messages. Never payloads."""

    parts = []
    current: BaseException | None = error
    while current is not None and len(parts) < 3:
        kind = type(current).__name__
        if type(current) is RuntimeError and str(current).startswith("Hermes "):
            kind = f"{kind}({current})"
        parts.append(kind)
        current = current.__cause__
    return "error=" + " <- ".join(parts)


def _failure(reason: str, *, requeue_seconds: int | None = None) -> _Outcome:
    return _Outcome(stop_reason=reason, failure=True, requeue_seconds=requeue_seconds)


async def _await_cancelled(task: asyncio.Task[Any]) -> None:
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        return
