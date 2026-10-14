"""Worker entrypoint: claim and run jobs one at a time, forever.

Run with `python -m engagemate.worker.main`. The process owns no tenant state
of its own — every run rebuilds its context from the database, so restarting a
worker can never carry one account's session, proxy, or budget into another's.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Protocol

from engagemate.worker.runner import RunSummary

logger = logging.getLogger(__name__)

# How long the worker waits before asking for a job again when there was none.
# This is the single biggest source of idle database traffic: at the previous
# 5-second default it queried ~17,280 times a day and, on a scale-to-zero
# Postgres, that alone kept the compute awake around the clock.
#
# The default is now longer than Neon's 300s idle-suspend threshold, because
# anything shorter pins the compute permanently awake and bills ~730 h/month
# whether or not a single account is connected. Being under the threshold was
# not a tuning choice anyone made; 5 seconds was simply the value the loop was
# written with, and it silently set the floor on the database bill.
#
# The cost is pickup latency: an enqueued job may now wait up to this long
# before a worker claims it. Jobs are enqueued once per account per day when
# `schedule_hour` arrives, so minutes of delay do not change what a story
# engagement looks like. Set WORKER_IDLE_SECONDS lower in development, where
# the database is local and latency is what you are watching.
_DEFAULT_IDLE_SECONDS = 900.0
_MAX_WORKER_ID = 128
# An idle worker is otherwise silent, which reads exactly like a hung one. Log a
# heartbeat on a wall-clock interval rather than an idle-tick count, so the gap
# between lines stays the same whatever `idle_seconds` is set to.
_HEARTBEAT_SECONDS = 300.0


class RunnerPort(Protocol):
    async def run_once(self, worker_id: str) -> RunSummary | None: ...


async def run_loop(
    runner: RunnerPort,
    worker_id: str,
    *,
    idle_seconds: float = _DEFAULT_IDLE_SECONDS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    should_continue: Callable[[], bool] | None = None,
    heartbeat_seconds: float = _HEARTBEAT_SECONDS,
    now: Callable[[], float] | None = None,
) -> None:
    """Drive `run_once` until told to stop, idling when no job is due."""

    if not worker_id:
        raise ValueError("worker_id must not be empty")
    if type(idle_seconds) is not float or idle_seconds <= 0:
        raise ValueError("idle_seconds must be positive")
    if type(heartbeat_seconds) is not float or heartbeat_seconds <= 0:
        raise ValueError("heartbeat_seconds must be positive")

    wait = sleep or asyncio.sleep
    permitted = should_continue or (lambda: True)
    clock = now or monotonic
    idle_since: float | None = None
    last_heartbeat = clock()

    while permitted():
        idle = False
        try:
            summary = await runner.run_once(worker_id)
            idle = summary is None
            if summary is not None:
                logger.info(
                    "run finished job=%s status=%s considered=%d engaged=%d "
                    "skipped=%d failed=%d reason=%s job_persisted=%s",
                    summary.job_id,
                    summary.status.value,
                    summary.considered,
                    summary.engaged,
                    summary.skipped,
                    summary.failed,
                    summary.stop_reason,
                    summary.job_persisted,
                )
        except asyncio.CancelledError:
            raise
        # One bad run must never take the worker process down.
        except Exception:
            logger.exception("run failed worker=%s", worker_id)
            idle = True
        if idle:
            # Report liveness while nothing is due, so a quiet worker is
            # distinguishable from a stuck one without touching the database.
            tick = clock()
            if idle_since is None:
                idle_since = tick
            if tick - last_heartbeat >= heartbeat_seconds:
                logger.info(
                    "worker idle id=%s idle_seconds=%d",
                    worker_id,
                    int(tick - idle_since),
                )
                last_heartbeat = tick
            await wait(idle_seconds)
        else:
            idle_since = None
            last_heartbeat = clock()


def idle_seconds() -> float:
    """Return the idle wait, overridable via WORKER_IDLE_SECONDS.

    Raising this does not slow a busy worker: `run_loop` only waits when a
    cycle found no job, and drains the queue back to back while work exists.
    It only lengthens how long an enqueued job may sit before pickup.
    """

    raw = os.environ.get("WORKER_IDLE_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_IDLE_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("WORKER_IDLE_SECONDS must be a positive number") from exc
    if value <= 0:
        raise ValueError("WORKER_IDLE_SECONDS must be a positive number")
    return value


def worker_id() -> str:
    """Return a per-process identity that names its host.

    Job leases are owned by this exact string, so two live workers must never
    produce the same one.
    """

    configured = os.environ.get("WORKER_ID", "").strip()
    if configured:
        return configured[:_MAX_WORKER_ID]
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"[:_MAX_WORKER_ID]


def require_execution_key(value: object) -> str:
    """Extract the worker-only MCP key or refuse process startup."""

    getter = getattr(value, "get_secret_value", None)
    key = getter() if callable(getter) else ""
    if not isinstance(key, str) or len(key) < 32:
        raise RuntimeError("execution MCP key is required")
    return key


async def run_worker_services(
    worker: Awaitable[None],
    mcp_server: Awaitable[None],
    *,
    stopping: asyncio.Event,
) -> None:
    """Keep the job loop and private MCP server in one failure domain."""

    worker_task = asyncio.create_task(worker)
    mcp_task = asyncio.create_task(mcp_server)
    stop_task = asyncio.create_task(stopping.wait())
    services = {worker_task, mcp_task}
    done, _ = await asyncio.wait(
        {*services, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if stop_task in done:
        for task in services:
            task.cancel()
        await asyncio.gather(*services, return_exceptions=True)
        return

    stop_task.cancel()
    for task in services - done:
        task.cancel()
    await asyncio.gather(*(services - done), return_exceptions=True)
    completed = next(iter(done & services))
    error = completed.exception()
    if error is not None:
        raise error
    raise RuntimeError("worker service exited unexpectedly")


async def main() -> None:  # pragma: no cover - process entrypoint
    """Start one worker and stop it cleanly on SIGINT or SIGTERM."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    import httpx
    import uvicorn
    from playwright.async_api import async_playwright

    from deploy.healthcheck import validate_worker_environment
    from engagemate.config import Settings
    from engagemate.db.jobs import JobRepository
    from engagemate.db.pool import create_pool
    from engagemate.worker.active_run import ActiveRunRegistry
    from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository
    from engagemate.worker.execution_mcp_server import build_execution_mcp_app
    from engagemate.worker.follow_maintenance import FollowMaintenance, SequencedWorker
    from engagemate.worker.follow_reel_maintenance import ReelMessageMaintenance
    from engagemate.worker.follow_reel_runtime import LiveFollowReelPerformer
    from engagemate.worker.follow_reel_store import PostgresFollowReelStore
    from engagemate.worker.follow_runtime import LiveFollowPerformer
    from engagemate.worker.follow_store import PostgresFollowStore
    from engagemate.worker.hermes_run import HermesRunClient
    from engagemate.worker.reconciliation_runtime import LiveReconciliationReader
    from engagemate.worker.reconciliation_store import PostgresReconciliationStore
    from engagemate.worker.relationship_reconciliation import RelationshipReconciliation
    from engagemate.worker.wiring import AsyncJobRepository, build_runner

    validate_worker_environment(os.environ)
    settings = Settings()
    # One worker runs exactly one job at a time; the spare connections cover
    # the repository calls a single run makes off the event loop.
    pool = create_pool(settings.database_url, min_size=1, max_size=4)
    identity = worker_id()
    stopping = asyncio.Event()

    loop = asyncio.get_running_loop()
    for received in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(received, stopping.set)

    logger.info("worker starting id=%s", identity)
    try:
        registry = ActiveRunRegistry()
        tool_repository = ExecutionMcpRepository(pool)
        execution_key = require_execution_key(settings.execution_mcp_key)
        mcp_app = build_execution_mcp_app(
            registry,
            execution_key,
            host=settings.execution_mcp_host,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                mcp_app,
                host=settings.execution_mcp_host,
                port=settings.execution_mcp_port,
                access_log=False,
            )
        )
        timeout = httpx.Timeout(settings.hermes_execution_timeout_seconds)
        async with (
            async_playwright() as playwright,
            httpx.AsyncClient(timeout=timeout) as http,
        ):
            hermes = HermesRunClient(
                http=http,
                endpoint=settings.hermes_agent_url,
                api_key=settings.hermes_agent_api_key,
                agent_model=settings.hermes_agent_model,
            )
            runner = build_runner(
                settings,
                pool,
                playwright,
                hermes=hermes,
                registry=registry,
                tool_repository=tool_repository,
                execution_mode=settings.instagram_execution_mode,
                jobs=AsyncJobRepository(JobRepository(
                    pool, lease_seconds=settings.job_lease_seconds,
                    serialize_account_actions=True,
                )),
            )
            sequenced = SequencedWorker(
                runner,
                FollowMaintenance(
                    PostgresFollowStore(pool),
                    LiveFollowPerformer(pool, settings, playwright),
                    execution_mode=settings.instagram_execution_mode,
                ),
                RelationshipReconciliation(
                    PostgresReconciliationStore(pool),
                    LiveReconciliationReader(pool, settings, playwright),
                    execution_mode=settings.instagram_execution_mode,
                ),
                reel_messages=ReelMessageMaintenance(
                    PostgresFollowReelStore(pool),
                    LiveFollowReelPerformer(pool, settings, playwright),
                    execution_mode=settings.instagram_execution_mode,
                ),
            )
            await run_worker_services(
                run_loop(
                    sequenced,
                    identity,
                    idle_seconds=idle_seconds(),
                    should_continue=lambda: not stopping.is_set(),
                ),
                server.serve(),
                stopping=stopping,
            )
    finally:
        pool.close()
        logger.info("worker stopped id=%s", identity)


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    asyncio.run(main())
