"""One-shot command for managed-account growth snapshot collection."""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

import httpx

from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.growth.collector import GrowthCollector
from engagemate.growth.repository import GrowthSnapshotRepository
from engagemate.onboarding.hypercrawl import HyperCrawlInstagramClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandOptions:
    dry_run: bool
    user_id: UUID | None


def parse_args(argv: list[str] | None = None) -> CommandOptions:
    parser = argparse.ArgumentParser(
        description="Collect one public growth snapshot per due managed account."
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-id", type=UUID)
    parsed = parser.parse_args(argv)
    return CommandOptions(dry_run=parsed.dry_run, user_id=parsed.user_id)


async def execute(options: CommandOptions) -> None:
    """Build the production boundaries and run one bounded batch."""

    settings = Settings()
    if not settings.hypercrawl_url:
        raise ValueError("HYPERCRAWL_URL is required for growth collection")

    pool = create_pool(
        settings.database_url,
        min_size=1,
        max_size=max(2, settings.growth_snapshot_concurrency + 1),
    )
    try:
        async with httpx.AsyncClient() as http:
            hypercrawl = HyperCrawlInstagramClient(
                http=http,
                base_url=settings.hypercrawl_url,
                rest_token=settings.hypercrawl_rest_token,
                timeout_seconds=settings.hypercrawl_timeout_seconds,
            )
            collector = GrowthCollector(
                GrowthSnapshotRepository(pool),
                hypercrawl,
                batch_size=settings.growth_snapshot_batch_size,
                concurrency=settings.growth_snapshot_concurrency,
            )
            result = await collector.run(
                datetime.now(timezone.utc),
                dry_run=options.dry_run,
                user_id=options.user_id,
            )
            logger.info(
                "growth batch finished due=%d succeeded=%d failed=%d dry_run=%s",
                len(result.due_account_ids),
                result.succeeded,
                result.failed,
                options.dry_run,
            )
    finally:
        pool.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    options = parse_args(argv)
    try:
        asyncio.run(execute(options))
    except Exception:  # noqa: BLE001 - command logs only a stable process code
        logger.error("growth batch failed code=batch_failed")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
