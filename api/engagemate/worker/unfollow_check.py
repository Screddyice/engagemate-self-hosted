"""Run one exact due unfollow with saved policy, leases and native receipts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from uuid import UUID

from engagemate.domain.models import JobStatus
from engagemate.worker.canary import CanaryError, CanaryScope
from engagemate.worker.follow_store import UnfollowStoreScope


class _LazyFollowPerformer:
    """Start Playwright only after maintenance claims the exact eligible row."""

    def __init__(self, pool, settings):
        self.pool, self.settings = pool, settings

    async def perform_with_result(self, claim, before_mutation, on_result):
        from playwright.async_api import async_playwright
        from engagemate.worker.follow_runtime import LiveFollowPerformer

        interruption = None
        try:
            async with async_playwright() as playwright:
                try:
                    return await LiveFollowPerformer(self.pool, self.settings, playwright).perform_with_result(
                        claim, before_mutation, on_result,
                    )
                except asyncio.CancelledError as error:
                    interruption = error
                    raise
        except asyncio.CancelledError as error:
            interruption = interruption if interruption is not None else error
            # Only on_result owns the durable native receipt. Driver startup
            # or cleanup exceptions cannot contribute a confirmation carrier.
            interruption.__dict__.pop("_engagemate_follow_result", None)
            raise interruption from None
        except Exception:
            if interruption is not None:
                interruption.__dict__.pop("_engagemate_follow_result", None)
                raise interruption from None
        # Raise outside the handler so a driver/body exception, its cause,
        # context or group cannot supply a foreign native-result carrier.
        raise RuntimeError("unfollow_browser_execution_error") from None


async def run_unfollow(scope: UnfollowStoreScope, *, worker_id: str):
    from deploy.healthcheck import validate_worker_environment
    from engagemate.config import Settings
    from engagemate.db.pool import create_pool
    from engagemate.worker.follow_maintenance import FollowMaintenance
    from engagemate.worker.follow_store import PostgresFollowStore

    if type(scope) is not UnfollowStoreScope:
        raise CanaryError("invalid_unfollow_scope")
    CanaryScope(scope.user_id, scope.account_id, scope.relationship_follow_id, worker_id)
    validate_worker_environment(os.environ)
    settings = Settings()
    if settings.instagram_execution_mode != "autonomous":
        raise CanaryError("unfollow_check_requires_saved_autonomous_mode")
    pool = create_pool(settings.database_url, min_size=1, max_size=4)
    try:
        result = await FollowMaintenance(
            PostgresFollowStore(pool, scope=scope), _LazyFollowPerformer(pool, settings),
            execution_mode=settings.instagram_execution_mode,
        ).run_once(worker_id)
        if result is None:
            raise CanaryError("eligible_unattempted_due_unfollow_required")
        return result
    finally:
        pool.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, type=UUID)
    parser.add_argument("--account-id", required=True, type=UUID)
    parser.add_argument("--relationship-id", required=True, type=UUID)
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(run_unfollow(UnfollowStoreScope(
            args.user_id, args.account_id, args.relationship_id,
        ), worker_id=args.worker_id))
    except CanaryError as error:
        print(f"unfollow check refused: {error}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - provider and driver errors may contain credentials
        print("unfollow check failed; inspect the exact relationship ledger", file=sys.stderr)
        return 2
    print(json.dumps(asdict(summary), default=str, sort_keys=True))
    return 0 if (summary.status is JobStatus.COMPLETED and summary.engaged == 1
                 and summary.stop_reason == "unfollow_confirmed") else 3


if __name__ == "__main__":
    raise SystemExit(main())
