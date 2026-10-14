"""One browser-bound execution run exposed through the private MCP server."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from engagemate.domain.budgets import ActionBudget
from engagemate.domain.models import Action, ClaimedJob
from engagemate.intelligence.text_gate import TextGate
from engagemate.worker.check_deadline import CheckDeadline
from engagemate.worker.executor import ExecutionContext, Executor
from engagemate.worker.run_context import RunContext
from engagemate.worker.discovery import DiscoveryService
from engagemate.worker.story_types import DiscoveryContext, StoryOpportunity


class ActiveRunConflict(RuntimeError):
    """Raised when a second job tries to bind to the worker MCP server."""


class NoActiveRun(RuntimeError):
    """Raised when Hermes calls a tool outside a scheduled job."""


@dataclass(slots=True)
class ActiveRun:
    run_id: UUID
    job: ClaimedJob
    context: RunContext
    config_version: int
    config_snapshot: Mapping[str, object]
    discovery_context: DiscoveryContext
    execution_context: ExecutionContext
    budget: ActionBudget
    text_gate: TextGate
    lease_is_live: Callable[[], Awaitable[bool]]
    discovery: DiscoveryService
    executor: Executor
    pace: Callable[[Action, StoryOpportunity], Awaitable[None]]
    opportunities: dict[UUID, StoryOpportunity] = field(default_factory=dict)
    completed: bool = False
    tool_repository: Any | None = None
    execution_mode: str = "disabled"
    view_only: bool = False
    hermes_session_id: str | None = None
    completion_reason: str | None = None
    completion_counters: dict[str, int] = field(default_factory=dict)
    check_deadline: CheckDeadline | None = None
    tool_tasks: set[asyncio.Task] = field(default_factory=set)
    claimed_opportunities: set[UUID] = field(default_factory=set)
    stopping: bool = False

    async def stop_tools(self) -> None:
        """Cancel only this run's calls before its browser and binding close."""
        self.stopping = True
        tasks = self.tool_tasks - {asyncio.current_task()}
        for task in tasks:
            if not task.done():
                task.cancel()

        async def drain():
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=10)
                for task in pending:
                    task.cancel()

        cleanup = asyncio.create_task(drain())
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        await cleanup
        if interrupted:
            raise asyncio.CancelledError


class ActiveRunRegistry:
    """Bind the process-wide private MCP toolset to one scheduled run."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._active: ActiveRun | None = None

    @contextmanager
    def activate(self, run: ActiveRun) -> Iterator[ActiveRun]:
        with self._lock:
            if self._active is not None:
                raise ActiveRunConflict("one worker already owns an active run")
            self._active = run
        try:
            yield run
        finally:
            with self._lock:
                if self._active is run:
                    self._active = None

    def require(self) -> ActiveRun:
        with self._lock:
            if self._active is None:
                raise NoActiveRun("no scheduled EngageMate run is active")
            return self._active
