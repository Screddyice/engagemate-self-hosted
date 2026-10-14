"""One-shot command for weekly growth report delivery."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from functools import partial
from datetime import datetime, timezone

import httpx

from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.reports.dispatcher import (
    CanaryReportRepository,
    DispatchResult,
    ReportDispatcher,
    validate_canary_configuration,
)
from engagemate.reports.repository import ReportRepository
from engagemate.reports.resend import ResendClient
from engagemate.reports.composio import ComposioEmailClient
from engagemate.reports.unsubscribe import unsubscribe_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CommandOptions:
    dry_run: bool
    canary: bool


def parse_args(argv: list[str] | None = None) -> CommandOptions:
    parser = argparse.ArgumentParser(
        description="Build and deliver a bounded batch of weekly growth reports."
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--canary", action="store_true")
    parsed = parser.parse_args(argv)
    return CommandOptions(dry_run=parsed.dry_run, canary=parsed.canary)


def execute(options: CommandOptions) -> DispatchResult:
    """Build runtime boundaries and dispatch one bounded report batch."""

    settings = Settings()
    http: httpx.Client | None = None
    client: ResendClient | None = None
    pool = None
    try:
        if not options.dry_run:
            if settings.resend_api_key is None and getattr(settings, "composio_api_key", None) is None:
                raise ValueError("RESEND_API_KEY is required")
            if settings.weekly_report_from is None:
                raise ValueError("WEEKLY_REPORT_FROM is required")
            if options.canary:
                validate_canary_configuration(
                    settings.weekly_report_canary_user_id,
                    settings.weekly_report_canary_recipient,
                )
            http = httpx.Client()
            if not getattr(settings, "composio_email_account_id", None):
                client = ResendClient(api_key=settings.resend_api_key.get_secret_value(),
                    sender=settings.weekly_report_from, http=http)
        pool = create_pool(settings.database_url, min_size=1, max_size=2)
        if not options.dry_run and getattr(settings, "composio_email_account_id", None):
            client = ComposioEmailClient(pool=pool, http=http,
                api_key=settings.composio_api_key.get_secret_value(),
                account_id=settings.composio_email_account_id, sender=settings.weekly_report_from,
                entity_id=settings.composio_email_entity_id)

        from engagemate.reports.recipients import queue_weekly_copies
        repository = ReportRepository(pool)
        dispatcher = ReportDispatcher(
            repository,
            client,
            batch_size=settings.weekly_report_batch_size,
            copy_notifications=partial(queue_weekly_copies, pool),
            canary_user_id=settings.weekly_report_canary_user_id,
            canary_recipient=settings.weekly_report_canary_recipient,
            canary_repository=CanaryReportRepository(pool, repository),
            unsubscribe_url=partial(unsubscribe_url, settings.internal_api_key.get_secret_value(), category="weekly_report"),
        )
        return dispatcher.run(
            datetime.now(timezone.utc),
            dry_run=options.dry_run,
            canary=options.canary,
        )
    finally:
        if http is not None:
            http.close()
        if pool is not None:
            pool.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    options = parse_args(argv)
    try:
        result = execute(options)
    except Exception:  # noqa: BLE001 - command logs only a stable process code
        logger.error("weekly_report_batch status=failed error_code=batch_failed")
        return 1
    error_code = _result_error_code(options, result)
    if error_code is not None:
        logger.error(
            "weekly_report_batch status=failed error_code=%s",
            error_code,
        )
        return 1
    logger.info("weekly_report_batch status=complete")
    return 0


def _result_error_code(
    options: CommandOptions,
    result: DispatchResult,
) -> str | None:
    if options.canary:
        if result.due != 1:
            return "canary_user_not_due"
        if (
            result.built != 1
            or result.claimed != 1
            or result.sent != 1
            or result.failed != 0
        ):
            return "canary_delivery_failed"
        return None
    if not options.dry_run and result.due > 0 and result.sent == 0:
        return "batch_delivery_failed"
    return None


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
