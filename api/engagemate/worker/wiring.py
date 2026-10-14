"""Join the sync repositories and the live browser to the async runner.

Everything here is adapter, not policy. The repositories are psycopg and
therefore blocking, so each call is offloaded off the event loop; the browser
pieces exist because `DiscoveryService` and `Executor` are constructed per
browser session rather than per process.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from psycopg_pool import ConnectionPool

from engagemate.browser.context import BrowserContextFactory
from engagemate.browser.fingerprint import FingerprintFactory
from engagemate.browser.lease import LeasedBrowserFactory
from engagemate.config import Settings
from engagemate.db.accounts import AccountRepository
from engagemate.db.executions import ExecutionRepository
from engagemate.db.jobs import JobRepository
from engagemate.db.proxies import ProxyBindingService, ProxyRepository
from engagemate.domain.models import ClaimedJob, JobStatus
from engagemate.proxy.iproyal import IPRoyalProvider
from engagemate.security.session_cipher import SessionCipher
from engagemate.worker.active_run import ActiveRunRegistry
from engagemate.worker.artifact_sink import FilesystemArtifactSink
from engagemate.worker.circuit_breaker import HealthSignal
from engagemate.worker.discovery import DiscoveryService
from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository
from engagemate.worker.executor import ExecutionResult, ExecutionResultStatus, Executor
from engagemate.worker.follow_maintenance import FollowMaintenance
from engagemate.worker.follow_runtime import StoryFollowPerformer
from engagemate.worker.follow_store import PostgresFollowStore
from engagemate.worker.follower_browser import PlaywrightFollowerBrowser
from engagemate.worker.follower_discovery import FollowerDiscovery, prospect_reason
from engagemate.worker.follower_store import PostgresFollowerStore
from engagemate.worker.hermes_run import HermesRunClient
from engagemate.worker.instagram_adapter import ArtifactSink
from engagemate.worker.run_context import RunContextLoader
from engagemate.worker.run_context_reader import PostgresRunContextReader
from engagemate.worker.runner import (
    ContextLoaderPort,
    JobsPort,
    RunSummary,
    WorkerRunner,
)
from engagemate.worker.story_actions import LiveStoryAdapter
from engagemate.worker.story_history import PostgresStoryHistoryRepository
from engagemate.worker.story_preview import ReadOnlyStoryBrowser

_MEDIA_TIMEOUT_MS = 15_000


class AsyncJobRepository:
    """Run the blocking job repository off the event loop."""

    def __init__(self, repository: JobRepository) -> None:
        self._repository = repository

    def __repr__(self) -> str:
        return "AsyncJobRepository(repository=<configured>)"

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        return await asyncio.to_thread(self._repository.claim_next, worker_id)

    async def heartbeat(self, job_id: UUID, worker_id: str) -> bool:
        return await asyncio.to_thread(self._repository.heartbeat, job_id, worker_id)

    async def finish(
        self,
        job_id: UUID,
        worker_id: str,
        status: JobStatus,
        error: str | None = None,
    ) -> bool:
        return await asyncio.to_thread(
            self._repository.finish, job_id, worker_id, status, error
        )

    async def defer(
        self, job_id: UUID, worker_id: str, error: str, *, delay_seconds: int = 300
    ) -> bool:
        return await asyncio.to_thread(
            self._repository.defer,
            job_id,
            worker_id,
            error,
            delay_seconds=delay_seconds,
        )


class AsyncAccountPauser:
    """Pause one account and cancel its queued jobs, off the event loop."""

    def __init__(self, repository: AccountRepository) -> None:
        self._repository = repository

    def __repr__(self) -> str:
        return "AsyncAccountPauser(repository=<configured>)"

    async def pause(self, user_id: UUID, ig_account_id: UUID, reason: str) -> None:
        await asyncio.to_thread(self._pause_sync, user_id, ig_account_id, reason)

    def _pause_sync(self, user_id: UUID, ig_account_id: UUID, reason: str) -> None:
        self._repository.set_paused(user_id, ig_account_id, paused=True, reason=reason)


class AsyncRunSummaryRepository:
    """Persist one bounded COGS summary after the job finish wins."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def __repr__(self) -> str:
        return "AsyncRunSummaryRepository(pool=<configured>)"

    async def persist(
        self, summary: RunSummary, started_at: datetime, completed_at: datetime
    ) -> None:
        await asyncio.to_thread(self._persist_sync, summary, started_at, completed_at)

    def _persist_sync(
        self, summary: RunSummary, started_at: datetime, completed_at: datetime
    ) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                WITH scoped_job AS (
                    SELECT id, user_id, ig_account_id FROM jobs
                    WHERE id = %(job_id)s AND user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND status IN ('completed', 'failed', 'cancelled')
                ), scoped_calls AS (
                    SELECT calls.*, runs.started_at AS run_started_at
                    FROM scoped_job job
                    JOIN hermes_execution_runs runs
                      ON runs.job_id = job.id AND runs.user_id = job.user_id
                     AND runs.ig_account_id = job.ig_account_id
                    JOIN hermes_tool_calls calls ON calls.run_id = runs.id
                ), sent_engagements AS (
                    SELECT DISTINCT engagement.id, engagement.action
                    FROM story_engagements engagement
                    JOIN scoped_calls calls
                      ON calls.target_id = engagement.target_id
                     AND calls.story_fingerprint = engagement.story_fingerprint
                     AND calls.tool_name = engagement.action
                    WHERE engagement.user_id = %(user_id)s
                      AND engagement.ig_account_id = %(account_id)s
                      AND calls.status = 'confirmed'
                      AND engagement.status = 'sent'
                      AND engagement.created_at >= calls.run_started_at
                      AND engagement.executed_at >= calls.run_started_at
                      AND engagement.executed_at <= %(completed_at)s
                ), action_counts AS (
                    SELECT count(*) FILTER (WHERE action = 'like') AS likes,
                           count(*) FILTER (WHERE action = 'reply') AS replies,
                           count(*) FILTER (WHERE action = 'dm') AS dms
                    FROM sent_engagements
                ), failure_counts AS (
                    SELECT count(*) FILTER (
                        WHERE status IN ('failed', 'uncertain')
                    ) AS failed FROM scoped_calls
                )
                INSERT INTO run_summaries (
                    user_id, ig_account_id, job_id,
                    likes_sent, replies_sent, dms_sent,
                    opportunities_found, skipped, gated, failed,
                    input_tokens, output_tokens, images_analyzed,
                    video_frames_analyzed, retries, runtime_ms,
                    started_at, completed_at
                ) SELECT
                    job.user_id, job.ig_account_id, job.id,
                    actions.likes, actions.replies, actions.dms,
                    %(considered)s, %(skipped)s, %(gated)s,
                    greatest(%(failed)s, failures.failed),
                    %(input_tokens)s, %(output_tokens)s, %(images_analyzed)s,
                    %(video_frames_analyzed)s, %(retries)s, %(runtime_ms)s,
                    %(started_at)s, %(completed_at)s
                FROM scoped_job job
                CROSS JOIN action_counts actions
                CROSS JOIN failure_counts failures
                ON CONFLICT (job_id) DO NOTHING
                """,
                {
                    "user_id": summary.user_id,
                    "account_id": summary.ig_account_id,
                    "job_id": summary.job_id,
                    "considered": summary.considered,
                    "skipped": summary.skipped,
                    "gated": summary.gated,
                    "failed": summary.failed,
                    "input_tokens": summary.input_tokens,
                    "output_tokens": summary.output_tokens,
                    "images_analyzed": summary.images_analyzed,
                    "video_frames_analyzed": summary.video_frames_analyzed,
                    "retries": summary.retries,
                    "runtime_ms": summary.runtime_ms,
                    "started_at": started_at,
                    "completed_at": completed_at,
                },
            )


class BrowserMediaFetcher:
    """Fetch story media through the account's own proxied browser context.

    Using the browser's request API rather than a separate HTTP client keeps
    the media request on the same IP, cookies, and TLS fingerprint as the page
    that referenced it.
    """

    def __init__(self, context: Any) -> None:
        self._context = context

    def __repr__(self) -> str:
        return "BrowserMediaFetcher(context=<browser>)"

    async def fetch(self, url: str) -> tuple[str, bytes]:
        if type(url) is not str or not url.startswith("https://"):
            raise ValueError("story media must be fetched over https")
        response = await self._context.request.get(url, timeout=_MEDIA_TIMEOUT_MS)
        if not response.ok:
            raise RuntimeError("story media request failed")
        content_type = response.headers.get("content-type")
        if type(content_type) is not str or not content_type.strip():
            raise RuntimeError("story media has no content type")
        return content_type.split(";")[0].strip().lower(), await response.body()


class DiscardingArtifactSink:
    """Drop diagnostic artifacts.

    Failure screenshots and DOM excerpts are derived from a logged-in
    Instagram session, so collecting nothing stays the default until an
    operator opts in to a private directory with `ARTIFACT_DIR`.
    """

    def __repr__(self) -> str:
        return "DiscardingArtifactSink()"

    async def write(self, artifact: object) -> None:
        return None


def artifact_sink_for(artifact_dir: str | None, job: ClaimedJob) -> ArtifactSink:
    """Choose one run's diagnostic destination from configuration.

    Diagnostics are opt-in: with no configured directory the run keeps the
    sink that drops everything, so a worker collects nothing by default.
    """

    if type(artifact_dir) is not str or not artifact_dir.strip():
        return DiscardingArtifactSink()
    return FilesystemArtifactSink(Path(artifact_dir.strip()), job.ig_account_id, job.id)


async def resolve_page(session: Any) -> Any:
    """Return the session's page, opening one only if the context has none."""

    pages = list(getattr(session.context, "pages", []) or [])
    if pages:
        return pages[0]
    return await session.context.new_page()


def build_runner(
    settings: Settings,
    pool: ConnectionPool,
    playwright: Any,
    *,
    hermes: HermesRunClient,
    registry: ActiveRunRegistry,
    tool_repository: ExecutionMcpRepository,
    execution_mode: str,
    jobs: JobsPort | None = None,
    context_loader_factory: Callable[[ContextLoaderPort], ContextLoaderPort]
    | None = None,
    follower_store: PostgresFollowerStore | None = None,
    check_deadline: Any = None,
) -> WorkerRunner:
    """Wire one production worker from settings and live infrastructure."""

    history = PostgresStoryHistoryRepository(pool)
    executions = ExecutionRepository(pool)
    pending_discovery: dict[int, _SessionBoundDiscovery] = {}

    def discovery_factory(session: Any) -> DiscoveryService:
        discovery = _SessionBoundDiscovery(
            session, history, follower_store if follower_store is not None else PostgresFollowerStore(pool)
        )
        pending_discovery[id(session)] = discovery
        return discovery

    def executor_factory(session: Any, job: ClaimedJob) -> Executor:
        return _SessionBoundExecutor(
            session,
            executions,
            artifact_sink_for(settings.artifact_dir, job),
            pending_discovery.pop(id(session)),
            pool=pool,
            job=job,
            allow_replies=execution_mode == "autonomous",
        )

    loader: ContextLoaderPort = RunContextLoader(
        reader=PostgresRunContextReader(pool),
        cipher_for=lambda ig_account_id: SessionCipher(
            settings.session_encryption_key.get_secret_value(), ig_account_id
        ),
        fingerprints=FingerprintFactory.from_settings(settings),
        proxy_bindings=ProxyBindingService(
            ProxyRepository(pool, settings.session_encryption_key.get_secret_value()),
            IPRoyalProvider.from_settings(settings),
        ),
    )
    if context_loader_factory is not None:
        loader = context_loader_factory(loader)

    return WorkerRunner(
        jobs=jobs
        if jobs is not None
        else AsyncJobRepository(
            JobRepository(pool, lease_seconds=settings.job_lease_seconds)
        ),
        loader=loader,
        browser_factory=LeasedBrowserFactory(
            pool,
            BrowserContextFactory(playwright=playwright),
        ),
        discovery_factory=discovery_factory,
        executor_factory=executor_factory,
        hermes=hermes,
        registry=registry,
        tool_repository=tool_repository,
        execution_mode=execution_mode,
        accounts=AsyncAccountPauser(
            AccountRepository(pool, settings.session_encryption_key.get_secret_value())
        ),
        summaries=AsyncRunSummaryRepository(pool),
        check_deadline=check_deadline,
        heartbeat_interval_seconds=float(settings.heartbeat_interval_seconds),
    )


class _SessionBoundDiscovery:
    """Build the real DiscoveryService once the session's page exists.

    The page is only reachable after Chrome launches, but the runner asks for
    the discovery service before its first call, so resolution is deferred to
    the first `find`.
    """

    def __init__(
        self,
        session: Any,
        history: PostgresStoryHistoryRepository,
        followers: Any = None,
    ) -> None:
        self._session = session
        self._history = history
        self._service: DiscoveryService | None = None
        self._follower_store = followers
        self._follower_service: FollowerDiscovery | None = None
        self._follower_browser: Any = None
        self._verified_profile: tuple[str, str, float] | None = None

    def __repr__(self) -> str:
        return "_SessionBoundDiscovery(session=<browser>)"

    async def find(self, context: Any, targets: Any, budget: Any) -> Any:
        if self._service is None:
            self._service = DiscoveryService(
                ReadOnlyStoryBrowser(self._session.context),
                self._history,
            )
        return await self._service.find(context, targets, budget)

    async def find_followers(self, run: Any, limit: int) -> Any:
        if self._follower_service is None:
            page = await resolve_page(self._session)
            self._follower_browser = PlaywrightFollowerBrowser(page)
            self._follower_service = FollowerDiscovery(
                browser=self._follower_browser,
                store=self._follower_store,
                stories=_FollowerStoryDiscovery(self),
            )
        return await self._follower_service.find(run, limit)

    def follower_report(self) -> dict:
        return self._follower_service.report() if self._follower_service else {}

    async def recheck_prospect(self, run: Any, opportunity: Any) -> str | None:
        self._verified_profile = None
        if self._follower_browser is None:
            return "profile_unreadable"
        profile = await self._follower_browser.inspect(opportunity.target_handle)
        excluded = {
            run.context.username.lower(),
            *(s["username"].lower() for s in run.config_snapshot["follower_sources"]),
        }
        reason = prospect_reason(profile, opportunity.owner_id, excluded)
        if reason == "available":
            self._verified_profile = (
                opportunity.target_handle,
                opportunity.owner_id,
                time.monotonic(),
            )
        return None if reason == "available" else reason

    async def preflight_health(self, opportunity: Any) -> HealthSignal:
        """Reuse the completed relationship check without opening a Story."""
        try:
            if self._follower_service is None:
                # Manual targets do not run the follower policy recheck.
                if self._follower_browser is None:
                    self._follower_browser = PlaywrightFollowerBrowser(
                        await resolve_page(self._session)
                    )
                profile = await self._follower_browser.inspect(
                    opportunity.target_handle
                )
                if profile["owner_id"] != opportunity.owner_id:
                    return HealthSignal.HEALTH_UNAVAILABLE
                self._verified_profile = (
                    opportunity.target_handle,
                    opportunity.owner_id,
                    time.monotonic(),
                )
            evidence = self._verified_profile
            if (
                evidence is None
                or evidence[:2] != (opportunity.target_handle, opportunity.owner_id)
                or not 0 <= time.monotonic() - evidence[2] <= 30
            ):
                return HealthSignal.HEALTH_UNAVAILABLE
            await self._follower_browser._check_page()
            url = urlsplit(self._follower_browser.page.url)
            if (
                url.scheme != "https"
                or url.netloc not in {"www.instagram.com", "instagram.com"}
                or url.path.rstrip("/") != "/" + opportunity.target_handle
            ):
                return HealthSignal.HEALTH_UNAVAILABLE
            return HealthSignal.NONE
        except Exception as error:  # noqa: BLE001 - browser failures become bounded health signals
            return {
                "rate_limited": HealthSignal.RATE_LIMIT,
                "authentication_required": HealthSignal.LOGIN_REQUIRED,
                "checkpoint_required": HealthSignal.CHECKPOINT,
            }.get(getattr(error, "reason", None), HealthSignal.HEALTH_UNAVAILABLE)


class _FollowerStoryDiscovery:
    """Sample exact media through the isolated, mutation-blocked preview."""

    def __init__(self, discovery: Any) -> None:
        self.discovery = discovery

    async def find(self, context: Any, targets: Any, budget: Any) -> Any:
        return await self.discovery.find(context, targets, budget)


class _SessionBoundExecutor:
    """Build the real Executor once the session's page exists."""

    def __init__(
        self,
        session: Any,
        executions: ExecutionRepository,
        artifact_sink: ArtifactSink,
        discovery: _SessionBoundDiscovery,
        *,
        pool: ConnectionPool | None = None,
        job: ClaimedJob | None = None,
        allow_replies: bool = False,
    ) -> None:
        self._session = session
        self._executions = executions
        self._artifact_sink = artifact_sink
        self._discovery = discovery
        self._pool = pool
        self._job = job
        self._allow_replies = allow_replies

    def __repr__(self) -> str:
        return "_SessionBoundExecutor(session=<browser>, sink=<configured>)"

    async def _prepare_reply_follow_claim(self, opportunity: Any, engagement_id: UUID, context: Any):
        """Reserve and finish the follow that must precede one Story reply."""
        if self._pool is None or self._job is None:
            return None
        store = PostgresFollowStore(self._pool)
        claim = await store.reserve_story_reply_follow(
            user_id=context.user_id,
            account_id=context.ig_account_id,
            target_id=opportunity.target_id,
            owner_id=opportunity.owner_id,
            engagement_id=engagement_id,
            job_id=self._job.id,
            worker_id=self._job.claimed_by,
            now=datetime.now(timezone.utc),
        )
        if claim is None:
            return None
        maintenance = FollowMaintenance(
            store,
            StoryFollowPerformer(self._pool, context.browser),
            execution_mode="autonomous",
            clock=lambda: datetime.now(timezone.utc),
        )
        finished, outcome = await maintenance.execute_claim(claim)
        if finished is not True or outcome != "confirmed":
            return None
        if not await store.reply_ready(claim, datetime.now(timezone.utc)):
            return None
        return claim

    async def prepare_reply_follow(self, opportunity: Any, engagement_id: UUID, context: Any) -> bool:
        """Follow and durably confirm a Story target before replying."""
        return await self._prepare_reply_follow_claim(opportunity, engagement_id, context) is not None

    async def execute(self, opportunity: Any, decision: Any, context: Any) -> Any:
        async def preflight_health() -> HealthSignal:
            return await self._discovery.preflight_health(opportunity)

        reply_claim = None

        async def before_action(engagement_id: UUID, action_context: Any) -> bool:
            nonlocal reply_claim
            reply_claim = await self._prepare_reply_follow_claim(
                opportunity, engagement_id, action_context
            )
            return reply_claim is not None

        async def reply_authorized() -> bool:
            if reply_claim is None or self._pool is None:
                return False
            return await PostgresFollowStore(self._pool).reply_ready(
                reply_claim, datetime.now(timezone.utc)
            )

        try:
            page = await resolve_page(self._session)
            adapter_args = {
                "preflight_health": preflight_health,
                "allow_reply_measurement": self._allow_replies,
            }
            if getattr(decision, "action", None) == "reply":
                adapter_args["reply_authorized"] = reply_authorized
            adapter = LiveStoryAdapter(
                page, self._artifact_sink, opportunity, **adapter_args
            )
            executor = Executor(self._executions, adapter)
        except asyncio.CancelledError as exc:
            exc.__dict__.pop("_engagemate_story_result", None)
            raise

        def with_evidence(result):
            evidence = adapter.action_evidence
            if result.action_evidence == "post_receipt_health_failed":
                evidence = (evidence + "," if evidence else "") + "post_receipt_health=failed"
            return replace(result, action_evidence=evidence)

        try:
            if getattr(decision, "action", None) == "reply":
                result = await executor.execute(
                    opportunity, decision, context, before_action=before_action
                )
            else:
                result = await executor.execute(opportunity, decision, context)
        except asyncio.CancelledError as exc:
            result = getattr(exc, "_engagemate_story_result", None)
            if type(result) is ExecutionResult and result.status is ExecutionResultStatus.SENT:
                exc._engagemate_story_result = with_evidence(result)
            raise
        return with_evidence(result)


__all__ = [
    "AsyncAccountPauser",
    "AsyncJobRepository",
    "AsyncRunSummaryRepository",
    "BrowserMediaFetcher",
    "DiscardingArtifactSink",
    "FilesystemArtifactSink",
    "artifact_sink_for",
    "build_runner",
    "resolve_page",
]
