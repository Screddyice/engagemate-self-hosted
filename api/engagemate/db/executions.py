"""Async tenant-scoped persistence for story executions."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool

if TYPE_CHECKING:
    from engagemate.worker.circuit_breaker import BreakerDecision


class RepositoryError(RuntimeError):
    def __repr__(self) -> str:
        return "RepositoryError(<redacted>)"


class UsageLimitExceeded(RepositoryError):
    def __repr__(self) -> str:
        return "UsageLimitExceeded(<redacted>)"


STORY_SKIP_REASONS = frozenset({"story_unavailable", "story_expired"})


class _RedactedText(str):
    def __repr__(self) -> str:
        return "<redacted-text>"


def _redacted_text(value: object, *, maximum: int) -> _RedactedText:
    valid = type(value) is str and bool(value.strip()) and len(value) <= maximum
    if not valid:
        del value
        raise ValueError("invalid claim metadata") from None
    safe = _RedactedText(value)
    del value
    return safe


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ClaimMetadata:
    user_id: UUID
    ig_account_id: UUID
    target_id: UUID
    fingerprint: str
    action: Literal["view", "like", "reply", "dm"]
    generated_text: str | None
    model_used: str
    rationale: str
    confidence: float

    def __init__(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        target_id: UUID,
        fingerprint: str,
        action: Literal["view", "like", "reply", "dm"],
        generated_text: str | None,
        model_used: str,
        rationale: str,
        confidence: float,
    ) -> None:
        try:
            safe_generated = (
                None
                if generated_text is None
                else _redacted_text(generated_text, maximum=200)
            )
        finally:
            del generated_text
        try:
            safe_rationale = _redacted_text(rationale, maximum=1_000)
        finally:
            del rationale
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "ig_account_id", ig_account_id)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "generated_text", safe_generated)
        object.__setattr__(self, "model_used", model_used)
        object.__setattr__(self, "rationale", safe_rationale)
        object.__setattr__(self, "confidence", confidence)

    def __repr__(self) -> str:
        return "ClaimMetadata(<redacted>)"


@dataclass(frozen=True, slots=True)
class ClaimResult:
    engagement_id: UUID
    duplicate: bool


class ExecutionRepository:
    """Run blocking psycopg transactions outside the event loop."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def claim(self, metadata: ClaimMetadata) -> ClaimResult:
        if not self._valid_metadata(metadata):
            raise ValueError("invalid claim metadata")
        return await self._offload(self._claim_sync, metadata)

    async def mark_sent(
        self, user_id: UUID, ig_account_id: UUID, engagement_id: UUID
    ) -> bool:
        return await self._offload(
            self._finish_sync, user_id, ig_account_id, engagement_id, True, None
        )

    async def mark_failed(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        engagement_id: UUID,
        failure_code: str,
    ) -> bool:
        if failure_code not in {
            "action_failed",
            "breaker_tripped",
            "cancelled",
            "repository_failed",
            "network_error",
            "proxy_error",
            "reply_follow_unconfirmed",
        }:
            raise ValueError("invalid failure code")
        return await self._offload(
            self._finish_sync,
            user_id,
            ig_account_id,
            engagement_id,
            False,
            failure_code,
        )

    async def mark_skipped(
        self, user_id: UUID, ig_account_id: UUID, engagement_id: UUID, reason: str
    ) -> bool:
        """Release unused capacity but retain the unique Story claim."""
        if reason not in STORY_SKIP_REASONS:
            raise ValueError("invalid skip reason")
        return await self._offload(
            self._finish_sync, user_id, ig_account_id, engagement_id,
            False, reason, True,
        )

    async def persist_breaker(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        decision: BreakerDecision,
    ) -> None:
        from engagemate.worker.circuit_breaker import (
            AccountHealthStatus,
            BreakerDecision,
        )

        exact_utc_observed = (
            type(decision) is BreakerDecision
            and type(decision.observed_at) is datetime
            and decision.observed_at.tzinfo is timezone.utc
        )
        exact_rate_pause = (
            type(decision) is BreakerDecision
            and type(decision.pause_until) is datetime
            and decision.pause_until.tzinfo is timezone.utc
            and exact_utc_observed
            and decision.pause_until
            == decision.observed_at + timedelta(hours=24)
        )
        expected_status = {
            "challenge": AccountHealthStatus.CHALLENGED,
            "checkpoint": AccountHealthStatus.CHALLENGED,
            "login_required": AccountHealthStatus.PAUSED,
            "suspicious_activity": AccountHealthStatus.PAUSED,
            "health_unavailable": AccountHealthStatus.PAUSED,
            "rate_limit": AccountHealthStatus.PAUSED,
        }
        valid = (
            type(decision) is BreakerDecision
            and decision.stop is True
            and exact_utc_observed
            and decision.reason_code in expected_status
            and decision.health_status is expected_status.get(decision.reason_code)
            and (
                (
                    decision.reason_code == "rate_limit"
                    and exact_rate_pause
                )
                or (
                    decision.reason_code != "rate_limit"
                    and decision.pause_until is None
                )
            )
        )
        if not valid:
            raise ValueError("invalid breaker decision")
        await self._offload(self._persist_breaker_sync, user_id, ig_account_id, decision)

    async def _offload(self, function: object, *args: object):
        failed = False
        try:
            return await asyncio.to_thread(function, *args)
        except UsageLimitExceeded:
            raise
        except Exception:  # noqa: BLE001 - adapters expose no shared exception base
            failed = True
        if failed:
            raise RepositoryError("execution repository operation failed") from None
        raise AssertionError("unreachable")

    @staticmethod
    def _valid_metadata(value: object) -> bool:
        if type(value) is not ClaimMetadata:
            return False
        text_consistent = (
            value.generated_text is None
            if value.action in {"view", "like"}
            else (
                type(value.generated_text) is _RedactedText
                and bool(value.generated_text.strip())
                and len(value.generated_text) <= 200
            )
        )
        return (
            type(value.user_id) is UUID
            and type(value.ig_account_id) is UUID
            and type(value.target_id) is UUID
            and type(value.fingerprint) is str
            and re.fullmatch(r"[0-9a-f]{64}", value.fingerprint) is not None
            and value.action in {"view", "like", "reply", "dm"}
            and text_consistent
            and value.model_used
            in {
                "claude-haiku-4-5",
                "claude-sonnet-4-6",
                "claude-opus-4-8",
                "gpt-5.6-terra",
                "engagemate-agent",
            }
            and type(value.rationale) is _RedactedText
            and bool(value.rationale.strip())
            and len(value.rationale) <= 1_000
            and type(value.confidence) is float
            and math.isfinite(value.confidence)
            and 0 <= value.confidence <= 1
        )

    def _claim_sync(self, metadata: ClaimMetadata) -> ClaimResult:
        params = {
            "user_id": metadata.user_id,
            "account_id": metadata.ig_account_id,
            "target_id": metadata.target_id,
            "fingerprint": metadata.fingerprint,
            "action": metadata.action,
            "generated_text": metadata.generated_text,
            "model_used": metadata.model_used,
            "rationale": metadata.rationale,
            "confidence": metadata.confidence,
        }
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    """
                    SELECT pg_advisory_xact_lock(
                        hashtextextended(
                            CAST(%(account_id)s AS text) || ':' || %(fingerprint)s,
                            0
                        )
                    ) AS locked
                    """,
                    params,
                ).fetchone()
                target = connection.execute(
                    """
                    SELECT id FROM targets
                    WHERE id = %(target_id)s
                      AND user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND enabled = true
                    """,
                    params,
                ).fetchone()
                if target is None:
                    raise RepositoryError("execution target unavailable")
                existing = connection.execute(
                    """
                    SELECT id FROM story_engagements
                    WHERE user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND story_fingerprint = %(fingerprint)s
                    LIMIT 1
                    """,
                    params,
                ).fetchone()
                if existing is not None:
                    return ClaimResult(existing["id"], True)
                if metadata.action != "view":
                    connection.execute(
                        """
                        SELECT pg_advisory_xact_lock(
                            hashtextextended(CAST(%(user_id)s AS text), 0)
                        ) AS locked
                        """,
                        params,
                    ).fetchone()
                inserted = connection.execute(
                    """
                    INSERT INTO story_engagements (
                        user_id, ig_account_id, target_id, story_fingerprint,
                        action, generated_text, model_used, rationale,
                        confidence, status
                    ) VALUES (
                        %(user_id)s, %(account_id)s, %(target_id)s,
                        %(fingerprint)s, %(action)s, %(generated_text)s,
                        %(model_used)s, %(rationale)s, %(confidence)s, 'skipped'
                    )
                    RETURNING id
                    """,
                    params,
                ).fetchone()
                if inserted is None:
                    raise RepositoryError("execution claim unavailable")
                if metadata.action != "view":
                    reservation = connection.execute(
                        """
                        INSERT INTO engagement_usage_events (
                            user_id, ig_account_id, engagement_id,
                            surface, action, quantity, status
                        ) VALUES (
                            %(user_id)s, %(account_id)s, %(engagement_id)s,
                            'story', %(action)s, 1, 'reserved'
                        )
                        RETURNING engagement_id
                        """,
                        {**params, "engagement_id": inserted["id"]},
                    ).fetchone()
                    if reservation is None:
                        raise RepositoryError("usage reservation unavailable")
                return ClaimResult(inserted["id"], False)
        except UniqueViolation:
            return self._duplicate_after_conflict(metadata)

    def _duplicate_after_conflict(self, metadata: ClaimMetadata) -> ClaimResult:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT id FROM story_engagements
                WHERE user_id = %(user_id)s
                  AND ig_account_id = %(account_id)s
                  AND story_fingerprint = %(fingerprint)s
                LIMIT 1
                """,
                {
                    "user_id": metadata.user_id,
                    "account_id": metadata.ig_account_id,
                    "fingerprint": metadata.fingerprint,
                },
            ).fetchone()
        if row is None:
            raise RepositoryError("execution claim conflict")
        return ClaimResult(row["id"], True)

    def _finish_sync(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        engagement_id: UUID,
        sent: bool,
        failure_code: str | None,
        skipped: bool = False,
    ) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                WITH finished_engagement AS (
                    UPDATE story_engagements
                    SET status = %(status)s,
                        failure_code = %(failure_code)s,
                        executed_at = CASE
                            WHEN %(status)s = 'sent' THEN now()
                            ELSE NULL
                        END
                    WHERE id = %(engagement_id)s
                      AND user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND status = 'skipped'
                      AND failure_code IS NULL
                      AND (
                          action = 'view'
                          OR EXISTS (
                              SELECT 1
                              FROM engagement_usage_events
                              WHERE engagement_id = %(engagement_id)s
                                AND user_id = %(user_id)s
                                AND ig_account_id = %(account_id)s
                                AND status = 'reserved'
                          )
                      )
                    RETURNING id, action, executed_at
                ), finalized_usage AS (
                    UPDATE engagement_usage_events
                    SET status = %(usage_status)s,
                        finalized_at = now(),
                        occurred_at = CASE
                            WHEN %(usage_status)s = 'billable'
                            THEN (SELECT executed_at FROM finished_engagement)
                            ELSE NULL
                        END
                    WHERE engagement_id = (SELECT id FROM finished_engagement)
                      AND user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND status = 'reserved'
                    RETURNING engagement_id
                )
                SELECT id AS engagement_id
                FROM finished_engagement
                WHERE action = 'view'
                   OR EXISTS (SELECT 1 FROM finalized_usage)
                """,
                {
                    "status": "sent" if sent else "skipped" if skipped else "failed",
                    "usage_status": "billable" if sent else "released",
                    "failure_code": failure_code,
                    "engagement_id": engagement_id,
                    "user_id": user_id,
                    "account_id": ig_account_id,
                },
            ).fetchone()
        return row is not None

    def _persist_breaker_sync(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        decision: BreakerDecision,
    ) -> None:
        if not decision.stop:
            raise ValueError("breaker persistence requires a stop decision")
        params = {
            "user_id": user_id,
            "account_id": ig_account_id,
            "event_type": decision.reason_code,
            "health_status": decision.health_status.value,
            "pause_until": (
                decision.pause_until.isoformat()
                if decision.pause_until is not None
                else None
            ),
            "paused_at": decision.observed_at,
        }
        with self._pool.connection() as connection:
            owned = connection.execute(
                """
                SELECT id FROM ig_accounts
                WHERE id = %(account_id)s AND user_id = %(user_id)s
                FOR UPDATE
                """,
                params,
            ).fetchone()
            if owned is None:
                raise RepositoryError("Instagram account unavailable")
            connection.execute(
                """
                INSERT INTO account_health_events (
                    user_id, ig_account_id, event_type, severity, evidence
                ) VALUES (
                    %(user_id)s, %(account_id)s, %(event_type)s, 'warning',
                    jsonb_build_object(
                        'signal', CAST(%(event_type)s AS text),
                        'pause_until', CAST(%(pause_until)s AS text)
                    )
                )
                """,
                params,
            )
            updated = connection.execute(
                """
                UPDATE ig_accounts
                SET health_status = %(health_status)s,
                    paused_at = COALESCE(paused_at, %(paused_at)s),
                    pause_reason = %(event_type)s,
                    updated_at = now()
                WHERE id = %(account_id)s AND user_id = %(user_id)s
                RETURNING id
                """,
                params,
            ).fetchone()
            if updated is None:
                raise RepositoryError("Instagram account unavailable")
            connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', updated_at = now()
                WHERE user_id = %(user_id)s
                  AND ig_account_id = %(account_id)s
                  AND status = 'queued'
                """,
                params,
            )
