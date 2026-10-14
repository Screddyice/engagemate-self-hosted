"""Run one reserved job through real Hermes with a reduced pilot budget.

The operator reserves the exact job and prepares any live-action approval.
This command never enables an account, raises saved limits, or grants approval.
The production worker must release its private MCP port for this window.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import sys
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from engagemate.db.jobs import JobRepository
from engagemate.domain.budgets import ActionBudget
from engagemate.domain.models import Action, ClaimedJob, JobStatus
from engagemate.worker.active_run import NoActiveRun
from engagemate.worker.run_context import RunContextError


class CanaryError(RuntimeError):
    """A credential-free operator failure code."""


@dataclass(frozen=True, slots=True)
class CanaryScope:
    user_id: UUID
    account_id: UUID
    job_id: UUID
    worker_id: str
    reply_only: bool = False

    def __post_init__(self):
        if any(
            type(value) is not UUID
            for value in (self.user_id, self.account_id, self.job_id)
        ):
            raise ValueError("canary scope requires UUID identifiers")
        if type(self.worker_id) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.worker_id):
            raise ValueError("invalid canary worker id")
        if type(self.reply_only) is not bool:
            raise ValueError("reply-only canary selection must be boolean")


class ExactJobRepository:
    """Yield one existing owned lease; never inspect or claim the global queue."""

    def __init__(self, pool: Any, scope: CanaryScope, *, lease_seconds: int):
        self.pool, self.scope = pool, scope
        self.repository = JobRepository(pool, lease_seconds=lease_seconds)
        self.yielded = False
        self._lock = asyncio.Lock()

    async def claim_next(self, worker_id: str) -> ClaimedJob | None:
        async with self._lock:
            if self.yielded:
                return None
            if worker_id != self.scope.worker_id:
                raise CanaryError("reserved_job_unavailable")
            job = await asyncio.to_thread(self._read_reserved)
            if job is None:
                raise CanaryError("reserved_job_unavailable")
            self.yielded = True
            return job

    def _read_reserved(self):
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs WHERE id=%(job_id)s
                  AND user_id=%(user_id)s AND ig_account_id=%(account_id)s
                  AND claimed_by=%(worker_id)s AND status='running'
                  AND lease_expires_at > now()
                FOR SHARE
            """,
                asdict(self.scope),
            ).fetchone()
        return ClaimedJob.from_row(row) if row is not None else None

    async def heartbeat(self, job_id, worker_id):
        if (
            not self.yielded
            or job_id != self.scope.job_id
            or worker_id != self.scope.worker_id
        ):
            return False
        return await asyncio.to_thread(self.repository.heartbeat, job_id, worker_id)

    async def defer(self, job_id, worker_id, error, *, delay_seconds=300):
        """Finish a scoped canary failure instead of returning it to the queue."""
        if (
            not self.yielded
            or job_id != self.scope.job_id
            or worker_id != self.scope.worker_id
        ):
            return False
        return await asyncio.to_thread(
            self.repository.finish, job_id, worker_id, JobStatus.FAILED, error
        )

    async def finish(self, job_id, worker_id, status, error=None):
        if (
            not self.yielded
            or job_id != self.scope.job_id
            or worker_id != self.scope.worker_id
        ):
            return False
        return await asyncio.to_thread(
            self.repository.finish, job_id, worker_id, status, error
        )


class FreshExactJobRepository(ExactJobRepository):
    """Claim one fresh queued job through the production repository guard."""

    def _read_reserved(self):
        return self.repository.claim_exact(
            self.scope.job_id, self.scope.user_id, self.scope.account_id,
            self.scope.worker_id,
        )


class CanaryContextLoader:
    """Retain the real context and shrink its current budget for this run only."""

    def __init__(self, loader: Any, scope: CanaryScope):
        self.loader, self.scope = loader, scope

    async def load(self, job):
        if (job.id, job.user_id, job.ig_account_id, job.claimed_by) != (
            self.scope.job_id,
            self.scope.user_id,
            self.scope.account_id,
            self.scope.worker_id,
        ):
            raise RunContextError("tenant_mismatch")
        context = await self.loader.load(job)
        if (context.user_id, context.ig_account_id) != (
            self.scope.user_id,
            self.scope.account_id,
        ):
            raise RunContextError("tenant_mismatch")
        original = context.budget
        replies = (
            min(original.remaining_for(Action.REPLY), 1) if self.scope.reply_only else 0
        )
        reduced = ActionBudget(
            likes=0 if self.scope.reply_only else min(original.remaining_for(Action.LIKE), 1),
            replies=replies,
            dms=0,
            plan_total=min(original.plan_total, 1),
            views=0 if self.scope.reply_only else min(original.remaining_for(Action.VIEW), 1),
            private_messages=replies,
        )
        snapshot = dict(context.config_snapshot)
        snapshot["canary"] = {
            "job_id": str(self.scope.job_id),
            "worker_id": self.scope.worker_id,
            "execution_mode": "canary",
            "reply_only": self.scope.reply_only,
            "original_budget": asdict(original),
            "effective_budget": asdict(reduced),
        }
        return replace(context, budget=reduced, config_snapshot=snapshot)


class ProductionReplyContextLoader(CanaryContextLoader):
    async def load(self, job):
        context = await super().load(job)
        if not context.config_snapshot.get("follower_sources"):
            raise RunContextError("approved_follower_sources_required")
        if context.budget.remaining_for(Action.REPLY) != 1:
            raise RunContextError("budget_exhausted")
        snapshot = dict(context.config_snapshot)
        snapshot["production_check"] = {
            **snapshot.pop("canary"), "execution_mode": "autonomous",
            "new_prospect_only": True,
        }
        return replace(context, config_snapshot=snapshot)


class ProductionStoryContextLoader(CanaryContextLoader):
    def __init__(self, loader, scope, action: Action):
        super().__init__(loader, scope)
        if type(action) is not Action or action not in (Action.VIEW, Action.LIKE) or scope.reply_only:
            raise CanaryError("invalid_production_story_action")
        self.action = action

    async def load(self, job):
        context = await super().load(job)
        if not context.config_snapshot.get("follower_sources"):
            raise RunContextError("approved_follower_sources_required")
        if context.budget.remaining_for(self.action) != 1:
            raise RunContextError("budget_exhausted")
        reduced = ActionBudget(
            likes=int(self.action is Action.LIKE), replies=0, dms=0,
            plan_total=int(self.action is Action.LIKE),
            views=int(self.action is Action.VIEW), private_messages=0,
        )
        snapshot = dict(context.config_snapshot)
        snapshot["production_check"] = {
            **snapshot.pop("canary"), "execution_mode": "autonomous",
            "new_prospect_only": True, "selected_action": self.action.value,
            "effective_budget": asdict(reduced),
        }
        return replace(context, budget=reduced, config_snapshot=snapshot)


def _story_identity(registry, values):
    """Report measured public IDs only after binding them to this exact attempt."""
    from engagemate.worker.story_fingerprint import StoryFingerprintError, fingerprint_story
    from engagemate.worker.story_types import StoryOpportunity

    try:
        active = registry.require()
        if (active.run_id, active.job.id, active.context.user_id,
            active.context.ig_account_id, active.job.claimed_by) != tuple(
                values[key] for key in ("run_id", "job_id", "user_id", "ig_account_id", "worker_id")):
            return None
        measured = active.opportunities.get(values["opportunity_id"])
        if (type(measured) is not StoryOpportunity
            or type(measured.target_id) is not UUID or type(values["opportunity_id"]) is not UUID
            or type(measured.owner_id) is not str or not re.fullmatch(r"[0-9]{1,30}", measured.owner_id)
            or type(measured.media_id) is not str or not re.fullmatch(r"[0-9]{1,30}(?:_[0-9]{1,30})?", measured.media_id)
            or measured.target_id != values["target_id"]
            or measured.fingerprint != values["story_fingerprint"]
            or fingerprint_story(measured.owner_id, measured.media_id, measured.taken_at, None) != measured.fingerprint):
            return None
        return {"owner_id": measured.owner_id, "media_id": measured.media_id,
                "target_id": measured.target_id, "opportunity_id": values["opportunity_id"],
                "story_fingerprint": measured.fingerprint}
    except (NoActiveRun, AttributeError, KeyError, StoryFingerprintError):
        return None


def _bind_mcp_socket(host: str, port: int) -> socket.socket:
    if host != "127.0.0.1":
        raise CanaryError("canary_mcp_requires_loopback")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(128)
        listener.setblocking(False)
    except OSError:
        listener.close()
        raise CanaryError("canary_mcp_port_unavailable") from None
    return listener


async def _serve_one(server, listener, runner, scope, *, timeout_seconds, abort):
    """Start MCP before the exact job, and tear down both on any failure."""
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    job = None
    try:
        async with asyncio.timeout(15):
            while not server.started:
                if serving.done():
                    raise CanaryError("canary_mcp_start_failed")
                await asyncio.sleep(0.01)
        job = asyncio.create_task(runner.run_once(scope.worker_id))
        done, _ = await asyncio.wait(
            {job, serving}, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
        )
        if job in done:
            return await job
        raise CanaryError("canary_mcp_stopped" if serving in done else "canary_timeout")
    except BaseException as error:
        # Capture the active audit identity before cancelling the runner, whose
        # registry binding disappears during cleanup. Abort itself is scoped.
        cleanup = abort(
            str(error) if isinstance(error, CanaryError) else "canary_interrupted"
        )
        if job is not None and not job.done():
            job.cancel()
        try:
            if job is not None:
                await asyncio.wait_for(
                    asyncio.gather(job, return_exceptions=True), timeout=30
                )
        finally:
            async with asyncio.timeout(15):
                await cleanup
        raise
    finally:
        server.should_exit = True
        try:
            async with asyncio.timeout(10):
                await asyncio.shield(serving)
        except asyncio.CancelledError:
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
            raise
        except Exception:  # noqa: BLE001 - cleanup must complete after server failure
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)


async def run_canary(scope: CanaryScope, *, timeout_seconds: int = 300):
    return await _run_scoped_job(scope, timeout_seconds=timeout_seconds)


async def _run_scoped_job(
    scope: CanaryScope, *, timeout_seconds: int = 300,
    production_reply_check: bool = False,
    production_story_action: Action | None = None,
    story_report: dict | None = None,
):
    if story_report is not None and (type(story_report) is not dict or story_report or production_story_action is None):
        raise CanaryError("invalid_production_story_report")
    if production_story_action is not None and (
        type(production_story_action) is not Action
        or production_story_action not in (Action.VIEW, Action.LIKE)
        or production_reply_check or scope.reply_only
    ):
        raise CanaryError("invalid_production_story_action")
    if type(production_reply_check) is not bool or (production_reply_check and not scope.reply_only):
        raise CanaryError("invalid_production_reply_check")
    production_check = production_reply_check or production_story_action is not None
    if not 1 <= timeout_seconds <= 300:
        raise CanaryError("canary_timeout_must_be_1_to_300_seconds")
    import httpx
    import uvicorn
    from playwright.async_api import async_playwright

    from deploy.healthcheck import validate_worker_environment
    from engagemate.config import Settings
    from engagemate.db.pool import create_pool
    from engagemate.worker.active_run import ActiveRunRegistry
    from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository
    from engagemate.worker.execution_mcp_server import build_execution_mcp_app
    from engagemate.worker.hermes_run import HermesRunClient
    from engagemate.worker.follower_store import PostgresFollowerStore
    from engagemate.worker.main import require_execution_key
    from engagemate.worker.wiring import build_runner

    validate_worker_environment(os.environ)
    settings = Settings()
    if production_check and settings.instagram_execution_mode != "autonomous":
        raise CanaryError("production_check_requires_saved_autonomous_mode")
    key = require_execution_key(settings.execution_mcp_key)
    # Reserve the exact configured endpoint before touching a database job.
    with _bind_mcp_socket(
        settings.execution_mcp_host, settings.execution_mcp_port
    ) as listener:
        pool = create_pool(settings.database_url, min_size=1, max_size=4)
        try:
            registry = ActiveRunRegistry()
            class FreshRuns(ExecutionMcpRepository):
                first_attempt = None

                async def reconcile_abandoned_runs(self):
                    # Fresh exact checks cannot recover older work in any tenant.
                    return 0

                async def begin_mutation(self, **values):
                    if production_story_action is not None:
                        # MCP serializes calls. Pin the first attempt as well as
                        # its Story: restored safe-skip budgets cannot move on.
                        attempt = tuple(values[key] for key in (
                            "run_id", "job_id", "user_id", "ig_account_id", "worker_id",
                            "opportunity_id", "target_id", "story_fingerprint", "idempotency_key",
                        ))
                        if values["action"] != production_story_action.value or (
                            self.first_attempt is not None and attempt != self.first_attempt
                        ):
                            return {"allowed": False, "replay": False,
                                    "reason": "production_check_attempt_exhausted"}
                        self.first_attempt = attempt
                        if story_report is not None and not story_report:
                            identity = _story_identity(registry, values)
                            if identity is not None:
                                story_report.update(identity)
                    return await super().begin_mutation(**values)

            repository = (FreshRuns if production_check else ExecutionMcpRepository)(pool)
            jobs = (FreshExactJobRepository if production_check else ExactJobRepository)(
                pool, scope, lease_seconds=settings.job_lease_seconds
            )
            app = build_execution_mcp_app(
                registry, key, host=settings.execution_mcp_host
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host=settings.execution_mcp_host,
                    port=settings.execution_mcp_port,
                    access_log=False,
                    log_level="warning",
                    timeout_graceful_shutdown=5,
                )
            )

            def abort(reason):
                try:
                    active = registry.require()
                    run_id = (
                        active.run_id
                        if (
                            active.job.id,
                            active.context.user_id,
                            active.context.ig_account_id,
                        )
                        == (scope.job_id, scope.user_id, scope.account_id)
                        else None
                    )
                except NoActiveRun:
                    run_id = None

                async def terminalize():
                    if not jobs.yielded or not await jobs.heartbeat(
                        scope.job_id, scope.worker_id
                    ):
                        return
                    try:
                        if run_id is not None:
                            await repository.fail_run(
                                run_id=run_id,
                                reason=reason,
                                completed_at=datetime.now(UTC),
                            )
                    finally:
                        await jobs.finish(
                            scope.job_id, scope.worker_id, JobStatus.FAILED, reason
                        )

                return terminalize()

            async with (
                async_playwright() as playwright,
                httpx.AsyncClient(
                    timeout=httpx.Timeout(
                        min(settings.hermes_execution_timeout_seconds, timeout_seconds),
                        connect=15,
                    ),
                ) as http,
            ):
                hermes = HermesRunClient(
                    http=http,
                    endpoint=settings.hermes_agent_url,
                    api_key=settings.hermes_agent_api_key,
                    agent_model=settings.hermes_agent_model,
                )
                from engagemate.worker.check_deadline import CheckDeadline, DeadlineRunner

                deadline = CheckDeadline(
                    production_story_action or Action.REPLY, timeout_seconds
                ) if production_check else None
                runner = build_runner(
                    settings,
                    pool,
                    playwright,
                    hermes=hermes,
                    registry=registry,
                    tool_repository=repository,
                    execution_mode="autonomous" if production_check else "canary",
                    jobs=jobs,
                    check_deadline=deadline,
                    follower_store=PostgresFollowerStore(pool, require_new_relationship=True)
                    if production_check else None,
                    context_loader_factory=lambda loader: (
                        ProductionStoryContextLoader(loader, scope, production_story_action)
                        if production_story_action is not None else
                        (ProductionReplyContextLoader if production_reply_check else CanaryContextLoader)(loader, scope)
                    ),
                )
                return await _serve_one(
                    server,
                    listener,
                    DeadlineRunner(runner, deadline) if deadline is not None else runner,
                    scope,
                    timeout_seconds=timeout_seconds,
                    abort=abort,
                )
        finally:
            pool.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=UUID)
    parser.add_argument("--account-id", required=True, type=UUID)
    parser.add_argument("--job-id", required=True, type=UUID)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--timeout-seconds", default=300, type=int)
    parser.add_argument(
        "--reply-only", action="store_true",
        help="Allow at most one saved, separately approved Story reply; disable other actions.",
    )
    args = parser.parse_args(argv)
    try:
        scope = CanaryScope(
            args.user_id, args.account_id, args.job_id, args.worker_id,
            reply_only=args.reply_only,
        )
        summary = asyncio.run(run_canary(scope, timeout_seconds=args.timeout_seconds))
    except CanaryError as error:
        print(f"canary refused: {error}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - never print credential-bearing driver errors
        print(
            "canary refused or failed; inspect the scoped job and run audit",
            file=sys.stderr,
        )
        return 2
    if summary is None:
        print("canary produced no job summary", file=sys.stderr)
        return 2
    print(json.dumps(asdict(summary), default=str, sort_keys=True))
    # A completed no-op is not a successful live-action proof.
    return 0 if summary.status is JobStatus.COMPLETED and summary.engaged > 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
