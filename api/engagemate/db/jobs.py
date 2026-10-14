"""Transactional PostgreSQL repository for leased worker jobs."""

from __future__ import annotations

import re
from uuid import UUID

from engagemate.domain.models import ClaimedJob, JobStatus
from psycopg_pool import ConnectionPool

_CLAIM_NEXT_SQL = """
WITH candidate AS (
    SELECT id
    FROM jobs
    WHERE ((
        status = 'queued' AND scheduled_for <= now()
    ) OR (
        status = 'running' AND lease_expires_at < now()
    ))
      AND coalesce(schedule_key, '') NOT LIKE 'operator-%%'
    ORDER BY scheduled_for, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE jobs
SET status = 'running',
    claimed_by = %(worker_id)s,
    lease_expires_at = now() + (%(lease_seconds)s * interval '1 second'),
    heartbeat_at = now(),
    attempts = attempts + 1,
    updated_at = now()
FROM candidate
WHERE jobs.id = candidate.id
RETURNING jobs.*;
"""


class JobRepository:
    """Claim and mutate jobs while enforcing lease ownership."""

    _TERMINAL_STATUSES = frozenset(
        {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    )

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        lease_seconds: int = 120,
        serialize_account_actions: bool = False,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if type(serialize_account_actions) is not bool:
            raise ValueError("serialize_account_actions must be boolean")
        self.pool = pool
        self.lease_seconds = lease_seconds
        self.serialize_account_actions = serialize_account_actions

    def claim_exact(
        self, job_id: UUID, user_id: UUID, account_id: UUID, worker_id: str
    ) -> ClaimedJob | None:
        """Claim one fresh owned job; refuse recovery and busy account lanes."""

        if any(type(value) is not UUID for value in (job_id, user_id, account_id)):
            raise ValueError("exact job scope requires UUID identifiers")
        if type(worker_id) is not str or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", worker_id
        ):
            raise ValueError("invalid exact job worker id")
        params = {
            "job_id": job_id, "user_id": user_id, "account_id": account_id,
            "worker_id": worker_id, "lease_seconds": self.lease_seconds,
        }
        with self.pool.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            account = connection.execute(
                """SELECT a.id FROM ig_accounts a
                   JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
                   WHERE a.id=%(account_id)s AND a.user_id=%(user_id)s
                     AND a.health_status='active' AND a.paused_at IS NULL AND c.enabled
                     AND EXISTS (SELECT 1 FROM jobs j WHERE j.id=%(job_id)s
                       AND j.user_id=a.user_id AND j.ig_account_id=a.id
                       AND j.status='queued' AND j.attempts=0 AND j.scheduled_for<=now()
                       AND j.claimed_by IS NULL AND j.lease_expires_at IS NULL)
                   FOR UPDATE OF a SKIP LOCKED""",
                params,
            ).fetchone()
            if account is None:
                return None
            # Account claims in other lanes use this same lock. Recheck their
            # committed leases in a fresh snapshot after acquiring it.
            row = connection.execute(
                """WITH candidate AS (
                    SELECT j.id FROM jobs j
                    JOIN ig_accounts a ON a.id=j.ig_account_id AND a.user_id=j.user_id
                    JOIN agent_configs c ON c.ig_account_id=a.id AND c.user_id=a.user_id
                    WHERE j.id=%(job_id)s AND a.id=%(account_id)s AND a.user_id=%(user_id)s
                      AND a.health_status='active' AND a.paused_at IS NULL AND c.enabled
                      AND j.status='queued' AND j.attempts=0 AND j.scheduled_for<=now()
                      AND j.claimed_by IS NULL AND j.lease_expires_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM jobs active
                        WHERE active.ig_account_id=a.id AND active.user_id=a.user_id
                          AND active.status='running' AND active.lease_expires_at>now())
                      AND NOT EXISTS (SELECT 1 FROM relationship_follows f
                        WHERE f.ig_account_id=a.id AND f.user_id=a.user_id
                          AND f.status IN ('follow_running','unfollow_running')
                          AND f.lease_expires_at>now())
                      AND NOT EXISTS (SELECT 1 FROM follow_reel_messages m
                        WHERE m.ig_account_id=a.id AND m.user_id=a.user_id
                          AND m.status='running' AND m.lease_expires_at>now())
                      AND NOT EXISTS (SELECT 1 FROM relationship_reconciliation_attempts r
                        WHERE r.ig_account_id=a.id AND r.user_id=a.user_id
                          AND r.outcome='pending' AND r.lease_expires_at>now())
                    FOR UPDATE OF j SKIP LOCKED)
                UPDATE jobs SET status='running',claimed_by=%(worker_id)s,
                    lease_expires_at=now()+(%(lease_seconds)s*interval '1 second'),
                    heartbeat_at=now(),attempts=attempts+1,updated_at=now()
                FROM candidate WHERE jobs.id=candidate.id RETURNING jobs.*""",
                params,
            ).fetchone()
        return ClaimedJob.from_row(row) if row is not None else None

    def claim_next(self, worker_id: str) -> ClaimedJob | None:
        """Atomically claim one due or expired job without blocking peers."""

        if not worker_id:
            raise ValueError("worker_id must not be empty")

        if self.serialize_account_actions:
            return self._claim_serialized(worker_id)
        with self.pool.connection() as connection:
            row = connection.execute(
                _CLAIM_NEXT_SQL,
                {
                    "worker_id": worker_id,
                    "lease_seconds": self.lease_seconds,
                },
            ).fetchone()

        return ClaimedJob.from_row(row) if row is not None else None

    def _claim_serialized(self, worker_id: str) -> ClaimedJob | None:
        # Relationship claims lock this same account row. The second SQL
        # statement takes a fresh READ COMMITTED snapshot after acquiring it.
        # A single CTE could retain a snapshot from before another claim committed.
        available = """
            NOT EXISTS (SELECT 1 FROM relationship_follows f
                WHERE f.ig_account_id=a.id AND f.user_id=a.user_id
                  AND f.status IN ('follow_running','unfollow_running')
                  AND f.lease_expires_at > now())
            AND NOT EXISTS (SELECT 1 FROM follow_reel_messages m
                WHERE m.ig_account_id=a.id AND m.user_id=a.user_id
                  AND m.status='running' AND m.lease_expires_at>now())
            AND NOT EXISTS (SELECT 1 FROM relationship_reconciliation_attempts observation
                WHERE observation.ig_account_id=a.id AND observation.user_id=a.user_id
                  AND observation.outcome='pending' AND observation.lease_expires_at > now())
            AND NOT EXISTS (SELECT 1 FROM jobs active
                WHERE active.ig_account_id=a.id AND active.user_id=a.user_id
                  AND active.status='running' AND active.lease_expires_at > now())
        """
        with self.pool.connection() as connection, connection.transaction():
            account = connection.execute(
                """SELECT a.id,a.user_id FROM ig_accounts a
                   WHERE EXISTS (SELECT 1 FROM jobs j
                     WHERE j.ig_account_id=a.id AND j.user_id=a.user_id
                       AND ((j.status='queued' AND j.scheduled_for<=now())
                         OR (j.status='running' AND j.lease_expires_at<now()))
                       AND coalesce(j.schedule_key, '') NOT LIKE 'operator-%%')
                     AND """ + available + """
                   ORDER BY a.id FOR UPDATE OF a SKIP LOCKED LIMIT 1""",
            ).fetchone()
            if account is None:
                return None
            row = connection.execute(
                """WITH candidate AS (
                    SELECT j.id FROM jobs j JOIN ig_accounts a
                      ON a.id=j.ig_account_id AND a.user_id=j.user_id
                    WHERE a.id=%(account_id)s AND a.user_id=%(user_id)s
                      AND ((j.status='queued' AND j.scheduled_for<=now())
                        OR (j.status='running' AND j.lease_expires_at<now()))
                      AND coalesce(j.schedule_key, '') NOT LIKE 'operator-%%'
                      AND """ + available + """
                    ORDER BY j.scheduled_for,j.created_at
                    FOR UPDATE OF j SKIP LOCKED LIMIT 1)
                UPDATE jobs SET status='running',claimed_by=%(worker_id)s,
                    lease_expires_at=now()+(%(lease_seconds)s*interval '1 second'),
                    heartbeat_at=now(),attempts=attempts+1,updated_at=now()
                FROM candidate WHERE jobs.id=candidate.id RETURNING jobs.*""",
                {"account_id": account["id"], "user_id": account["user_id"],
                 "worker_id": worker_id, "lease_seconds": self.lease_seconds},
            ).fetchone()
        return ClaimedJob.from_row(row) if row is not None else None

    def heartbeat(self, job_id: UUID, worker_id: str) -> bool:
        """Extend a live lease only for its current worker."""

        with self.pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE jobs
                SET heartbeat_at = now(),
                    lease_expires_at =
                        now() + (%(lease_seconds)s * interval '1 second'),
                    updated_at = now()
                WHERE id = %(job_id)s
                  AND claimed_by = %(worker_id)s
                  AND status = 'running'
                  AND lease_expires_at > now()
                RETURNING id
                """,
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "lease_seconds": self.lease_seconds,
                },
            ).fetchone()
        return row is not None

    def finish(
        self,
        job_id: UUID,
        worker_id: str,
        status: JobStatus,
        error: str | None = None,
    ) -> bool:
        """Finish a running job only when the caller still owns its lease."""

        if status not in self._TERMINAL_STATUSES:
            raise ValueError(f"finish requires a terminal job status, got {status}")

        with self.pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE jobs
                SET status = %(status)s,
                    last_error = %(error)s,
                    lease_expires_at = NULL,
                    updated_at = now()
                WHERE id = %(job_id)s
                  AND claimed_by = %(worker_id)s
                  AND status = 'running'
                  AND lease_expires_at > now()
                RETURNING id
                """,
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "status": status.value,
                    "error": error,
                },
            ).fetchone()
        return row is not None

    def defer(
        self,
        job_id: UUID,
        worker_id: str,
        error: str,
        *,
        delay_seconds: int = 300,
    ) -> bool:
        """Return one transiently blocked job to the queue for a later retry."""

        if not error or delay_seconds <= 0:
            raise ValueError("defer requires an error and positive delay")
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE jobs
                SET status = 'queued',
                    claimed_by = NULL,
                    lease_expires_at = NULL,
                    heartbeat_at = NULL,
                    scheduled_for = now() + (%(delay)s * interval '1 second'),
                    last_error = %(error)s,
                    updated_at = now()
                WHERE id = %(job_id)s
                  AND claimed_by = %(worker_id)s
                  AND status = 'running'
                  AND lease_expires_at > now()
                RETURNING id
                """,
                {
                    "job_id": job_id,
                    "worker_id": worker_id,
                    "error": error,
                    "delay": delay_seconds,
                },
            ).fetchone()
        return row is not None
