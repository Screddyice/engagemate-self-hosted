"""Execute a prepared, claim-bound post-follow message."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from engagemate.domain.models import JobStatus
from engagemate.worker.runner import RunSummary


@dataclass(frozen=True, slots=True)
class InterruptedReelResult:
    """A transport result retained on the original cancellation exception."""

    outcome: str
    evidence: dict

    @staticmethod
    def from_error(error):
        # asyncio.timeout retains the original cancellation as its cause. Keep
        # CancelledError's type intact for Python 3.12's timeout conversion.
        if isinstance(error, TimeoutError):
            error = error.__cause__
        if not isinstance(error, asyncio.CancelledError):
            return None
        retained = getattr(error, "_engagemate_reel_result", None)
        return retained if type(retained) is InterruptedReelResult else None

    def checked(self, attempted):
        try:
            return _checked_result(self.outcome, self.evidence, attempted)
        except (TypeError, ValueError):
            return None


def _checked_result(outcome, evidence, attempted):
    if outcome not in {"sent", "uncertain", "deferred"} or type(evidence) is not dict:
        raise ValueError("invalid reel message result")
    if outcome == "sent" and not attempted:
        return "uncertain", {**evidence, "reason": "unarmed_send"}
    if outcome == "deferred" and attempted:
        return "uncertain", {**evidence, "reason": "attempt_not_confirmed"}
    return outcome, evidence


class ReelMessageMaintenance:
    def __init__(self, store, performer, *, execution_mode: str, clock=None):
        self.store, self.performer, self.execution_mode = store, performer, execution_mode
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self, worker_id: str) -> RunSummary | None:
        if self.execution_mode != "autonomous":
            return None
        claim = await self.store.claim_next(worker_id, self.clock())
        if claim is None:
            return None
        attempted = False
        observed_result = None
        finish_task = None

        async def before_mutation() -> bool:
            nonlocal attempted
            if attempted:
                return False
            allowed = await self.store.mark_attempting(claim, self.clock())
            attempted = allowed is True
            return attempted

        async def prepare(reel_url: str, body: str) -> bool:
            if attempted:
                return False
            return await self.store.prepare(claim, reel_url, body, self.clock()) is True

        async def on_result(outcome, evidence):
            nonlocal observed_result, finish_task
            if finish_task is not None:
                return False
            outcome, evidence = _checked_result(outcome, evidence, attempted)
            observed_result = outcome, dict(evidence)
            finish_task = asyncio.create_task(self.store.finish(
                claim, outcome, observed_result[1], self.clock(),
            ))
            async with asyncio.timeout(5):
                return await asyncio.shield(finish_task)

        try:
            async with asyncio.timeout(150):
                observed_perform = getattr(self.performer, "perform_with_result", None)
                if callable(observed_perform):
                    outcome, evidence = await observed_perform(claim, prepare, before_mutation, on_result)
                else:
                    outcome, evidence = await self.performer(claim, prepare, before_mutation)
                outcome, evidence = _checked_result(outcome, evidence, attempted)
        except asyncio.CancelledError as error:
            retained = InterruptedReelResult.from_error(error)
            outcome, evidence = "uncertain" if attempted else "deferred", {
                "reason": "worker_cancelled", "attempted": attempted,
            }
            known = retained.checked(attempted) if retained is not None else None
            if known is not None:
                outcome, evidence = known
                evidence = {**evidence, "worker_cancelled": True}
            elif retained is not None:
                evidence["invalid_retained_result"] = True
            cleanup = finish_task if finish_task is not None else asyncio.create_task(
                self.store.finish(claim, outcome, evidence, self.clock()))
            try:
                async with asyncio.timeout(5):
                    await asyncio.shield(cleanup)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001
                pass
            finally:
                if not cleanup.done():
                    cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except Exception as error:  # noqa: BLE001 - no retry after a possible external write
            outcome = "uncertain" if attempted else "deferred"
            evidence = {"reason": "reel_message_execution_error", "attempted": attempted}
            retained = InterruptedReelResult.from_error(error)
            known = retained.checked(attempted) if retained is not None else None
            if known is not None:
                outcome, evidence = known
                evidence = {**evidence, "worker_timeout": True}
            elif retained is not None:
                evidence["invalid_retained_result"] = True

        if finish_task is not None:
            # The first finish owns persistence. Later cleanup flags only
            # describe the return value and cannot replace a durable receipt.
            outcome, evidence = observed_result
            async with asyncio.timeout(5):
                finished = await asyncio.shield(finish_task)
        else:
            finished = await self.store.finish(claim, outcome, evidence, self.clock())
        sent = finished and outcome == "sent"
        deferred = finished and outcome == "deferred"
        return RunSummary(
            job_id=claim.id, user_id=claim.user_id, ig_account_id=claim.ig_account_id,
            status=JobStatus.COMPLETED if sent or deferred else JobStatus.FAILED,
            considered=1, engaged=int(sent), skipped=int(deferred), gated=0,
            failed=int(not (sent or deferred)),
            stop_reason=f"follow_reel_{outcome if finished else 'lease_lost'}",
        )
