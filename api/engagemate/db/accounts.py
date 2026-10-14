"""Tenant-scoped PostgreSQL repositories for accounts, sessions, and jobs."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool
from psycopg.types.json import Jsonb

from engagemate.security.session_cipher import SessionCipher, SessionCipherError
from engagemate.domain.budgets import RECONNECT_WARMUP_AFTER, WARMUP_RESET_EVENT


logger = logging.getLogger(__name__)

_INSTAGRAM_USERNAME = re.compile(r"^[A-Za-z0-9._]+$")
_INSTAGRAM_USER_ID = re.compile(r"^[1-9][0-9]*$")


class AccountNotRunnableError(ValueError):
    """The owned account exists but its health state forbids new work."""


class AccountSessionIdentityMismatchError(ValueError):
    """The saved Instagram session does not match a paused account identity."""


class AccountLimitReachedError(ValueError):
    """The owner already holds every Instagram account the owner workspace permits."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__("Instagram account limit reached")


def account_seats(plan: object) -> int:
    """Bound owner workspace size independently of subscriptions."""
    return 100


def canonicalize_instagram_username(value: str) -> str:
    """Return the lowercase ASCII Instagram username used for persistence."""

    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or _INSTAGRAM_USERNAME.fullmatch(value) is None
    ):
        raise ValueError("invalid Instagram username")
    return value.lower()


def canonicalize_instagram_user_id(value: str) -> str:
    """Return a positive canonical ASCII-decimal Instagram user ID."""

    if not isinstance(value, str):
        raise ValueError("invalid Instagram user ID")
    normalized = value.strip()
    if (
        not 1 <= len(normalized) <= 128
        or _INSTAGRAM_USER_ID.fullmatch(normalized) is None
    ):
        raise ValueError("invalid Instagram user ID")
    return normalized


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


@dataclass(frozen=True, slots=True)
class AccountRecord:
    id: UUID
    username: str
    health: str
    captured_at: datetime | None
    proxy_bound: bool
    paused_at: datetime | None
    pause_reason: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "AccountRecord":
        return cls(
            id=row["id"],
            username=_text(row["username"]),
            health=_text(row["health_status"]),
            captured_at=row["captured_at"],
            proxy_bound=bool(row["proxy_bound"]),
            paused_at=row["paused_at"],
            pause_reason=(
                _text(row["pause_reason"]) if row["pause_reason"] is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: UUID
    ig_account_id: UUID
    schedule_key: str
    scheduled_for: datetime
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "JobRecord":
        return cls(
            id=row["id"],
            ig_account_id=row["ig_account_id"],
            schedule_key=_text(row["schedule_key"]),
            scheduled_for=row["scheduled_for"],
            status=_text(row["status"]),
            attempts=row["attempts"],
            last_error=(
                _text(row["last_error"]) if row["last_error"] is not None else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


_ACCOUNT_PROJECTION = """
SELECT account.id,
       account.username,
       account.health_status,
       account.paused_at,
       account.pause_reason,
       session.captured_at,
       EXISTS (
           SELECT 1
           FROM proxy_bindings binding
           WHERE binding.ig_account_id = account.id
             AND binding.user_id = account.user_id
             AND binding.released_at IS NULL
       ) AS proxy_bound
FROM ig_accounts account
LEFT JOIN LATERAL (
    SELECT captured_at
    FROM ig_sessions
    WHERE ig_account_id = account.id
      AND user_id = account.user_id
    ORDER BY captured_at DESC, id DESC
    LIMIT 1
) session ON TRUE
WHERE account.id = %(account_id)s
  AND account.user_id = %(user_id)s
"""


class AccountRepository:
    """Persist control-plane state without allowing cross-tenant access."""

    def __init__(self, pool: ConnectionPool, session_encryption_key: str) -> None:
        self.pool = pool
        self._session_encryption_key = session_encryption_key

    def account_exists(self, user_id: UUID, ig_user_id: str) -> bool:
        """Check ownership before applying a new-account capacity gate."""

        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT 1 FROM ig_accounts WHERE user_id = %s AND ig_user_id = %s",
                (user_id, ig_user_id),
            ).fetchone()
        return row is not None

    def repair_verified_identity(
        self,
        user_id: UUID,
        account_id: UUID,
        *,
        expected_ig_user_id: str,
        verified_ig_user_id: str,
        verified_username: str,
    ) -> bool:
        """Repair a stale numeric ID only after authenticated username proof."""

        expected_id = canonicalize_instagram_user_id(expected_ig_user_id)
        verified_id = canonicalize_instagram_user_id(verified_ig_user_id)
        username = canonicalize_instagram_username(verified_username)
        if expected_id == verified_id:
            return False
        with self.pool.connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(CAST(%s AS text), 0))",
                (str(user_id),),
            )
            account = connection.execute(
                """
                SELECT username, ig_user_id, health_status, paused_at
                FROM ig_accounts
                WHERE id=%(account_id)s AND user_id=%(user_id)s
                FOR UPDATE
                """,
                {"account_id": account_id, "user_id": user_id},
            ).fetchone()
            if (
                account is None
                or _text(account["username"]) != username
                or account["ig_user_id"] is None
                or _text(account["ig_user_id"]) != expected_id
                or _text(account["health_status"]) != "active"
                or account["paused_at"] is not None
            ):
                return False
            collision = connection.execute(
                """
                SELECT 1 FROM ig_accounts
                WHERE user_id=%(user_id)s AND ig_user_id=%(ig_user_id)s
                  AND id<>%(account_id)s
                """,
                {
                    "user_id": user_id,
                    "ig_user_id": verified_id,
                    "account_id": account_id,
                },
            ).fetchone()
            if collision is not None:
                return False
            updated = connection.execute(
                """
                UPDATE ig_accounts
                SET ig_user_id=%(verified_id)s, updated_at=now()
                WHERE id=%(account_id)s AND user_id=%(user_id)s
                  AND ig_user_id=%(expected_id)s AND lower(username)=%(username)s
                  AND health_status='active' AND paused_at IS NULL
                RETURNING id
                """,
                {
                    "verified_id": verified_id,
                    "account_id": account_id,
                    "user_id": user_id,
                    "expected_id": expected_id,
                    "username": username,
                },
            ).fetchone()
            if updated is None:
                return False
            connection.execute(
                """
                INSERT INTO account_health_events
                    (user_id, ig_account_id, event_type, severity, evidence)
                VALUES (%s, %s, 'identity_repaired', 'info', %s)
                """,
                (
                    user_id,
                    account_id,
                    Jsonb(
                        {"source": "authenticated_session", "username": username}
                    ),
                ),
            )
            return True

    def upsert_session(
        self,
        *,
        user_id: UUID,
        username: str,
        ig_user_id: str,
        cookies: Sequence[Mapping[str, Any]],
        user_agent: str,
        login_country: str,
        login_region: str | None,
        login_city: str | None,
        expires_at: datetime | None,
        connection: Connection | None = None,
        account_id: UUID | None = None,
    ) -> tuple[AccountRecord, bool]:
        """Create/update one account and replace its current encrypted session."""

        username = canonicalize_instagram_username(username)
        ig_user_id = canonicalize_instagram_user_id(ig_user_id)
        transaction = nullcontext(connection) if connection is not None else self.pool.connection()
        with transaction as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(CAST(%s AS text), 0))",
                (str(user_id),),
            )
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"{user_id}:{ig_user_id}",),
            )
            existing = connection.execute(
                """
                SELECT id
                FROM ig_accounts
                WHERE user_id = %(user_id)s AND ig_user_id = %(ig_user_id)s
                """,
                {"user_id": user_id, "ig_user_id": ig_user_id},
            ).fetchone()
            if existing is None:
                owner = connection.execute(
                    "SELECT plan FROM users WHERE id = %(user_id)s",
                    {"user_id": user_id},
                ).fetchone()
                if owner is None:
                    raise ValueError("account owner not found")
                seats = account_seats(owner["plan"])
                owned = connection.execute(
                    "SELECT count(*) AS owned FROM ig_accounts WHERE user_id = %(user_id)s",
                    {"user_id": user_id},
                ).fetchone()
                if int(owned["owned"]) >= seats:
                    raise AccountLimitReachedError(seats)
            account_row = connection.execute(
                """
                INSERT INTO ig_accounts (
                    id, user_id, username, ig_user_id, login_country,
                    login_region, login_city, warmup_started_at,
                    last_healthy_at
                )
                VALUES (
                    %(account_id)s, %(user_id)s, %(username)s, %(ig_user_id)s,
                    %(country)s, %(region)s, %(city)s, NULL, now()
                )
                ON CONFLICT (user_id, ig_user_id)
                    WHERE ig_user_id IS NOT NULL
                DO UPDATE SET
                    username = EXCLUDED.username,
                    login_country = EXCLUDED.login_country,
                    login_region = EXCLUDED.login_region,
                    login_city = EXCLUDED.login_city,
                    last_healthy_at = now(),
                    updated_at = now()
                RETURNING id
                """,
                {
                    "account_id": account_id or uuid4(),
                    "user_id": user_id,
                    "username": username,
                    "ig_user_id": ig_user_id,
                    "country": login_country,
                    "region": login_region,
                    "city": login_city,
                },
            ).fetchone()
            account_id = account_row["id"]
            created = existing is None

            # Both the scheduler and the worker inner-join agent_configs, so an
            # account captured without one is silently never scheduled and
            # fails a manual job with config_missing. Seed it disabled with
            # likes-only defaults capped to the plan; Launch starts warm-up.
            connection.execute(
                """
                INSERT INTO agent_configs (user_id, ig_account_id, daily_like_limit)
                SELECT %(user_id)s, %(account_id)s,
                       20
                FROM users WHERE id=%(user_id)s
                ON CONFLICT (ig_account_id) DO NOTHING
                """,
                {"user_id": user_id, "account_id": account_id},
            )

            # The upsert resolves stable identity. Lock that row before replacing
            # the mutable session so concurrent first captures cannot create
            # multiple session rows.
            connection.execute(
                """
                SELECT id
                FROM ig_accounts
                WHERE id = %(account_id)s AND user_id = %(user_id)s
                FOR UPDATE
                """,
                {"account_id": account_id, "user_id": user_id},
            ).fetchone()

            encrypted = SessionCipher(
                self._session_encryption_key, account_id
            ).encrypt({"cookies": list(cookies)})
            current_session = connection.execute(
                """
                SELECT id
                FROM ig_sessions
                WHERE ig_account_id = %(account_id)s
                  AND user_id = %(user_id)s
                ORDER BY captured_at DESC, id DESC
                LIMIT 1
                FOR UPDATE
                """,
                {"account_id": account_id, "user_id": user_id},
            ).fetchone()
            session_values = {
                "user_id": user_id,
                "account_id": account_id,
                "blob": encrypted,
                "user_agent": user_agent,
                "expires_at": expires_at,
            }
            if current_session is None:
                connection.execute(
                    """
                    INSERT INTO ig_sessions (
                        user_id, ig_account_id, encrypted_cookie_blob,
                        encryption_version, captured_user_agent, expires_at
                    )
                    VALUES (
                        %(user_id)s, %(account_id)s, %(blob)s, 1,
                        %(user_agent)s, %(expires_at)s
                    )
                    """,
                    session_values,
                )
            else:
                connection.execute(
                    """
                    UPDATE ig_sessions
                    SET encrypted_cookie_blob = %(blob)s,
                        encryption_version = 1,
                        captured_user_agent = %(user_agent)s,
                        captured_at = now(),
                        last_validated_at = NULL,
                        expires_at = %(expires_at)s
                    WHERE id = %(session_id)s
                      AND user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                    """,
                    {**session_values, "session_id": current_session["id"]},
                )
                connection.execute(
                    """
                    DELETE FROM ig_sessions
                    WHERE user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND id <> %(session_id)s
                    """,
                    {**session_values, "session_id": current_session["id"]},
                )

            row = connection.execute(
                _ACCOUNT_PROJECTION,
                {"account_id": account_id, "user_id": user_id},
            ).fetchone()
        return AccountRecord.from_row(row), created

    def get(self, user_id: UUID, account_id: UUID) -> AccountRecord | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                _ACCOUNT_PROJECTION,
                {"account_id": account_id, "user_id": user_id},
            ).fetchone()
        return AccountRecord.from_row(row) if row is not None else None

    def rename(
        self, user_id: UUID, account_id: UUID, username: str
    ) -> AccountRecord | None:
        username = canonicalize_instagram_username(username)
        with self.pool.connection() as connection:
            try:
                row = connection.execute(
                    """
                    UPDATE ig_accounts
                    SET username = %(username)s, updated_at = now()
                    WHERE id = %(account_id)s AND user_id = %(user_id)s
                    RETURNING id
                    """,
                    {
                        "username": username,
                        "account_id": account_id,
                        "user_id": user_id,
                    },
                ).fetchone()
            except UniqueViolation:
                raise
            if row is None:
                return None
            account = connection.execute(
                _ACCOUNT_PROJECTION,
                {"account_id": account_id, "user_id": user_id},
            ).fetchone()
        return AccountRecord.from_row(account)

    def set_paused(
        self,
        user_id: UUID,
        account_id: UUID,
        *,
        paused: bool,
        reason: str | None = None,
    ) -> AccountRecord | None:
        with self.pool.connection() as connection:
            if not paused:
                readiness = connection.execute(
                    """
                    SELECT account.health_status, account.pause_reason,
                           account.paused_at, account.ig_user_id,
                           session.encrypted_cookie_blob, session.captured_at
                    FROM ig_accounts account
                    LEFT JOIN LATERAL (
                        SELECT encrypted_cookie_blob, captured_at
                        FROM ig_sessions
                        WHERE user_id = account.user_id
                          AND ig_account_id = account.id
                        ORDER BY captured_at DESC, id DESC
                        LIMIT 1
                    ) session ON TRUE
                    WHERE account.id = %(account_id)s AND account.user_id = %(user_id)s
                    FOR UPDATE OF account
                    """,
                    {"account_id": account_id, "user_id": user_id},
                ).fetchone()
                if readiness is None:
                    return None
                if _text(readiness["pause_reason"]) == "relationship_identity_mismatch":
                    if (
                        _text(readiness["health_status"]) != "paused"
                        or readiness["paused_at"] is None
                        or readiness["captured_at"] is None
                        or readiness["captured_at"] <= readiness["paused_at"]
                        or not readiness["ig_user_id"]
                        or readiness["encrypted_cookie_blob"] is None
                    ):
                        raise AccountSessionIdentityMismatchError(
                            "Reconnect the matching Instagram account before resuming"
                        )
                    try:
                        payload = SessionCipher(
                            self._session_encryption_key, account_id
                        ).decrypt(_text(readiness["encrypted_cookie_blob"]))
                    except SessionCipherError:
                        raise AccountSessionIdentityMismatchError(
                            "Reconnect the matching Instagram account before resuming"
                        ) from None
                    cookies = payload.get("cookies")
                    identities = {
                        cookie.get("value")
                        for cookie in cookies
                        if isinstance(cookie, dict)
                        and cookie.get("name") == "ds_user_id"
                        and isinstance(cookie.get("value"), str)
                    } if isinstance(cookies, list) else set()
                    if identities != {_text(readiness["ig_user_id"])}:
                        raise AccountSessionIdentityMismatchError(
                            "Reconnect the matching Instagram account before resuming"
                        )

            row = connection.execute(
                """
                UPDATE ig_accounts
                SET health_status = %(health)s,
                    paused_at = CASE WHEN %(paused)s THEN COALESCE(paused_at, now()) ELSE NULL END,
                    pause_reason = CASE WHEN %(paused)s THEN %(reason)s ELSE NULL END,
                    updated_at = now()
                WHERE id = %(account_id)s AND user_id = %(user_id)s
                  AND (
                    (%(paused)s AND health_status IN ('active', 'degraded', 'paused'))
                    OR (NOT %(paused)s AND health_status = 'paused')
                  )
                RETURNING id
                """,
                {
                    "health": "paused" if paused else "active",
                    "paused": paused,
                    "reason": reason,
                    "account_id": account_id,
                    "user_id": user_id,
                },
            ).fetchone()
            if row is None:
                return None
            if not paused and readiness["paused_at"] is not None:
                connection.execute(
                    """
                    INSERT INTO account_health_events (
                        user_id, ig_account_id, event_type, severity, evidence
                    )
                    SELECT %(user_id)s, %(account_id)s, %(event_type)s, 'info',
                           jsonb_build_object('disconnected_at', %(disconnected_at)s::text)
                    WHERE %(disconnected_at)s <= now() - %(restart_after)s
                    """,
                    {
                        "user_id": user_id,
                        "account_id": account_id,
                        "event_type": WARMUP_RESET_EVENT,
                        "disconnected_at": readiness["paused_at"],
                        "restart_after": RECONNECT_WARMUP_AFTER,
                    },
                )
            if paused:
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled',
                        lease_expires_at = NULL,
                        updated_at = now()
                    WHERE user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND status = 'queued'
                    """,
                    {"user_id": user_id, "account_id": account_id},
                )
            account = connection.execute(
                _ACCOUNT_PROJECTION,
                {"account_id": account_id, "user_id": user_id},
            ).fetchone()
        return AccountRecord.from_row(account)

    def create_job(
        self,
        user_id: UUID,
        account_id: UUID,
        *,
        scheduled_for: datetime | None,
        schedule_key: str | None,
    ) -> JobRecord | None:
        """Create a queued job only after proving tenant ownership."""

        job_id = uuid4()
        key = schedule_key or f"manual:{account_id}:{job_id}"
        when = scheduled_for or datetime.now(timezone.utc)
        with self.pool.connection() as connection:
            account = connection.execute(
                """
                SELECT health_status
                FROM ig_accounts
                WHERE id = %(account_id)s AND user_id = %(user_id)s
                FOR SHARE
                """,
                {"account_id": account_id, "user_id": user_id},
            ).fetchone()
            if account is None:
                return None
            health = _text(account["health_status"])
            if health not in {"active", "degraded"}:
                raise AccountNotRunnableError(health)
            row = connection.execute(
                """
                INSERT INTO jobs (
                    id, user_id, ig_account_id, schedule_key, scheduled_for
                )
                VALUES (
                    %(job_id)s, %(user_id)s, %(account_id)s,
                    %(schedule_key)s, %(scheduled_for)s
                )
                RETURNING *
                """,
                {
                    "job_id": job_id,
                    "account_id": account_id,
                    "user_id": user_id,
                    "schedule_key": key,
                    "scheduled_for": when,
                },
            ).fetchone()
        return JobRecord.from_row(row) if row is not None else None

    def get_job(self, user_id: UUID, job_id: UUID) -> JobRecord | None:
        with self.pool.connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = %s AND user_id = %s",
                (job_id, user_id),
            ).fetchone()
        return JobRecord.from_row(row) if row is not None else None
