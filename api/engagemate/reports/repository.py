"""Tenant-scoped weekly aggregates and retry-safe report delivery claims."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4

from psycopg_pool import ConnectionPool

from engagemate.reports.aggregate import previous_week
from engagemate.reports.models import (
    AccountWeeklyReport,
    ManagerWeeklyReport,
    ReportingPeriod,
)

_LEASE_DURATION = timedelta(minutes=15)
_RETRY_HORIZON = timedelta(hours=23)
_MAX_ATTEMPTS = 5
_PAYLOAD_HASH = re.compile(r"[0-9a-f]{64}")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_RECIPIENT_LENGTH = 320
_MAX_PROVIDER_MESSAGE_ID_LENGTH = 255

_ENSURE_PREFERENCES_SQL = """
INSERT INTO weekly_report_preferences (
    user_id,
    enabled,
    timezone,
    send_hour,
    created_at,
    updated_at
)
SELECT
    manager.id,
    true,
    COALESCE(
        (
            SELECT config.timezone
            FROM ig_accounts AS account
            JOIN agent_configs AS config
              ON config.ig_account_id = account.id
             AND config.user_id = manager.id
            WHERE account.user_id = manager.id
            ORDER BY account.created_at, account.id
            LIMIT 1
        ),
        'UTC'
    ),
    9,
    now(),
    now()
FROM users AS manager
WHERE EXISTS (
    SELECT 1
    FROM ig_accounts AS account
    WHERE account.user_id = manager.id
)
ON CONFLICT (user_id) DO NOTHING
"""

_EXPIRE_UNCLAIMABLE_SQL = """
UPDATE weekly_report_deliveries
SET status = 'failed',
    last_error_code = CASE
        WHEN created_at <= %(retry_cutoff)s
        THEN 'idempotency_window_expired'
        ELSE 'attempt_limit_reached'
    END,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = %(now)s
WHERE status IN ('pending', 'sending')
  AND (
      status = 'pending'
      OR lease_expires_at <= %(now)s
  )
  AND (
      attempts >= %(max_attempts)s
      OR created_at <= %(retry_cutoff)s
  )
"""

_DUE_MANAGERS_SQL = """
WITH candidates AS (
    SELECT
        preference.user_id,
        manager.email AS recipient_email,
        preference.timezone,
        (
            date_trunc(
                'week',
                %(now)s::timestamptz AT TIME ZONE preference.timezone
            )::date - 7
        ) AS week_start
    FROM weekly_report_preferences AS preference
    JOIN users AS manager
      ON manager.id = preference.user_id
    JOIN pg_timezone_names AS timezone_name
      ON timezone_name.name = preference.timezone
    WHERE preference.enabled = true
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
)
SELECT
    candidate.user_id,
    candidate.recipient_email,
    candidate.timezone,
    candidate.week_start
FROM candidates AS candidate
LEFT JOIN weekly_report_deliveries AS delivery
  ON delivery.user_id = candidate.user_id
 AND delivery.week_start = candidate.week_start
WHERE delivery.id IS NULL
   OR (
       delivery.status = 'pending'
       AND delivery.attempts < %(max_attempts)s
       AND delivery.created_at > %(retry_cutoff)s
   )
   OR (
       delivery.status = 'sending'
       AND delivery.lease_expires_at <= %(now)s
       AND delivery.attempts < %(max_attempts)s
       AND delivery.created_at > %(retry_cutoff)s
   )
ORDER BY candidate.user_id
LIMIT %(limit)s
"""

_REPORT_SQL = """
WITH managed_accounts AS (
    SELECT account.id, account.user_id, account.username, account.created_at
    FROM ig_accounts AS account
    WHERE account.user_id = %(user_id)s
), run_activity AS (
    SELECT
        summary.user_id,
        summary.ig_account_id,
        COALESCE(SUM(summary.opportunities_found), 0)::bigint
            AS opportunities_reviewed,
        COALESCE(SUM(summary.skipped), 0)::bigint AS skipped,
        COALESCE(SUM(summary.gated), 0)::bigint AS gated,
        COALESCE(SUM(summary.failed), 0)::bigint AS failed
    FROM run_summaries AS summary
    WHERE summary.user_id = %(user_id)s
      AND summary.completed_at >= %(start_utc)s
      AND summary.completed_at < %(end_utc)s
    GROUP BY summary.user_id, summary.ig_account_id
), sent_activity AS (
    SELECT
        engagement.user_id,
        engagement.ig_account_id,
        COUNT(*) FILTER (WHERE engagement.action = 'like')::bigint AS likes_sent,
        COUNT(*) FILTER (WHERE engagement.action = 'reply')::bigint AS replies_sent,
        COUNT(*) FILTER (WHERE engagement.action = 'dm')::bigint AS dms_sent,
        COUNT(*) FILTER (WHERE engagement.action = 'view')::bigint AS views_sent
    FROM story_engagements AS engagement
    WHERE engagement.user_id = %(user_id)s
      AND engagement.status = 'sent'
      AND engagement.executed_at >= %(start_utc)s
      AND engagement.executed_at < %(end_utc)s
    GROUP BY engagement.user_id, engagement.ig_account_id
)
SELECT
    account.id AS ig_account_id,
    account.username,
    (SELECT count(*) FROM relationship_follows f WHERE f.ig_account_id=account.id
      AND f.evidence #>> '{follow,outcome}'='confirmed'
      AND f.user_id=%(user_id)s AND f.confirmed_follow_at >= %(start_utc)s
      AND f.confirmed_follow_at < %(end_utc)s)::bigint AS follows_sent,
    (SELECT count(*) FROM relationship_follows f WHERE f.ig_account_id=account.id
      AND f.evidence #>> '{unfollow,outcome}'='confirmed'
      AND f.user_id=%(user_id)s AND f.unfollowed_at >= %(start_utc)s
      AND f.unfollowed_at < %(end_utc)s)::bigint AS unfollows_sent,
    start_snapshot.follower_count AS follower_start,
    end_snapshot.follower_count AS follower_end,
    start_snapshot.engagement_rate_bps AS engagement_rate_bps_start,
    end_snapshot.engagement_rate_bps AS engagement_rate_bps_end,
    COALESCE(run_activity.opportunities_reviewed, 0)::bigint
        AS opportunities_reviewed,
    COALESCE(sent_activity.likes_sent, 0)::bigint AS likes_sent,
    COALESCE(sent_activity.replies_sent, 0)::bigint AS replies_sent,
    COALESCE(sent_activity.dms_sent, 0)::bigint AS dms_sent,
    COALESCE(sent_activity.views_sent, 0)::bigint AS views_sent,
    COALESCE(run_activity.skipped, 0)::bigint AS skipped,
    COALESCE(run_activity.gated, 0)::bigint AS gated,
    COALESCE(run_activity.failed, 0)::bigint AS failed
FROM managed_accounts AS account
LEFT JOIN LATERAL (
    SELECT snapshot.follower_count, snapshot.engagement_rate_bps
    FROM account_growth_snapshots AS snapshot
    WHERE snapshot.user_id = account.user_id
      AND snapshot.ig_account_id = account.id
      AND snapshot.status IN ('complete', 'partial')
      AND snapshot.snapshot_date <= %(week_start)s
    ORDER BY snapshot.snapshot_date DESC, snapshot.captured_at DESC, snapshot.id DESC
    LIMIT 1
) AS start_snapshot ON true
LEFT JOIN LATERAL (
    SELECT snapshot.follower_count, snapshot.engagement_rate_bps
    FROM account_growth_snapshots AS snapshot
    WHERE snapshot.user_id = account.user_id
      AND snapshot.ig_account_id = account.id
      AND snapshot.status IN ('complete', 'partial')
      AND snapshot.snapshot_date <= %(next_week_start)s
    ORDER BY snapshot.snapshot_date DESC, snapshot.captured_at DESC, snapshot.id DESC
    LIMIT 1
) AS end_snapshot ON true
LEFT JOIN run_activity
  ON run_activity.user_id = account.user_id
 AND run_activity.ig_account_id = account.id
LEFT JOIN sent_activity
  ON sent_activity.user_id = account.user_id
 AND sent_activity.ig_account_id = account.id
ORDER BY account.created_at, account.id
"""

_INSERT_DELIVERY_SQL = """
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

_EXPIRE_ONE_SQL = """
UPDATE weekly_report_deliveries
SET status = 'failed',
    last_error_code = CASE
        WHEN created_at <= %(retry_cutoff)s
        THEN 'idempotency_window_expired'
        ELSE 'attempt_limit_reached'
    END,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = %(now)s
WHERE user_id = %(user_id)s
  AND week_start = %(week_start)s
  AND status IN ('pending', 'sending')
  AND (status = 'pending' OR lease_expires_at <= %(now)s)
  AND (
      attempts >= %(max_attempts)s
      OR created_at <= %(retry_cutoff)s
  )
"""

_SELECT_CLAIMABLE_SQL = """
SELECT id, user_id, week_start, recipient_email, attempts
FROM weekly_report_deliveries
WHERE user_id = %(user_id)s
  AND week_start = %(week_start)s
  AND attempts < %(max_attempts)s
  AND created_at > %(retry_cutoff)s
  AND (
      status = 'pending'
      OR (status = 'sending' AND lease_expires_at <= %(now)s)
  )
FOR UPDATE SKIP LOCKED
"""

_CLAIM_SQL = """
UPDATE weekly_report_deliveries
SET status = 'sending',
    attempts = attempts + 1,
    last_error_code = NULL,
    lease_token = %(lease_token)s,
    lease_expires_at = %(lease_expires_at)s,
    updated_at = %(now)s
WHERE id = %(delivery_id)s
RETURNING id, user_id, week_start, recipient_email, attempts
"""


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _aware_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    try:
        if value.utcoffset() is None:
            raise ValueError(f"{name} must be a timezone-aware datetime")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{name} must be a timezone-aware datetime") from exc
    return value


def _uuid(value: object, name: str) -> UUID:
    if type(value) is not UUID:
        raise ValueError(f"{name} must be a UUID")
    return value


def _recipient(value: object) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or len(value) > _MAX_RECIPIENT_LENGTH
    ):
        raise ValueError("recipient_email is invalid")
    return value


def _error_code(value: object) -> str:
    if type(value) is not str or _ERROR_CODE.fullmatch(value) is None:
        raise ValueError("error_code must be a bounded stable code")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class DueManager:
    """Safe inputs needed to claim and build one manager's prior-week report."""

    user_id: UUID
    recipient_email: str
    timezone: str
    period: ReportingPeriod

    def __repr__(self) -> str:
        return "DueManager(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class DeliveryClaim:
    """A delivery row and its short-lived, log-redacted mutation capability."""

    delivery_id: UUID
    user_id: UUID
    week_start: date
    recipient_email: str
    attempt_number: int
    lease_token: UUID

    def __repr__(self) -> str:
        return "DeliveryClaim(<redacted>)"


class ReportRepository:
    """Read aggregate report data and serialize one weekly delivery attempt."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def ensure_missing_preferences(self) -> int:
        """Create defaults for managers added after the reporting migration."""

        with self._pool.connection() as connection:
            cursor = connection.execute(_ENSURE_PREFERENCES_SQL)
            return cursor.rowcount

    def list_due_managers(self, now: datetime, limit: int) -> list[DueManager]:
        """Return enabled managers whose current local Monday send time passed."""

        checked_at = _aware_datetime(now, "now")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")

        self.ensure_missing_preferences()
        retry_cutoff = checked_at - _RETRY_HORIZON
        with self._pool.connection() as connection:
            connection.execute(
                _EXPIRE_UNCLAIMABLE_SQL,
                {
                    "now": checked_at,
                    "retry_cutoff": retry_cutoff,
                    "max_attempts": _MAX_ATTEMPTS,
                },
            )
            rows = connection.execute(
                _DUE_MANAGERS_SQL,
                {
                    "now": checked_at,
                    "limit": limit,
                    "retry_cutoff": retry_cutoff,
                    "max_attempts": _MAX_ATTEMPTS,
                },
            ).fetchall()

        return [
            DueManager(
                user_id=row["user_id"],
                recipient_email=_text(row["recipient_email"]),
                timezone=_text(row["timezone"]),
                period=previous_week(checked_at, _text(row["timezone"])),
            )
            for row in rows
        ]

    def build_report(
        self,
        user_id: UUID,
        period: ReportingPeriod,
    ) -> ManagerWeeklyReport:
        """Build aggregate-only account sections for one manager and local week."""

        manager_id = _uuid(user_id, "user_id")
        if type(period) is not ReportingPeriod:
            raise ValueError("period must be a ReportingPeriod")
        with self._pool.connection() as connection:
            rows = connection.execute(
                _REPORT_SQL,
                {
                    "user_id": manager_id,
                    "week_start": period.week_start,
                    "next_week_start": period.week_start + timedelta(days=7),
                    "start_utc": period.start_utc,
                    "end_utc": period.end_utc,
                },
            ).fetchall()

        accounts = tuple(
            AccountWeeklyReport(
                username=_text(row["username"]),
                follower_start=row["follower_start"],
                follower_end=row["follower_end"],
                engagement_rate_bps_start=row["engagement_rate_bps_start"],
                engagement_rate_bps_end=row["engagement_rate_bps_end"],
                opportunities_reviewed=int(row["opportunities_reviewed"]),
                likes_sent=int(row["likes_sent"]),
                replies_sent=int(row["replies_sent"]),
                dms_sent=int(row["dms_sent"]),
                views_sent=int(row["views_sent"]),
                follows_sent=int(row["follows_sent"]),
                unfollows_sent=int(row["unfollows_sent"]),
                skipped=int(row["skipped"]),
                gated=int(row["gated"]),
                failed=int(row["failed"]),
            )
            for row in rows
        )
        return ManagerWeeklyReport(period=period, accounts=accounts)

    def claim(
        self,
        user_id: UUID,
        week_start: date,
        recipient_email: str,
        now: datetime,
    ) -> DeliveryClaim | None:
        """Create the logical delivery and acquire its next bounded attempt."""

        manager_id = _uuid(user_id, "user_id")
        if type(week_start) is not date or week_start.weekday() != 0:
            raise ValueError("week_start must be a Monday date")
        recipient = _recipient(recipient_email)
        claimed_at = _aware_datetime(now, "now")

        with self._pool.connection() as connection:
            return self._claim_with_connection(
                connection,
                manager_id,
                week_start,
                recipient,
                claimed_at,
            )

    def _claim_with_connection(
        self,
        connection,
        user_id: UUID,
        week_start: date,
        recipient_email: str,
        now: datetime,
    ) -> DeliveryClaim | None:
        """Claim through an existing transaction after public input validation."""

        params = {
            "user_id": user_id,
            "week_start": week_start,
            "recipient_email": recipient_email,
            "now": now,
            "retry_cutoff": now - _RETRY_HORIZON,
            "max_attempts": _MAX_ATTEMPTS,
        }

        connection.execute(_INSERT_DELIVERY_SQL, params)
        connection.execute(_EXPIRE_ONE_SQL, params)
        row = connection.execute(_SELECT_CLAIMABLE_SQL, params).fetchone()
        if row is None:
            return None

        lease_token = uuid4()
        claimed = connection.execute(
            _CLAIM_SQL,
            {
                **params,
                "delivery_id": row["id"],
                "lease_token": lease_token,
                "lease_expires_at": now + _LEASE_DURATION,
            },
        ).fetchone()
        if claimed is None:
            return None

        return DeliveryClaim(
            delivery_id=claimed["id"],
            user_id=claimed["user_id"],
            week_start=claimed["week_start"],
            recipient_email=_text(claimed["recipient_email"]),
            attempt_number=int(claimed["attempts"]),
            lease_token=lease_token,
        )

    def lock_payload(
        self,
        delivery_id: UUID,
        lease_token: UUID,
        payload_hash: str,
        now: datetime | None = None,
    ) -> bool:
        """Persist the first request hash or fail a changed retry closed."""

        delivery = _uuid(delivery_id, "delivery_id")
        lease = _uuid(lease_token, "lease_token")
        if (
            type(payload_hash) is not str
            or _PAYLOAD_HASH.fullmatch(payload_hash) is None
        ):
            raise ValueError("payload_hash must be a lowercase SHA-256 digest")
        checked_at = _aware_datetime(
            datetime.now(timezone.utc) if now is None else now,
            "now",
        )
        params = {
            "delivery_id": delivery,
            "lease_token": lease,
            "payload_hash": payload_hash,
            "now": checked_at,
        }
        with self._pool.connection() as connection:
            accepted = connection.execute(
                """
                UPDATE weekly_report_deliveries
                SET payload_hash = COALESCE(payload_hash, %(payload_hash)s),
                    updated_at = %(now)s
                WHERE id = %(delivery_id)s
                  AND status = 'sending'
                  AND lease_token = %(lease_token)s
                  AND lease_expires_at > %(now)s
                  AND (payload_hash IS NULL OR payload_hash = %(payload_hash)s)
                RETURNING id
                """,
                params,
            ).fetchone()
            if accepted is not None:
                return True

            connection.execute(
                """
                UPDATE weekly_report_deliveries
                SET status = 'failed',
                    last_error_code = 'payload_hash_mismatch',
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = %(now)s
                WHERE id = %(delivery_id)s
                  AND status = 'sending'
                  AND lease_token = %(lease_token)s
                  AND lease_expires_at > %(now)s
                  AND payload_hash IS NOT NULL
                  AND payload_hash <> %(payload_hash)s
                RETURNING id
                """,
                params,
            ).fetchone()
        return False

    def mark_sent(
        self,
        delivery_id: UUID,
        lease_token: UUID,
        provider_message_id: str,
        now: datetime,
    ) -> bool:
        """Finalize a payload-locked attempt once the provider accepted it."""

        delivery = _uuid(delivery_id, "delivery_id")
        lease = _uuid(lease_token, "lease_token")
        if (
            type(provider_message_id) is not str
            or not provider_message_id.strip()
            or len(provider_message_id) > _MAX_PROVIDER_MESSAGE_ID_LENGTH
        ):
            raise ValueError("provider_message_id is invalid")
        completed_at = _aware_datetime(now, "now")
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE weekly_report_deliveries
                SET status = 'sent',
                    provider_message_id = %(provider_message_id)s,
                    last_error_code = NULL,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = %(now)s,
                    sent_at = %(now)s
                WHERE id = %(delivery_id)s
                  AND status = 'sending'
                  AND lease_token = %(lease_token)s
                  AND lease_expires_at > %(now)s
                  AND payload_hash IS NOT NULL
                RETURNING id
                """,
                {
                    "delivery_id": delivery,
                    "lease_token": lease,
                    "provider_message_id": provider_message_id,
                    "now": completed_at,
                },
            ).fetchone()
        return row is not None

    def mark_failed(
        self,
        delivery_id: UUID,
        lease_token: UUID,
        error_code: str,
        retryable: bool,
        now: datetime,
    ) -> bool:
        """Release a retryable attempt or permanently close the delivery."""

        delivery = _uuid(delivery_id, "delivery_id")
        lease = _uuid(lease_token, "lease_token")
        code = _error_code(error_code)
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        failed_at = _aware_datetime(now, "now")
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE weekly_report_deliveries
                SET status = CASE
                        WHEN %(retryable)s
                         AND attempts < %(max_attempts)s
                         AND created_at > %(retry_cutoff)s
                        THEN 'pending'
                        ELSE 'failed'
                    END,
                    last_error_code = %(error_code)s,
                    lease_token = NULL,
                    lease_expires_at = NULL,
                    updated_at = %(now)s
                WHERE id = %(delivery_id)s
                  AND status = 'sending'
                  AND lease_token = %(lease_token)s
                  AND lease_expires_at > %(now)s
                RETURNING id
                """,
                {
                    "delivery_id": delivery,
                    "lease_token": lease,
                    "error_code": code,
                    "retryable": retryable,
                    "now": failed_at,
                    "retry_cutoff": failed_at - _RETRY_HORIZON,
                    "max_attempts": _MAX_ATTEMPTS,
                },
            ).fetchone()
        return row is not None
