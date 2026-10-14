"""Refresh one due outcome per fair worker turn, without social actions."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from engagemate.domain.models import JobStatus
from engagemate.worker.runner import RunSummary


class RelationshipReconciliation:
    def __init__(self, store, reader, *, execution_mode, clock=None):
        self.store, self.reader, self.execution_mode = store, reader, execution_mode
        self.clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self, worker_id):
        if self.execution_mode != "autonomous":
            return None
        claim = await self.store.claim_next(worker_id, self.clock())
        if claim is None:
            return None

        async def permitted():
            return await self.store.permitted(claim, self.clock())

        value, reason = None, "unknown"
        try:
            async with asyncio.timeout(90):
                value, reason = await self.reader(claim, permitted)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(
                self.store.finish(claim, self.clock(), reason="worker_cancelled")
            )
            try:
                async with asyncio.timeout(5):
                    await asyncio.shield(cleanup)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001, S110 - preserve cancellation and expire the lease
                pass
            finally:
                if not cleanup.done():
                    cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)
            raise
        except TimeoutError:
            reason = "timeout"
        except Exception:  # noqa: BLE001 - provider failures cannot expose secrets or stop the worker
            # Never persist provider responses, credentials, or exception text.
            reason = "unknown"
        finished = await self.store.finish(claim, self.clock(), value, reason)
        return RunSummary(
            job_id=claim.id,
            user_id=claim.user_id,
            ig_account_id=claim.ig_account_id,
            status=JobStatus.COMPLETED if finished else JobStatus.FAILED,
            considered=1,
            engaged=0,
            skipped=1 if finished else 0,
            gated=0,
            failed=0 if finished else 1,
            stop_reason="relationship_check_finished"
            if finished
            else "reconciliation_lease_lost",
        )
