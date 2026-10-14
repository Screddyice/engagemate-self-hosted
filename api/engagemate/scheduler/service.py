"""Enqueue account jobs once their local schedule windows have arrived.

Idempotency is enforced by a globally unique `jobs.schedule_key` of the form
`{ig_account_id}:{local_date_iso}:{schedule_hour}`. View-only jobs use
`views:{ig_account_id}:{local_date_iso}:{slot_hour}` and spread the independent
Story-view budget across four local-time windows. Re-running the scheduler for
the same window inserts nothing new.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timezone
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.connection import Connection
from psycopg_pool import ConnectionPool
from engagemate.domain.models import VIEW_SLOT_COUNT

logger = logging.getLogger(__name__)

_CANDIDATES_SQL = """
SELECT
    account.id AS ig_account_id,
    account.user_id AS user_id,
    config.timezone AS timezone,
    config.schedule_hour AS schedule_hour,
    config.daily_view_limit AS daily_view_limit
FROM agent_configs AS config
JOIN ig_accounts AS account
    ON account.id = config.ig_account_id
   AND account.user_id = config.user_id
WHERE config.enabled = true
  AND account.health_status IN ('active', 'degraded')
  AND account.paused_at IS NULL
"""

_INSERT_JOB_SQL = """
INSERT INTO jobs (
    user_id, ig_account_id, schedule_key, scheduled_for, status
)
VALUES (
    %(user_id)s, %(ig_account_id)s, %(schedule_key)s,
    %(scheduled_for)s, 'queued'
)
ON CONFLICT (schedule_key) DO NOTHING
RETURNING id
"""


@dataclass(frozen=True)
class ScheduleSlot:
    """One account's due daily schedule window, expressed in UTC."""

    local_date_iso: str
    schedule_hour: int
    scheduled_for: datetime
    view_only: bool = False


def build_schedule_key(
    ig_account_id: UUID,
    local_date_iso: str,
    schedule_hour: int,
) -> str:
    """Return the stable unique key for one account's schedule window."""

    return f"{ig_account_id}:{local_date_iso}:{schedule_hour}"


def build_view_schedule_key(
    ig_account_id: UUID,
    local_date_iso: str,
    slot_hour: int,
) -> str:
    """Return the idempotent key for one local view-only slot."""

    return f"views:{ig_account_id}:{local_date_iso}:{slot_hour}"


def resolve_schedule_slot(
    now: datetime,
    tz_name: str,
    schedule_hour: int,
) -> ScheduleSlot | None:
    """Return the due slot when local hour has reached `schedule_hour`.

    Unknown or invalid timezones yield `None` so a bad config cannot take down
    the whole batch. A slot whose UTC instant is still in the future is also
    rejected (defensive; the hour check already implies the slot has started).
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not 0 <= schedule_hour <= 23:
        return None

    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return None

    local_now = now.astimezone(zone)
    if local_now.hour < schedule_hour:
        return None

    local_date = local_now.date()
    local_slot = datetime.combine(
        local_date, time(hour=schedule_hour, minute=0, second=0), tzinfo=zone
    )
    scheduled_for = local_slot.astimezone(timezone.utc)
    if scheduled_for > now.astimezone(timezone.utc):
        return None

    return ScheduleSlot(
        local_date_iso=local_date.isoformat(),
        schedule_hour=schedule_hour,
        scheduled_for=scheduled_for,
    )


def resolve_view_schedule_slots(
    now: datetime,
    tz_name: str,
    daily_view_limit: int,
) -> tuple[ScheduleSlot, ...]:
    """Return due six-hour view windows in the account's local timezone.

    The view lane is independent of the engagement schedule. Four bounded
    windows give the worker time to discover fresh Stories throughout a local
    day while the account's daily usage query remains the final cap.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if type(daily_view_limit) is not int or daily_view_limit <= 0:
        return ()
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return ()
    local_now = now.astimezone(zone)
    slots: list[ScheduleSlot] = []
    for slot_hour in range(0, 24, 24 // VIEW_SLOT_COUNT):
        local_slot = datetime.combine(
            local_now.date(), time(hour=slot_hour), tzinfo=zone
        )
        scheduled_for = local_slot.astimezone(timezone.utc)
        if scheduled_for <= now.astimezone(timezone.utc):
            slots.append(
                ScheduleSlot(
                    local_date_iso=local_now.date().isoformat(),
                    schedule_hour=slot_hour,
                    scheduled_for=scheduled_for,
                    view_only=True,
                )
            )
    return tuple(slots)


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


class Scheduler:
    """Find enabled healthy accounts that are due and enqueue one job each."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        capacity_tick: Callable[[], int] | None = None,
        require_proxy_binding: bool = False,
    ) -> None:
        self.pool = pool
        self._capacity_tick = capacity_tick
        self._require_proxy_binding = require_proxy_binding

    def enqueue_due(self, now: datetime) -> int:
        """Insert queued jobs for every due account; return newly inserted count.

        Accounts already holding a job for the same `schedule_key` are skipped
        via `ON CONFLICT DO NOTHING` and do not contribute to the return value.
        """

        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")

        if self._capacity_tick is not None:
            changed = self._capacity_tick()
            logger.info("proxy capacity tick changed=%d", changed)

        with self.pool.connection() as connection:
            candidates_sql = _CANDIDATES_SQL
            if self._require_proxy_binding:
                candidates_sql += """
                  AND EXISTS (
                      SELECT 1
                      FROM proxy_bindings binding
                      JOIN proxies proxy ON proxy.id = binding.proxy_id
                      WHERE binding.user_id = account.user_id
                        AND binding.ig_account_id = account.id
                        AND binding.released_at IS NULL
                        AND proxy.health_status = 'active'
                  )
                """
            candidates = connection.execute(candidates_sql).fetchall()
            inserted = 0
            for row in candidates:
                inserted += self._try_enqueue(connection, row, now)
        return inserted

    def _try_enqueue(
        self,
        connection: Connection,
        row: Mapping[str, object],
        now: datetime,
    ) -> int:
        ig_account_id = row["ig_account_id"]
        user_id = row["user_id"]
        tz_name = _text(row["timezone"])  # type: ignore[arg-type]
        schedule_hour = int(row["schedule_hour"])  # type: ignore[arg-type]
        daily_view_limit = int(row.get("daily_view_limit", 0) or 0)  # type: ignore[union-attr]

        inserted = 0
        slot = resolve_schedule_slot(now, tz_name, schedule_hour)
        if slot is not None:
            result = connection.execute(
                _INSERT_JOB_SQL,
                {
                    "user_id": user_id,
                    "ig_account_id": ig_account_id,
                    "schedule_key": build_schedule_key(
                        ig_account_id, slot.local_date_iso, slot.schedule_hour
                    ),
                    "scheduled_for": slot.scheduled_for,
                },
            ).fetchone()
            inserted += result is not None
        elif tz_name and not _is_known_timezone(tz_name):
            logger.warning(
                "skipping account with invalid timezone account=%s tz=%s",
                ig_account_id,
                tz_name,
            )
            return 0
        if daily_view_limit <= 0:
            return inserted
        for view_slot in resolve_view_schedule_slots(now, tz_name, daily_view_limit):
            result = connection.execute(
                _INSERT_JOB_SQL,
                {
                    "user_id": user_id,
                    "ig_account_id": ig_account_id,
                    "schedule_key": build_view_schedule_key(
                        ig_account_id, view_slot.local_date_iso, view_slot.schedule_hour
                    ),
                    "scheduled_for": view_slot.scheduled_for,
                },
            ).fetchone()
            inserted += result is not None
        return inserted


def _is_known_timezone(tz_name: str) -> bool:
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return False
    return True
