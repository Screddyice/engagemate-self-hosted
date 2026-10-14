"""Tenant-scoped persistence for daily managed-account growth snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import quote
from uuid import UUID

from psycopg_pool import ConnectionPool

from engagemate.growth.metrics import ProfileMetrics

_FAILURE_CODES = frozenset(
    {
        "provider_unavailable",
        "invalid_profile",
        "private_profile",
        "invalid_username",
    }
)
_INSTAGRAM_USERNAME = re.compile(r"[a-z0-9._]{1,30}")

_CANDIDATES_SQL = """
WITH eligible AS (
    SELECT
        account.user_id,
        account.id AS ig_account_id,
        account.username,
        config.timezone,
        (%(now)s::timestamptz AT TIME ZONE timezone_name.name)::date
            AS snapshot_date
    FROM ig_accounts AS account
    JOIN agent_configs AS config
      ON config.ig_account_id = account.id
     AND config.user_id = account.user_id
    JOIN pg_timezone_names AS timezone_name
      ON timezone_name.name = config.timezone
    WHERE config.enabled = true
      AND account.paused_at IS NULL
      AND account.health_status = 'active'
      AND (%(user_id)s::uuid IS NULL OR account.user_id = %(user_id)s::uuid)
), due AS (
    SELECT
        eligible.user_id,
        eligible.ig_account_id,
        eligible.username,
        eligible.timezone,
        eligible.snapshot_date,
        retry.captured_at AS retry_captured_at
    FROM eligible
    LEFT JOIN account_growth_snapshots AS retry
      ON retry.user_id = eligible.user_id
     AND retry.ig_account_id = eligible.ig_account_id
     AND retry.snapshot_date = eligible.snapshot_date
     AND retry.status = 'unavailable'
    WHERE NOT EXISTS (
        SELECT 1
        FROM account_growth_snapshots AS valid
        WHERE valid.user_id = eligible.user_id
          AND valid.ig_account_id = eligible.ig_account_id
          AND valid.snapshot_date = eligible.snapshot_date
          AND valid.status IN ('partial', 'complete')
    )
)
SELECT user_id, ig_account_id, username, timezone, snapshot_date
FROM due
ORDER BY
    CASE WHEN retry_captured_at IS NULL THEN 0 ELSE 1 END,
    retry_captured_at ASC NULLS FIRST,
    ig_account_id
LIMIT %(limit)s
"""

_UPSERT_SQL = """
WITH accepted AS (
    INSERT INTO account_growth_snapshots (
        user_id,
        ig_account_id,
        snapshot_date,
        captured_at,
        follower_count,
        following_count,
        media_count,
        engagement_rate_bps,
        posts_sampled,
        source,
        status,
        failure_code
    )
    VALUES (
        %(user_id)s,
        %(ig_account_id)s,
        %(snapshot_date)s,
        %(captured_at)s,
        %(follower_count)s,
        %(following_count)s,
        %(media_count)s,
        %(engagement_rate_bps)s,
        %(posts_sampled)s,
        %(source)s,
        %(status)s,
        %(failure_code)s
    )
    ON CONFLICT (ig_account_id, snapshot_date) DO UPDATE SET
        user_id = EXCLUDED.user_id,
        captured_at = EXCLUDED.captured_at,
        follower_count = EXCLUDED.follower_count,
        following_count = EXCLUDED.following_count,
        media_count = EXCLUDED.media_count,
        engagement_rate_bps = EXCLUDED.engagement_rate_bps,
        posts_sampled = EXCLUDED.posts_sampled,
        source = EXCLUDED.source,
        status = EXCLUDED.status,
        failure_code = EXCLUDED.failure_code
    WHERE (
        EXCLUDED.status = 'unavailable'
        AND account_growth_snapshots.status = 'unavailable'
    ) OR (
        CASE EXCLUDED.status
            WHEN 'unavailable' THEN 0
            WHEN 'partial' THEN 1
            WHEN 'complete' THEN 2
        END
        > CASE account_growth_snapshots.status
            WHEN 'unavailable' THEN 0
            WHEN 'partial' THEN 1
            WHEN 'complete' THEN 2
        END
    )
    RETURNING user_id, ig_account_id, follower_count, status
), updated_account AS (
    UPDATE ig_accounts AS account
       SET follower_count = accepted.follower_count,
           updated_at = %(captured_at)s
      FROM accepted
     WHERE account.id = accepted.ig_account_id
       AND account.user_id = accepted.user_id
       AND accepted.status IN ('partial', 'complete')
       AND accepted.follower_count IS NOT NULL
    RETURNING account.id
)
SELECT EXISTS (SELECT 1 FROM accepted) AS accepted
"""


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def normalize_instagram_username(username: str | bytes) -> str:
    """Return the canonical public-path form of a stored username."""

    normalized = _text(username).strip().removeprefix("@").strip().lower()
    if _INSTAGRAM_USERNAME.fullmatch(normalized) is None:
        raise ValueError("Instagram username is invalid")
    return normalized


@dataclass(frozen=True, slots=True)
class DueAccount:
    """One managed account missing a valid observation for its local date."""

    user_id: UUID
    ig_account_id: UUID
    username: str
    timezone: str
    snapshot_date: date

    @property
    def profile_url(self) -> str:
        username = normalize_instagram_username(self.username)
        return f"https://www.instagram.com/{quote(username, safe='._-')}/"


class GrowthSnapshotRepository:
    """Select due accounts and atomically keep their strongest daily snapshot."""

    def __init__(self, pool: ConnectionPool, *, source: str = "hypercrawl") -> None:
        if source not in {"hypercrawl", "account_session"}:
            raise ValueError("invalid growth source")
        self.pool = pool
        self.source = source

    def list_due(
        self,
        now: datetime,
        limit: int,
        *,
        user_id: UUID | None = None,
    ) -> list[DueAccount]:
        """Return enabled, healthy accounts without a valid local-date row."""

        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if user_id is not None and not isinstance(user_id, UUID):
            raise ValueError("user_id must be a UUID")

        with self.pool.connection() as connection:
            candidates = connection.execute(
                _CANDIDATES_SQL,
                {"now": now, "user_id": user_id, "limit": limit},
            ).fetchall()
        return [
            DueAccount(
                user_id=row["user_id"],
                ig_account_id=row["ig_account_id"],
                username=_text(row["username"]),
                timezone=_text(row["timezone"]),
                snapshot_date=row["snapshot_date"],
            )
            for row in candidates
        ]

    def upsert(
        self,
        account: DueAccount,
        metrics: ProfileMetrics,
        captured_at: datetime,
        failure_code: str | None = None,
    ) -> bool:
        """Store a stronger observation or refresh an unavailable attempt."""

        if captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if metrics.status == "unavailable":
            if failure_code not in _FAILURE_CODES:
                raise ValueError("unavailable snapshots require a stable failure code")
        elif failure_code is not None:
            raise ValueError("valid snapshots cannot carry a failure code")

        params = {
            "user_id": account.user_id,
            "ig_account_id": account.ig_account_id,
            "snapshot_date": account.snapshot_date,
            "source": self.source,
            "captured_at": captured_at,
            "follower_count": metrics.follower_count,
            "following_count": metrics.following_count,
            "media_count": metrics.media_count,
            "engagement_rate_bps": metrics.engagement_rate_bps,
            "posts_sampled": metrics.posts_sampled,
            "status": metrics.status,
            "failure_code": failure_code,
        }
        with self.pool.connection() as connection:
            row = connection.execute(_UPSERT_SQL, params).fetchone()
        return bool(row["accepted"])
