"""A one-action operator check reserves time within its existing hard limit."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from engagemate.domain.models import Action
from engagemate.domain.pacing import PacingPolicy

logger = logging.getLogger(__name__)
MODEL_SECONDS = 30
ACTION_SECONDS = 45
COMPLETION_SECONDS = 15


class CheckDeadline:
    def __init__(self, action: Action, seconds: int, *, clock: Callable[[], float] = time.monotonic):
        if action not in {Action.VIEW, Action.LIKE, Action.REPLY} or not 1 <= seconds <= 300:
            raise ValueError("invalid operator check deadline")
        self.action, self.seconds, self.clock = action, seconds, clock
        self.started_at: float | None = None

    def start(self) -> None:
        if self.started_at is not None:
            raise RuntimeError("operator check deadline already started")
        self.started_at = self.clock()

    def remaining(self) -> float:
        if self.started_at is None:
            return 0
        return max(0, self.seconds - (self.clock() - self.started_at))

    def discovery_remaining(self) -> float:
        # Reserve the upper pacing bound, not a reduced check-only delay.
        pacing = 180 if self.action is Action.REPLY else 75
        return max(0, self.remaining() - pacing - MODEL_SECONDS - ACTION_SECONDS - COMPLETION_SECONDS)

    def can_admit(self, action: Action, seed: str) -> bool:
        allowed = action is self.action and self.remaining() >= (
            PacingPolicy.delay(action, seed).total_seconds() + ACTION_SECONDS + COMPLETION_SECONDS
        )
        self.record("action_admitted" if allowed else "action_admission_denied")
        return allowed

    def can_execute(self) -> bool:
        allowed = self.remaining() >= ACTION_SECONDS + COMPLETION_SECONDS
        if not allowed:
            self.record("action_admission_denied")
        return allowed

    def record(self, stage: str) -> None:
        if stage not in {"discovery_started", "discovery_finished", "action_admitted", "action_admission_denied"}:
            raise ValueError("invalid check stage")
        logger.info("production check stage=%s remaining_ms=%d", stage, int(self.remaining() * 1000))


class DeadlineRunner:
    """Start the clock before job claim/context/proxy/browser work, exactly once."""

    def __init__(self, runner, deadline: CheckDeadline):
        self.runner, self.deadline = runner, deadline

    async def run_once(self, worker_id):
        self.deadline.start()
        return await self.runner.run_once(worker_id)
