"""Failure-isolated weekly report delivery orchestration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from psycopg_pool import ConnectionPool

from engagemate.reports.aggregate import previous_week
from engagemate.reports.models import ManagerWeeklyReport
from engagemate.reports.render import RenderedEmail, render_weekly_report
from engagemate.reports.repository import DeliveryClaim, DueManager, ReportRepository
from engagemate.reports.resend import (
    ResendClient,
    ResendError,
    request_payload_hash,
    validate_recipient,
)

logger = logging.getLogger(__name__)

_CANARY_DUE_SQL = """
SELECT preference.user_id, preference.timezone
FROM weekly_report_preferences AS preference
JOIN pg_timezone_names AS timezone_name
  ON timezone_name.name = preference.timezone
WHERE preference.user_id = %(user_id)s
  AND preference.enabled = true
  AND EXISTS (
      SELECT 1
      FROM ig_accounts AS account
      WHERE account.user_id = preference.user_id
  )
  AND %(now)s::timestamptz AT TIME ZONE preference.timezone
      >= date_trunc(
             'week',
             %(now)s::timestamptz AT TIME ZONE preference.timezone
         ) + make_interval(hours => preference.send_hour)
"""

_CANARY_INSERT_SQL = """
INSERT INTO weekly_report_deliveries (
    user_id,
    week_start,
    status,
    attempts,
    recipient_email,
    created_at,
    updated_at
)
VALUES (
    %(user_id)s,
    %(week_start)s,
    'pending',
    0,
    %(recipient_email)s,
    %(now)s,
    %(now)s
)
ON CONFLICT (user_id, week_start) DO NOTHING
"""


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """Aggregate outcomes from one bounded dispatcher invocation."""

    due: int
    built: int
    claimed: int
    sent: int
    failed: int


def _checked_now(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("now must be a timezone-aware datetime") from exc
    return value


def validate_canary_configuration(
    user_id: UUID | None,
    recipient: str | None,
) -> tuple[UUID, str]:
    """Require one user and one recipient before canary processing starts."""

    if type(user_id) is not UUID or recipient is None:
        raise ValueError("canary configuration requires one user and one recipient")
    try:
        validated_recipient = validate_recipient(recipient)
    except ValueError:
        raise ValueError(
            "canary configuration requires one user and one recipient"
        ) from None
    return user_id, validated_recipient


class CanaryReportRepository:
    """Query and claim one canary without replacing a captured recipient."""

    def __init__(
        self,
        pool: ConnectionPool,
        repository: ReportRepository,
    ) -> None:
        self._pool = pool
        self._repository = repository

    def find_due_manager(
        self,
        now: datetime,
        user_id: UUID,
        recipient: str,
    ) -> DueManager | None:
        """Return the configured user only when its local send time is due."""

        checked_at = _checked_now(now)
        if type(user_id) is not UUID:
            raise ValueError("canary user must be a UUID")
        captured_recipient = validate_recipient(recipient)
        self._repository.ensure_missing_preferences()
        with self._pool.connection() as connection:
            row = connection.execute(
                _CANARY_DUE_SQL,
                {"now": checked_at, "user_id": user_id},
            ).fetchone()
        if row is None:
            return None
        timezone_name = row["timezone"]
        if isinstance(timezone_name, bytes):
            timezone_name = timezone_name.decode("utf-8")
        return DueManager(
            user_id=row["user_id"],
            recipient_email=captured_recipient,
            timezone=timezone_name,
            period=previous_week(checked_at, timezone_name),
        )

    def claim(
        self,
        user_id: UUID,
        week_start: date,
        recipient: str,
        now: datetime,
    ) -> DeliveryClaim | None:
        """Claim only a row already bound to the configured canary mailbox."""

        if type(user_id) is not UUID:
            raise ValueError("canary user must be a UUID")
        if type(week_start) is not date or week_start.weekday() != 0:
            raise ValueError("week_start must be a Monday date")
        captured_recipient = validate_recipient(recipient)
        checked_at = _checked_now(now)
        params = {
            "user_id": user_id,
            "week_start": week_start,
            "recipient_email": captured_recipient,
            "now": checked_at,
        }
        with self._pool.connection() as connection:
            connection.execute(_CANARY_INSERT_SQL, params)
            row = connection.execute(
                """
                SELECT recipient_email
                FROM weekly_report_deliveries
                WHERE user_id = %(user_id)s
                  AND week_start = %(week_start)s
                FOR UPDATE
                """,
                params,
            ).fetchone()
            if row is None:
                return None
            stored_recipient = row["recipient_email"]
            if isinstance(stored_recipient, bytes):
                stored_recipient = stored_recipient.decode("utf-8")
            if stored_recipient != captured_recipient:
                return None
            return self._repository._claim_with_connection(
                connection,
                user_id,
                week_start,
                captured_recipient,
                checked_at,
            )


class ReportDispatcher:
    """Coordinate report claims, locked payloads, and provider finalization."""

    def __init__(
        self,
        repository: ReportRepository,
        client: ResendClient | None,
        *,
        batch_size: int,
        canary_user_id: UUID | None = None,
        canary_recipient: str | None = None,
        canary_repository: CanaryReportRepository | None = None,
        renderer: Callable[[ManagerWeeklyReport], RenderedEmail] = render_weekly_report,
        unsubscribe_url: Callable[[UUID], str] | None = None,
        copy_notifications: Callable | None = None,
    ) -> None:
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError("batch_size must be between 1 and 100")
        self._repository = repository
        self._client = client
        self._batch_size = batch_size
        self._canary_user_id = canary_user_id
        self._canary_recipient = canary_recipient
        self._canary_repository = canary_repository
        self._renderer = renderer
        self._unsubscribe_url = unsubscribe_url
        self._copy_notifications = copy_notifications

    def _render(self, report: ManagerWeeklyReport, user_id: UUID) -> RenderedEmail:
        if self._unsubscribe_url is None:
            return self._renderer(report)
        return self._renderer(report, unsubscribe_url=self._unsubscribe_url(user_id))

    def run(
        self,
        now: datetime,
        dry_run: bool = False,
        canary: bool = False,
    ) -> DispatchResult:
        """Process a bounded due set while isolating each manager's failure."""

        checked_at = _checked_now(now)
        if type(dry_run) is not bool or type(canary) is not bool:
            raise ValueError("dispatch modes must be booleans")
        if dry_run and canary:
            raise ValueError("dry run and canary modes are mutually exclusive")
        if not dry_run and self._client is None:
            raise ValueError("Resend client is required outside dry run")

        canary_values: tuple[UUID, str] | None = None
        if canary:
            canary_values = validate_canary_configuration(
                self._canary_user_id,
                self._canary_recipient,
            )

        if canary_values is None:
            selected = self._repository.list_due_managers(
                checked_at,
                self._batch_size,
            )
        else:
            if self._canary_repository is None:
                raise ValueError("canary repository is required")
            canary_user_id, canary_recipient = canary_values
            due_manager = self._canary_repository.find_due_manager(
                checked_at,
                canary_user_id,
                canary_recipient,
            )
            if due_manager is not None and (
                due_manager.user_id != canary_user_id
                or due_manager.recipient_email != canary_recipient
            ):
                raise ValueError("canary lookup mismatch")
            selected = [] if due_manager is None else [due_manager]
        built = claimed = sent = failed = 0

        for manager in selected:
            try:
                report = self._repository.build_report(
                    manager.user_id,
                    manager.period,
                )
                built += 1
            except Exception:  # noqa: BLE001 - no report data reaches diagnostics
                failed += 1
                self._log_outcome(manager, None, "failed", "report_build_failed")
                continue

            if dry_run:
                try:
                    self._render(report, manager.user_id)
                except Exception:  # noqa: BLE001 - no rendered data reaches diagnostics
                    failed += 1
                    self._log_outcome(manager, None, "failed", "render_failed")
                else:
                    self._log_outcome(manager, None, "dry_run_validated", "none")
                continue

            recipient = (
                canary_values[1]
                if canary_values is not None
                else manager.recipient_email
            )
            try:
                if canary_values is None:
                    claim = self._repository.claim(
                        manager.user_id,
                        manager.period.week_start,
                        recipient,
                        checked_at,
                    )
                else:
                    canary_repository = self._canary_repository
                    if canary_repository is None:
                        raise RuntimeError("canary repository unavailable")
                    claim = canary_repository.claim(
                        manager.user_id,
                        manager.period.week_start,
                        recipient,
                        checked_at,
                    )
            except Exception:  # noqa: BLE001 - repository errors remain data-free
                failed += 1
                self._log_outcome(manager, None, "failed", "claim_failed")
                continue
            if claim is None:
                failed += 1
                error_code = (
                    "canary_claim_unavailable"
                    if canary_values is not None
                    else "claim_unavailable"
                )
                self._log_outcome(manager, None, "failed", error_code)
                continue
            claimed += 1

            try:
                rendered = self._render(report, manager.user_id)
                client = self._client
                if client is None:  # guarded above; keeps type narrowing explicit
                    raise RuntimeError("provider client unavailable")
                payload_hash = request_payload_hash(
                    client.sender,
                    claim.recipient_email,
                    rendered,
                )
            except Exception:  # noqa: BLE001 - rendering failures use a stable code
                self._finalize_failure(
                    claim,
                    "payload_invalid",
                    False,
                    checked_at,
                )
                failed += 1
                self._log_outcome(manager, claim, "failed", "payload_invalid")
                continue

            try:
                locked = self._repository.lock_payload(
                    claim.delivery_id,
                    claim.lease_token,
                    payload_hash,
                    checked_at,
                )
            except Exception:  # noqa: BLE001 - repository errors remain data-free
                failed += 1
                self._log_outcome(manager, claim, "failed", "payload_lock_failed")
                continue
            if not locked:
                failed += 1
                self._log_outcome(manager, claim, "failed", "payload_hash_rejected")
                continue

            if canary_values is None and self._copy_notifications is not None:
                try:
                    self._copy_notifications(manager.user_id, claim.delivery_id, claim.recipient_email, rendered)
                except Exception:
                    self._finalize_failure(claim, "notification_queue_failed", True, checked_at)
                    failed += 1
                    self._log_outcome(manager, claim, "failed", "notification_queue_failed")
                    continue

            key = f"engagemate-weekly/{claim.delivery_id}"
            try:
                provider_id = client.send(
                    claim.recipient_email,
                    rendered,
                    key,
                )
            except ResendError as exc:
                self._finalize_failure(
                    claim,
                    exc.code,
                    exc.retryable,
                    checked_at,
                )
                failed += 1
                self._log_outcome(manager, claim, "failed", exc.code)
                continue
            except Exception:  # noqa: BLE001 - unexpected text is discarded
                self._finalize_failure(
                    claim,
                    "provider_client_error",
                    True,
                    checked_at,
                )
                failed += 1
                self._log_outcome(manager, claim, "failed", "provider_client_error")
                continue

            try:
                finalized = self._repository.mark_sent(
                    claim.delivery_id,
                    claim.lease_token,
                    provider_id,
                    checked_at,
                )
            except Exception:  # noqa: BLE001 - provider response is never logged
                finalized = False
            if finalized:
                sent += 1
                self._log_outcome(manager, claim, "sent", "none")
            else:
                failed += 1
                self._log_outcome(manager, claim, "failed", "sent_finalize_failed")

        return DispatchResult(
            due=len(selected),
            built=built,
            claimed=claimed,
            sent=sent,
            failed=failed,
        )

    def _finalize_failure(
        self,
        claim: DeliveryClaim,
        error_code: str,
        retryable: bool,
        now: datetime,
    ) -> None:
        try:
            self._repository.mark_failed(
                claim.delivery_id,
                claim.lease_token,
                error_code,
                retryable,
                now,
            )
        except Exception:  # noqa: BLE001 - preserve per-manager isolation
            return

    @staticmethod
    def _log_outcome(
        manager: DueManager,
        claim: DeliveryClaim | None,
        status: str,
        error_code: str,
    ) -> None:
        log = logger.info if status in {"sent", "dry_run_validated"} else logger.warning
        log(
            "weekly_report_delivery delivery_id=%s user_id=%s week_start=%s "
            "attempt=%d status=%s error_code=%s",
            "none" if claim is None else claim.delivery_id,
            manager.user_id,
            manager.period.week_start,
            0 if claim is None else claim.attempt_number,
            status,
            error_code,
        )
