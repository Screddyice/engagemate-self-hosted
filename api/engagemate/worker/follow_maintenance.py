"""Durable follow sequencing, independent of the next Story campaign."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from engagemate.domain.models import JobStatus
from engagemate.worker.runner import RunSummary


def _checked_result(outcome, evidence, attempted):
    if outcome not in {
        "confirmed", "already_following", "already_absent", "uncertain", "deferred",
    } or type(evidence) is not dict:
        raise ValueError("invalid relationship result")
    if outcome == "confirmed" and not attempted:
        return "uncertain", {"reason": "unarmed_confirmation"}
    if outcome == "deferred" and attempted:
        return "uncertain", {"reason": "attempt_not_confirmed"}
    return outcome, evidence


@dataclass(frozen=True, slots=True)
class InterruptedFollowResult:
    """Retain a result without changing asyncio's cancellation exception type."""

    outcome: str
    evidence: dict

    @staticmethod
    def from_error(error):
        if isinstance(error, TimeoutError):
            error = error.__cause__
        if not isinstance(error, asyncio.CancelledError):
            return None
        retained = getattr(error, "_engagemate_follow_result", None)
        return retained if type(retained) is InterruptedFollowResult else None

    def checked(self, attempted):
        try:
            return _checked_result(self.outcome, self.evidence, attempted)
        except (TypeError, ValueError):
            return None


class FollowMaintenance:
    """Execute one leased relationship action without creating billable usage."""

    def __init__(self, store: Any, performer: Any, *, execution_mode: str,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.store, self.performer = store, performer
        self.execution_mode = execution_mode
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self, worker_id: str) -> RunSummary | None:
        # A Story canary approval does not authorize a relationship mutation.
        if self.execution_mode != "autonomous":
            return None
        await self.store.enqueue_confirmed(self.clock())
        claim = await self.store.claim_next(worker_id, self.clock())
        if claim is None:
            return None
        finished, outcome = await self.execute_claim(claim)
        successful = finished and outcome == "confirmed"
        skipped = finished and outcome in {"already_following", "already_absent", "deferred"}
        # This result is used only by the process log. It never enters the
        # Story summary repository or engagement/billing tables.
        return RunSummary(
            job_id=claim.id, user_id=claim.user_id, ig_account_id=claim.ig_account_id,
            status=JobStatus.COMPLETED if successful or skipped else JobStatus.FAILED,
            considered=1, engaged=int(successful), skipped=int(skipped), gated=0,
            failed=int(not (successful or skipped)),
            stop_reason=f"{claim.action}_{outcome if finished else 'lease_lost'}",
        )

    async def execute_claim(self, claim) -> tuple[bool, str]:
        """Execute an already reserved claim and retain its first receipt.

        Story reply orchestration uses this same cancellation and receipt
        handling while holding the current Story browser context.
        """
        attempted = False
        observed_result = None
        finish_task = None

        async def before_mutation() -> bool:
            nonlocal attempted
            if attempted:
                return False
            permitted = await self.store.mark_attempting(claim, self.clock())
            if permitted is True:
                attempted = True
            return permitted is True

        async def on_result(outcome, evidence):
            nonlocal observed_result, finish_task
            if finish_task is not None:
                return False
            outcome, evidence = _checked_result(outcome, evidence, attempted)
            observed_result = outcome, dict(evidence)
            finish_task = asyncio.create_task(self.store.finish(
                claim, outcome, self.clock(), observed_result[1],
            ))
            async with asyncio.timeout(5):
                return await asyncio.shield(finish_task)

        try:
            async with asyncio.timeout(105):
                observed_perform = getattr(self.performer, "perform_with_result", None)
                if callable(observed_perform):
                    outcome, evidence = await observed_perform(claim, before_mutation, on_result)
                else:
                    outcome, evidence = await self.performer(claim, before_mutation)
                outcome, evidence = _checked_result(outcome, evidence, attempted)
        except asyncio.CancelledError as error:
            outcome, evidence = "uncertain" if attempted else "deferred", {
                "reason": "worker_cancelled", "attempted": attempted,
            }
            retained = InterruptedFollowResult.from_error(error)
            known = retained.checked(attempted) if retained is not None else None
            if known is not None:
                outcome, evidence = known
                evidence = {**evidence, "worker_cancelled": True}
            elif retained is not None:
                evidence["invalid_retained_result"] = True
            cleanup = finish_task if finish_task is not None else asyncio.create_task(
                self.store.finish(claim, outcome, self.clock(), evidence))
            try:
                async with asyncio.timeout(5):
                    await asyncio.shield(cleanup)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001, S110 - preserve cancellation
                pass  # Preserve cancellation; an expired lease becomes uncertain.
            finally:
                if not cleanup.done():
                    cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except Exception as error:  # noqa: BLE001 - never retry an uncertain external write
            outcome = "uncertain" if attempted else "deferred"
            evidence = {"reason": "relationship_execution_error", "attempted": attempted}
            retained = InterruptedFollowResult.from_error(error)
            known = retained.checked(attempted) if retained is not None else None
            if known is not None:
                outcome, evidence = known
                evidence = {**evidence, "worker_timeout": True}
            elif retained is not None:
                evidence["invalid_retained_result"] = True

        if finish_task is not None:
            # The first finish retains its receipt; cleanup flags stay in the
            # returned diagnostic result and cannot rewrite the ledger.
            outcome, evidence = observed_result
            async with asyncio.timeout(5):
                finished = await asyncio.shield(finish_task)
        else:
            finished = await self.store.finish(claim, outcome, self.clock(), evidence)
        return finished, outcome


class SequencedWorker:
    """Round-robin maintenance and Story jobs without starving a busy lane."""

    def __init__(self, stories: Any, relationships: FollowMaintenance, reconciliation: Any = None,
                 reel_messages: Any = None) -> None:
        self.stories, self.relationships = stories, relationships
        self._lanes = [relationships, stories]
        if reconciliation is not None:
            self._lanes.append(reconciliation)
        if reel_messages is not None:
            self._lanes.append(reel_messages)
        self._next = 0

    async def run_once(self, worker_id: str) -> RunSummary | None:
        for offset in range(len(self._lanes)):
            index = (self._next + offset) % len(self._lanes)
            result = await self._lanes[index].run_once(worker_id)
            if result is not None:
                self._next = (index + 1) % len(self._lanes)
                return result
        self._next = (self._next + 1) % len(self._lanes)
        return None
