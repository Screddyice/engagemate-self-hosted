"""Tenant-scoped policy and audit transactions for private execution tools."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from engagemate.db.text import text

_DISCOVERY_RESTRICTION_SIGNALS = {
    "rate_limited": "rate_limit",
    "checkpoint_required": "checkpoint",
    "authentication_required": "login_required",
}


def recovered_job_status(run_status, stop_reason):
    if run_status == "cancelled":
        return "cancelled"
    if run_status == "completed" and stop_reason not in {
        "safety_stop",
        "account_unhealthy",
    }:
        return "completed"
    return "failed"


class ExecutionMcpRepository:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def reconcile_abandoned_runs(self) -> int:
        """Close expired executions before the job queue can replay them.

        A crash can leave an audit run open after its job lease expires or its
        job finishes. Preserve completed receipts and mark unfinished writes
        uncertain: starting a new browser session cannot establish their outcome.
        Jobs without an execution run keep the queue's normal retry behavior.
        """
        return await asyncio.to_thread(self._reconcile_abandoned_runs)

    def _reconcile_abandoned_runs(self) -> int:
        with self._pool.connection() as connection, connection.transaction():
            rows = connection.execute(
                """
                SELECT j.id AS job_id, j.status AS job_status,
                       r.id AS run_id, r.status AS run_status, r.stop_reason
                FROM jobs j JOIN hermes_execution_runs r
                  ON r.job_id=j.id AND r.user_id=j.user_id
                 AND r.ig_account_id=j.ig_account_id
                WHERE (j.status='running' AND j.lease_expires_at<=now())
                   OR (j.status IN ('completed','failed','cancelled')
                       AND (r.status IN ('starting','running') OR EXISTS (
                           SELECT 1 FROM hermes_tool_calls tc
                           WHERE tc.run_id=r.id AND tc.status='started')))
                ORDER BY j.updated_at, j.id
                LIMIT 25
                FOR UPDATE OF j, r SKIP LOCKED
                """
            ).fetchall()
            for row in rows:
                interrupted = row["run_status"] in ("starting", "running")
                reason = "worker_interrupted" if interrupted else row["stop_reason"]
                if interrupted:
                    connection.execute(
                        """UPDATE hermes_execution_runs
                           SET status='failed',stop_reason='worker_interrupted',completed_at=now()
                           WHERE id=%s""",
                        (row["run_id"],),
                    )
                connection.execute(
                    """UPDATE hermes_tool_calls
                       SET status='uncertain',failure_class='worker_interrupted',
                           safe_result='{"status":"uncertain","reason":"worker_interrupted"}'::jsonb,
                           completed_at=now()
                       WHERE run_id=%s AND status='started'""",
                    (row["run_id"],),
                )
                if row["job_status"] == "running":
                    status = recovered_job_status(row["run_status"], reason)
                    connection.execute(
                        """UPDATE jobs SET status=%s,last_error=%s,
                           lease_expires_at=NULL,updated_at=now() WHERE id=%s""",
                        (status, reason or status, row["job_id"]),
                    )
        return len(rows)

    async def discovery_stop_reason(
        self, run: Any, *, action: str | None = None
    ) -> str | None:
        """Recheck launch, health, controls and exact source approval between reads."""
        sources = run.config_snapshot.get("follower_sources", [])
        return await asyncio.to_thread(
            self._discovery_stop_reason,
            {
                "user_id": run.context.user_id,
                "account_id": run.context.ig_account_id,
                "run_id": run.run_id,
                "action": action,
                "source_ids": [UUID(source["id"]) for source in sources],
            },
        )

    def _discovery_stop_reason(self, params: dict) -> str | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT a.health_status, a.paused_at, c.enabled,
                       EXISTS(SELECT 1 FROM hermes_execution_runs active_run
                              WHERE active_run.id=%(run_id)s
                                AND active_run.user_id=a.user_id
                                AND active_run.ig_account_id=a.id
                                AND active_run.status='running') AS run_live,
                       ARRAY(SELECT r.id FROM brand_reference_accounts r
                             JOIN brand_profiles p ON p.id=r.brand_profile_id
                             WHERE p.user_id=a.user_id AND p.ig_account_id=a.id
                               AND p.status='complete'
                               AND p.follower_audience_approved_at IS NOT NULL) AS sources,
                       EXISTS(SELECT 1 FROM instagram_execution_controls x
                              WHERE x.blocked AND (x.scope='system'
                                OR (x.scope='action' AND x.action=%(action)s)
                                OR (x.scope='tenant' AND x.user_id=a.user_id)
                                OR (x.scope='account' AND x.user_id=a.user_id
                                    AND x.ig_account_id=a.id))) AS blocked
                FROM ig_accounts a JOIN agent_configs c
                  ON c.user_id=a.user_id AND c.ig_account_id=a.id
                WHERE a.user_id=%(user_id)s AND a.id=%(account_id)s
            """,
                params,
            ).fetchone()
        if row is None or row["enabled"] is not True:
            return "account_disabled"
        if not row["run_live"]:
            return "run_unavailable"
        if row["paused_at"] is not None or row["health_status"] not in ("active", b"active"):
            return "account_unhealthy"
        if row["blocked"]:
            return "safety_stop"
        if not params["source_ids"] or set(row["sources"]) != set(params["source_ids"]):
            return "source_changed"
        return None

    async def record_discovery(
        self,
        *,
        run_id: UUID,
        user_id: UUID,
        ig_account_id: UUID,
        report: dict,
    ) -> None:
        await asyncio.to_thread(
            self._record_discovery,
            {
                "run_id": run_id,
                "user_id": user_id,
                "account_id": ig_account_id,
                "report": Jsonb(report),
                "signal": _DISCOVERY_RESTRICTION_SIGNALS.get(report.get("stop_reason")),
                "evidence": Jsonb(
                    {
                        "source": "follower_discovery",
                        "run_id": str(run_id),
                        "reason": report.get("stop_reason"),
                    }
                ),
            },
        )

    def _record_discovery(self, params: dict) -> None:
        with self._pool.connection() as connection:
            recorded = connection.execute(
                """
                UPDATE hermes_execution_runs SET discovery_report=%(report)s
                WHERE id=%(run_id)s AND user_id=%(user_id)s
                  AND ig_account_id=%(account_id)s AND status='running'
                RETURNING id
                """,
                params,
            ).fetchone()
            if recorded is None or params["signal"] is None:
                return
            # The report, pause, and restriction signal commit together. Only
            # an active/degraded -> paused transition emits an event, so repeated
            # reports neither duplicate signals nor move the disconnect time.
            connection.execute(
                """
                WITH paused AS (
                    UPDATE ig_accounts
                    SET health_status='paused', paused_at=now(),
                        pause_reason=%(signal)s, updated_at=now()
                    WHERE user_id=%(user_id)s AND id=%(account_id)s
                      AND health_status IN ('active', 'degraded')
                    RETURNING user_id, id
                )
                INSERT INTO account_health_events (
                    user_id, ig_account_id, event_type, severity, evidence
                )
                SELECT user_id, id, %(signal)s, 'warning', %(evidence)s FROM paused
                """,
                params,
            )

    async def start_run(self, **values: Any) -> bool:
        return await asyncio.to_thread(self._start_run, values)

    def _start_run(self, values: dict[str, Any]) -> bool:
        params = {
            **values,
            "account_id": values["ig_account_id"],
            "config_snapshot": Jsonb(dict(values["config_snapshot"])),
        }
        with self._pool.connection() as connection, connection.transaction():
            inserted = connection.execute(
                """
                INSERT INTO hermes_execution_runs (
                    id, job_id, user_id, ig_account_id, hermes_session_id,
                    config_version, config_snapshot, status, started_at
                )
                SELECT
                    %(run_id)s, job.id, job.user_id, job.ig_account_id,
                    %(hermes_session_id)s, %(config_version)s,
                    %(config_snapshot)s, 'running', %(started_at)s
                FROM jobs job
                WHERE job.id = %(job_id)s
                  AND job.user_id = %(user_id)s
                  AND job.ig_account_id = %(account_id)s
                  AND job.claimed_by = %(worker_id)s
                  AND job.status = 'running'
                  AND job.lease_expires_at > %(started_at)s
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                params,
            ).fetchone()
        return inserted is not None

    async def fail_run(self, **values: Any) -> bool:
        return await asyncio.to_thread(self._fail_run, values)

    async def can_retry_without_external_write(self, run_id: UUID) -> bool:
        """Return true only when this run has no non-denied action receipt.

        A Hermes transport failure can be retried once when admission failed
        before Instagram was touched. Any started, confirmed, failed, or
        uncertain action remains held for reconciliation instead of risking a
        duplicate external write.
        """

        return await asyncio.to_thread(self._can_retry_without_external_write, run_id)

    def _can_retry_without_external_write(self, run_id: UUID) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT NOT EXISTS (
                    SELECT 1
                    FROM hermes_tool_calls
                    WHERE run_id = %s
                      AND tool_name IN ('view', 'like', 'reply', 'dm')
                      AND status <> 'denied'
                ) AS safe
                """,
                (run_id,),
            ).fetchone()
        return bool(row and row["safe"] is True)

    def _fail_run(self, values: dict[str, Any]) -> bool:
        with self._pool.connection() as connection:
            updated = connection.execute(
                """
                UPDATE hermes_execution_runs
                SET status = 'failed', stop_reason = %(reason)s,
                    completed_at = %(completed_at)s
                WHERE id = %(run_id)s AND status IN ('starting', 'running')
                RETURNING id
                """,
                values,
            ).fetchone()
        return updated is not None

    async def begin_mutation(self, **values: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._begin_mutation, values)

    def _begin_mutation(self, values: dict[str, Any]) -> dict[str, Any]:
        params = {
            "run_id": values["run_id"],
            "job_id": values["job_id"],
            "user_id": values["user_id"],
            "account_id": values["ig_account_id"],
            "worker_id": values["worker_id"],
            "opportunity_id": values["opportunity_id"],
            "target_id": values["target_id"],
            "fingerprint": values["story_fingerprint"],
            "action": values["action"],
            "idempotency_key": values["idempotency_key"],
            "generated_text_hash": values["generated_text_hash"],
            "proposed_text": values.get("proposed_text"),
            "now": values["now"],
            "mode": values["execution_mode"],
        }
        with self._pool.connection() as connection, connection.transaction():
            # Separate jobs on one account share one unresolved-Story guard.
            account = connection.execute(
                """SELECT id, health_status, paused_at FROM ig_accounts
                   WHERE id=%(account_id)s AND user_id=%(user_id)s FOR UPDATE""",
                params,
            ).fetchone()
            if account is None:
                return {"allowed": False, "reason": "account_unavailable"}
            inserted = connection.execute(
                """
                INSERT INTO hermes_tool_calls (
                    run_id, tool_name, opportunity_id, target_id,
                    story_fingerprint, generated_text_hash, proposed_text,
                    idempotency_key, status, safe_result
                ) VALUES (
                    %(run_id)s, %(action)s, %(opportunity_id)s, %(target_id)s,
                    %(fingerprint)s, %(generated_text_hash)s, %(proposed_text)s,
                    %(idempotency_key)s, 'started', '{"status":"started","phase":"reserved"}'::jsonb
                )
                ON CONFLICT (run_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                params,
            ).fetchone()
            if inserted is None:
                existing = connection.execute(
                    """
                    SELECT status, failure_class, safe_result
                    FROM hermes_tool_calls
                    WHERE run_id = %(run_id)s
                      AND idempotency_key = %(idempotency_key)s
                    """,
                    params,
                ).fetchone()
                safe_result = None if existing is None else existing["safe_result"]
                if existing is None or existing["status"] == "started":
                    return {
                        "allowed": False,
                        "replay": True,
                        "reason": "idempotency_in_progress",
                        "result": {
                            "status": "uncertain",
                            "reason": "idempotency_in_progress",
                        },
                    }
                return {
                    "allowed": False,
                    "replay": True,
                    "reason": "idempotency_replay",
                    "result": dict(safe_result),
                }

            if account["paused_at"] is not None or text(account["health_status"]) != "active":
                return self._deny(connection, params, "account_unhealthy")

            lease = connection.execute(
                """
                SELECT id FROM jobs
                WHERE id = %(job_id)s
                  AND user_id = %(user_id)s
                  AND ig_account_id = %(account_id)s
                  AND claimed_by = %(worker_id)s
                  AND status = 'running'
                  AND lease_expires_at > %(now)s
                FOR UPDATE
                """,
                params,
            ).fetchone()
            if lease is None:
                return self._deny(connection, params, "lease_lost")
            prior = connection.execute(
                """SELECT tc.status FROM hermes_tool_calls tc
                   JOIN hermes_execution_runs r ON r.id=tc.run_id
                   WHERE r.user_id=%(user_id)s AND r.ig_account_id=%(account_id)s
                     AND tc.story_fingerprint=%(fingerprint)s
                     AND tc.status IN ('started','uncertain','confirmed') AND tc.id<>%(call_id)s
                   LIMIT 1""",
                {**params, "call_id": inserted["id"]},
            ).fetchone()
            if prior is not None:
                return self._deny(connection, params, "unresolved_story_action" if text(prior["status"]) != "confirmed" else "duplicate_story")
            if params["mode"] == "disabled":
                return self._deny(connection, params, "execution_disabled")

            blocked = connection.execute(
                """
                SELECT reason_code
                FROM instagram_execution_controls
                WHERE blocked = true AND (
                    scope = 'system'
                    OR (scope = 'action' AND action = %(action)s)
                    OR (scope = 'tenant' AND user_id = %(user_id)s)
                    OR (
                        scope = 'account'
                        AND user_id = %(user_id)s
                        AND ig_account_id = %(account_id)s
                    )
                )
                ORDER BY CASE scope
                    WHEN 'system' THEN 1
                    WHEN 'action' THEN 2
                    WHEN 'tenant' THEN 3
                    ELSE 4
                END
                LIMIT 1
                """,
                params,
            ).fetchone()
            if blocked is not None:
                return self._deny(connection, params, str(blocked["reason_code"]))

            if params["mode"] == "canary":
                approval = connection.execute(
                    """
                    SELECT id FROM live_action_approvals
                    WHERE user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND target_id = %(target_id)s
                      AND story_fingerprint = %(fingerprint)s
                      AND action = %(action)s
                      AND generated_text_hash IS NOT DISTINCT FROM %(generated_text_hash)s
                      AND consumed_at IS NULL
                      AND expires_at > %(now)s
                    ORDER BY approved_at
                    LIMIT 1
                    FOR UPDATE
                    """,
                    params,
                ).fetchone()
                if approval is None:
                    return self._deny(connection, params, "canary_approval_required")
                connection.execute(
                    """
                    UPDATE live_action_approvals
                    SET consumed_at = %(now)s
                    WHERE id = %(approval_id)s AND consumed_at IS NULL
                    """,
                    {**params, "approval_id": approval["id"]},
                )
            elif params["mode"] != "autonomous":
                return self._deny(connection, params, "invalid_execution_mode")

            return {"allowed": True, "replay": False}

    @staticmethod
    def _deny(connection: Any, params: dict[str, Any], reason: str) -> dict[str, Any]:
        safe = {"status": "denied", "reason": reason}
        connection.execute(
            """
            UPDATE hermes_tool_calls
            SET status = 'denied', failure_class = %(reason)s,
                safe_result = %(safe_result)s, completed_at = %(now)s
            WHERE run_id = %(run_id)s AND idempotency_key = %(idempotency_key)s
            """,
            {**params, "reason": reason, "safe_result": Jsonb(safe)},
        )
        return {"allowed": False, "replay": False, "reason": reason}

    async def finish_mutation(self, **values: Any) -> None:
        await asyncio.to_thread(self._finish_mutation, values)

    def _finish_mutation(self, values: dict[str, Any]) -> None:
        safe_result = dict(values["safe_result"])
        with self._pool.connection() as connection:
            connection.execute(
                """
                UPDATE hermes_tool_calls
                SET status = %(status)s, failure_class = %(failure_class)s,
                    safe_result = %(safe_result)s, completed_at = %(completed_at)s
                WHERE run_id = %(run_id)s
                  AND idempotency_key = %(idempotency_key)s
                  AND (status = 'started' OR (status='uncertain'
                       AND failure_class='worker_interrupted' AND %(status)s='confirmed'))
                """,
                {
                    **values,
                    "safe_result": Jsonb(safe_result),
                },
            )

    async def mark_execution_started(self, **values: Any) -> bool:
        return await asyncio.to_thread(self._mark_execution_started, values)

    def _mark_execution_started(self, values: dict[str, Any]) -> bool:
        with self._pool.connection() as connection, connection.transaction():
            # Acquire the call lock before evaluating current lease time. An
            # UPDATE predicate can otherwise run before waiting for that lock.
            connection.execute(
                "SELECT id FROM hermes_tool_calls WHERE run_id=%(run_id)s "
                "AND idempotency_key=%(idempotency_key)s FOR UPDATE", values,
            ).fetchone()
            row = connection.execute(
                """UPDATE hermes_tool_calls tc
                   SET safe_result=jsonb_build_object('status','started','phase','execution_started',
                       'phase_at',clock_timestamp())
                   FROM hermes_execution_runs r JOIN jobs j ON j.id=r.job_id
                     AND j.user_id=r.user_id AND j.ig_account_id=r.ig_account_id
                   WHERE tc.run_id=%(run_id)s AND r.id=tc.run_id
                     AND tc.idempotency_key=%(idempotency_key)s AND tc.status='started'
                     AND tc.safe_result->>'phase'='reserved'
                     AND r.status='running' AND j.status='running'
                     AND j.claimed_by=%(worker_id)s
                     AND r.user_id=%(user_id)s AND r.ig_account_id=%(account_id)s
                     AND j.lease_expires_at>clock_timestamp()
                   RETURNING tc.id""", values,
            ).fetchone()
        return row is not None

    async def complete_run(self, **values: Any) -> dict[str, int]:
        return await asyncio.to_thread(self._complete_run, values)

    def _complete_run(self, values: dict[str, Any]) -> dict[str, int]:
        run_id: UUID = values["run_id"]
        reason = values["reason"]
        completed_at: datetime = values["completed_at"]
        with self._pool.connection() as connection, connection.transaction():
            connection.execute(
                """
                UPDATE hermes_execution_runs
                SET status = 'completed', stop_reason = %s, completed_at = %s
                WHERE id = %s AND status IN ('starting', 'running')
                """,
                (reason, completed_at, run_id),
            )
            row = connection.execute(
                """
                SELECT
                    count(*) FILTER (WHERE tool_name <> 'complete_run') AS considered,
                    count(*) FILTER (WHERE status = 'confirmed') AS confirmed,
                    count(*) FILTER (WHERE status = 'failed') AS failed,
                    count(*) FILTER (
                        WHERE status = 'denied'
                          AND (
                            failure_class IS NULL
                            OR left(failure_class, 10) <> 'text_gate_'
                          )
                    ) AS denied,
                    count(*) FILTER (
                        WHERE status = 'denied'
                          AND left(failure_class, 10) = 'text_gate_'
                    ) AS gated,
                    count(*) FILTER (WHERE status = 'uncertain') AS uncertain
                FROM hermes_tool_calls
                WHERE run_id = %s
                """,
                (run_id,),
            ).fetchone()
        return {
            key: int(row[key] or 0)
            for key in (
                "considered",
                "confirmed",
                "failed",
                "denied",
                "gated",
                "uncertain",
            )
        }
