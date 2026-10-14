from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg
import pytest

from engagemate.db.executions import ClaimMetadata, ExecutionRepository
from engagemate.db.pool import create_pool
from engagemate.worker.execution_mcp_repository import ExecutionMcpRepository


@pytest.fixture(scope="module")
def pool():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is unavailable")
    with psycopg.connect(database_url) as connection:
        migrated = connection.execute(
            "SELECT to_regclass('public.hermes_execution_runs') AS table_name"
        ).fetchone()
    if migrated is None or migrated[0] is None:
        pytest.skip("TEST_DATABASE_URL does not have migration 0014")
    value = create_pool(database_url, min_size=1, max_size=2)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture()
def world(pool):
    ids = {
        "user_id": uuid4(),
        "account_id": uuid4(),
        "target_id": uuid4(),
        "job_id": uuid4(),
        "run_id": uuid4(),
    }
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO users (id, owner_key, email, plan)
            VALUES (%(user_id)s, %(owner_key)s, %(email)s, 'pro')
            """,
            {
                **ids,
                "owner_key": f"clerk-{ids['user_id']}",
                "email": f"{ids['user_id']}@example.test",
            },
        )
        connection.execute(
            """
            INSERT INTO ig_accounts (id, user_id, username)
            VALUES (%(account_id)s, %(user_id)s, %(username)s)
            """,
            {**ids, "username": f"account_{ids['account_id'].hex}"},
        )
        connection.execute(
            """
            INSERT INTO targets (
                id, user_id, ig_account_id, username, tier, source
            ) VALUES (
                %(target_id)s, %(user_id)s, %(account_id)s,
                %(username)s, 'vip', 'manual'
            )
            """,
            {**ids, "username": f"target_{ids['target_id'].hex}"},
        )
        connection.execute(
            """
            INSERT INTO jobs (
                id, user_id, ig_account_id, schedule_key, scheduled_for,
                status, claimed_by, lease_expires_at, heartbeat_at
            ) VALUES (
                %(job_id)s, %(user_id)s, %(account_id)s, %(schedule_key)s,
                now(), 'running', 'worker-1', now() + interval '5 minutes', now()
            )
            """,
            {**ids, "schedule_key": f"test-{ids['job_id']}"},
        )
        connection.execute(
            """
            INSERT INTO hermes_execution_runs (
                id, job_id, user_id, ig_account_id, hermes_session_id,
                config_version, config_snapshot, status
            ) VALUES (
                %(run_id)s, %(job_id)s, %(user_id)s, %(account_id)s,
                %(session_id)s, 1, '{"version": 1}'::jsonb, 'running'
            )
            """,
            {**ids, "session_id": f"em_{ids['run_id'].hex}"},
        )
    try:
        yield ids
    finally:
        with pool.connection() as connection:
            connection.execute("DELETE FROM users WHERE id = %s", (ids["user_id"],))


def _mutation(world: dict[str, UUID], **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "run_id": world["run_id"],
        "job_id": world["job_id"],
        "user_id": world["user_id"],
        "ig_account_id": world["account_id"],
        "worker_id": "worker-1",
        "opportunity_id": uuid4(),
        "target_id": world["target_id"],
        "story_fingerprint": "ab" * 32,
        "action": "like",
        "idempotency_key": f"fixture_{uuid4().hex}",
        "generated_text_hash": None,
        "now": datetime.now(UTC),
        "execution_mode": "autonomous",
    }
    values.update(overrides)
    return values


def _tool_call(pool, run_id: UUID, idempotency_key: str):
    with pool.connection() as connection:
        return connection.execute(
            """
            SELECT status, failure_class, safe_result
            FROM hermes_tool_calls
            WHERE run_id = %s AND idempotency_key = %s
            """,
            (run_id, idempotency_key),
        ).fetchone()


@pytest.mark.asyncio
@pytest.mark.parametrize("job_status", ["running", "failed", "cancelled"])
async def test_recovery_closes_abandoned_runs_without_replaying_actions(pool, world, job_status):
    with pool.connection() as connection:
        connection.execute(
            "UPDATE jobs SET status=%s, lease_expires_at=now()-interval '1 minute' WHERE id=%s",
            (job_status, world["job_id"]),
        )
        for status in ("started", "confirmed"):
            connection.execute(
                """INSERT INTO hermes_tool_calls (run_id,tool_name,idempotency_key,status)
                   VALUES (%s,'like',%s,%s)""",
                (world["run_id"], f"recovery_{status}_key", status),
            )
    repository = ExecutionMcpRepository(pool)
    assert await repository.reconcile_abandoned_runs() == 1
    assert await repository.reconcile_abandoned_runs() == 0
    with pool.connection() as connection:
        job = connection.execute("SELECT status,lease_expires_at FROM jobs WHERE id=%s", (world["job_id"],)).fetchone()
        run = connection.execute("SELECT status,stop_reason,completed_at FROM hermes_execution_runs WHERE id=%s", (world["run_id"],)).fetchone()
    assert job["status"] == ("failed" if job_status == "running" else job_status)
    assert run["status"] == "failed"
    assert run["stop_reason"] == "worker_interrupted"
    assert run["completed_at"] is not None
    assert _tool_call(pool, world["run_id"], "recovery_started_key")["status"] == "uncertain"
    assert _tool_call(pool, world["run_id"], "recovery_confirmed_key")["status"] == "confirmed"


@pytest.mark.asyncio
async def test_recovery_preserves_live_lease_and_skips_locked_job(pool, world):
    repository = ExecutionMcpRepository(pool)
    assert await repository.reconcile_abandoned_runs() == 0
    with pool.connection() as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 minute' WHERE id=%s", (world["job_id"],))
    with pool.connection() as locked:
        locked.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (world["job_id"],))
        assert await repository.reconcile_abandoned_runs() == 0
    assert await repository.reconcile_abandoned_runs() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected", [("time_limit", "completed"), ("safety_stop", "failed")])
async def test_recovery_preserves_a_recorded_completion(pool, world, reason, expected):
    with pool.connection() as connection:
        connection.execute("UPDATE jobs SET lease_expires_at=now()-interval '1 minute' WHERE id=%s", (world["job_id"],))
        connection.execute("UPDATE hermes_execution_runs SET status='completed',stop_reason=%s,completed_at=now() WHERE id=%s", (reason, world["run_id"]))
    assert await ExecutionMcpRepository(pool).reconcile_abandoned_runs() == 1
    with pool.connection() as connection:
        job = connection.execute("SELECT status,last_error FROM jobs WHERE id=%s", (world["job_id"],)).fetchone()
        run = connection.execute("SELECT status,stop_reason FROM hermes_execution_runs WHERE id=%s", (world["run_id"],)).fetchone()
    assert job == {"status": expected, "last_error": reason}
    assert run == {"status": "completed", "stop_reason": reason}


@pytest.mark.asyncio
async def test_disabled_mode_denies_and_persists_an_audit_row(pool, world) -> None:
    values = _mutation(world, execution_mode="disabled")

    result = await ExecutionMcpRepository(pool).begin_mutation(**values)

    assert result == {
        "allowed": False,
        "replay": False,
        "reason": "execution_disabled",
    }
    row = _tool_call(pool, world["run_id"], values["idempotency_key"])
    assert row["status"] == "denied"
    assert row["failure_class"] == "execution_disabled"
    assert row["safe_result"] == {
        "status": "denied",
        "reason": "execution_disabled",
    }


@pytest.mark.asyncio
async def test_autonomous_mode_allows_and_finalizes_one_tool_call(pool, world) -> None:
    values = _mutation(world)
    repository = ExecutionMcpRepository(pool)

    allowed = await repository.begin_mutation(**values)
    await repository.finish_mutation(
        run_id=world["run_id"],
        idempotency_key=values["idempotency_key"],
        status="confirmed",
        failure_class=None,
        safe_result={"status": "confirmed"},
        completed_at=datetime.now(UTC),
    )

    assert allowed == {"allowed": True, "replay": False}
    assert _tool_call(pool, world["run_id"], values["idempotency_key"])[
        "safe_result"
    ] == {"status": "confirmed"}


@pytest.mark.asyncio
async def test_control_row_denies_the_matching_account(pool, world) -> None:
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO instagram_execution_controls (
                scope, user_id, ig_account_id, blocked, reason_code
            ) VALUES ('account', %s, %s, true, 'operator_stop')
            """,
            (world["user_id"], world["account_id"]),
        )
    values = _mutation(world)

    result = await ExecutionMcpRepository(pool).begin_mutation(**values)

    assert result["reason"] == "operator_stop"
    assert _tool_call(pool, world["run_id"], values["idempotency_key"])[
        "status"
    ] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE ig_accounts SET paused_at=now() WHERE id=%s",
        "UPDATE ig_accounts SET health_status='degraded' WHERE id=%s",
    ],
)
async def test_account_health_is_rechecked_before_mutation_claim(pool, world, mutation) -> None:
    with pool.connection() as connection:
        connection.execute(mutation, (world["account_id"],))
    values = _mutation(world)

    result = await ExecutionMcpRepository(pool).begin_mutation(**values)

    assert result == {
        "allowed": False,
        "replay": False,
        "reason": "account_unhealthy",
    }
    assert _tool_call(pool, world["run_id"], values["idempotency_key"])[
        "failure_class"
    ] == "account_unhealthy"


@pytest.mark.asyncio
async def test_canary_approval_is_consumed_once_and_cannot_authorize_replay(
    pool, world
) -> None:
    first = _mutation(world, execution_mode="canary")
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO live_action_approvals (
                user_id, ig_account_id, target_id, story_fingerprint,
                action, approved_by, expires_at
            ) VALUES (%s, %s, %s, %s, 'like', 'operator', now() + interval '5 minutes')
            """,
            (
                world["user_id"],
                world["account_id"],
                world["target_id"],
                first["story_fingerprint"],
            ),
        )
    repository = ExecutionMcpRepository(pool)

    allowed = await repository.begin_mutation(**first)
    second = await repository.begin_mutation(
        **_mutation(
            world,
            execution_mode="canary",
            story_fingerprint=first["story_fingerprint"],
        )
    )

    assert allowed == {"allowed": True, "replay": False}
    assert second["reason"] == "unresolved_story_action"
    with pool.connection() as connection:
        consumed = connection.execute(
            """
            SELECT consumed_at IS NOT NULL AS consumed
            FROM live_action_approvals
            WHERE user_id = %s
            """,
            (world["user_id"],),
        ).fetchone()
    assert consumed["consumed"] is True


@pytest.mark.asyncio
async def test_idempotency_replay_returns_the_persisted_safe_result(pool, world) -> None:
    values = _mutation(world)
    repository = ExecutionMcpRepository(pool)
    await repository.begin_mutation(**values)
    await repository.finish_mutation(
        run_id=world["run_id"],
        idempotency_key=values["idempotency_key"],
        status="confirmed",
        failure_class=None,
        safe_result={"status": "confirmed"},
        completed_at=datetime.now(UTC),
    )

    replay = await repository.begin_mutation(**values)

    assert replay == {
        "allowed": False,
        "replay": True,
        "reason": "idempotency_replay",
        "result": {"status": "confirmed"},
    }
    with pool.connection() as connection:
        count = connection.execute(
            "SELECT count(*) AS count FROM hermes_tool_calls WHERE run_id = %s",
            (world["run_id"],),
        ).fetchone()
    assert count["count"] == 1


@pytest.mark.asyncio
async def test_in_flight_idempotency_replay_returns_uncertain_without_crashing(
    pool, world
) -> None:
    values = _mutation(world)
    repository = ExecutionMcpRepository(pool)
    assert await repository.begin_mutation(**values) == {
        "allowed": True,
        "replay": False,
    }

    replay = await repository.begin_mutation(**values)

    assert replay == {
        "allowed": False,
        "replay": True,
        "reason": "idempotency_in_progress",
        "result": {
            "status": "uncertain",
            "reason": "idempotency_in_progress",
        },
    }


@pytest.mark.asyncio
async def test_job_ownership_mismatch_denies_and_records_lease_loss(pool, world) -> None:
    values = _mutation(world, worker_id="another-worker")

    result = await ExecutionMcpRepository(pool).begin_mutation(**values)

    assert result["reason"] == "lease_lost"
    assert _tool_call(pool, world["run_id"], values["idempotency_key"])[
        "failure_class"
    ] == "lease_lost"


@pytest.mark.asyncio
async def test_view_claim_and_finalization_create_no_billing_usage(pool, world) -> None:
    repository = ExecutionRepository(pool)
    claim = await repository.claim(
        ClaimMetadata(
            user_id=world["user_id"],
            ig_account_id=world["account_id"],
            target_id=world["target_id"],
            fingerprint="cd" * 32,
            action="view",
            generated_text=None,
            model_used="engagemate-agent",
            rationale="Hermes selected a bounded view",
            confidence=1.0,
        )
    )

    assert await repository.mark_sent(
        world["user_id"], world["account_id"], claim.engagement_id
    )
    with pool.connection() as connection:
        engagement = connection.execute(
            "SELECT action, status FROM story_engagements WHERE id = %s",
            (claim.engagement_id,),
        ).fetchone()
        usage = connection.execute(
            "SELECT count(*) AS count FROM engagement_usage_events WHERE engagement_id = %s",
            (claim.engagement_id,),
        ).fetchone()
    assert engagement == {"action": "view", "status": "sent"}
    assert usage["count"] == 0


@pytest.mark.asyncio
async def test_start_run_persists_the_exact_owned_job_and_config_snapshot(
    pool, world
) -> None:
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM hermes_execution_runs WHERE id = %s", (world["run_id"],)
        )
    snapshot = {
        "version": 7,
        "profile": {"tone": "warm"},
        "targets": [{"username": "target.one"}],
    }

    started = await ExecutionMcpRepository(pool).start_run(
        run_id=world["run_id"],
        job_id=world["job_id"],
        user_id=world["user_id"],
        ig_account_id=world["account_id"],
        worker_id="worker-1",
        hermes_session_id=f"em_{uuid4().hex}",
        config_version=7,
        config_snapshot=snapshot,
        started_at=datetime.now(UTC),
    )

    assert started is True
    with pool.connection() as connection:
        row = connection.execute(
            """
            SELECT config_version, config_snapshot, status
            FROM hermes_execution_runs WHERE id = %s
            """,
            (world["run_id"],),
        ).fetchone()
    assert row == {
        "config_version": 7,
        "config_snapshot": snapshot,
        "status": "running",
    }


@pytest.mark.asyncio
async def test_start_run_refuses_a_job_owned_by_another_worker(pool, world) -> None:
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM hermes_execution_runs WHERE id = %s", (world["run_id"],)
        )

    started = await ExecutionMcpRepository(pool).start_run(
        run_id=world["run_id"],
        job_id=world["job_id"],
        user_id=world["user_id"],
        ig_account_id=world["account_id"],
        worker_id="another-worker",
        hermes_session_id=f"em_{uuid4().hex}",
        config_version=1,
        config_snapshot={"version": 1},
        started_at=datetime.now(UTC),
    )

    assert started is False
    with pool.connection() as connection:
        count = connection.execute(
            "SELECT count(*) AS count FROM hermes_execution_runs WHERE id = %s",
            (world["run_id"],),
        ).fetchone()
    assert count["count"] == 0


@pytest.mark.asyncio
async def test_fail_run_closes_an_unfinished_audit_row(pool, world) -> None:
    closed = await ExecutionMcpRepository(pool).fail_run(
        run_id=world["run_id"],
        reason="hermes_failed",
        completed_at=datetime.now(UTC),
    )

    assert closed is True
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT status, stop_reason FROM hermes_execution_runs WHERE id = %s",
            (world["run_id"],),
        ).fetchone()
    assert row == {"status": "failed", "stop_reason": "hermes_failed"}


@pytest.mark.asyncio
async def test_complete_run_reports_uncertain_calls_separately(pool, world) -> None:
    repository = ExecutionMcpRepository(pool)
    values = _mutation(world)
    await repository.begin_mutation(**values)
    await repository.finish_mutation(
        run_id=world["run_id"],
        idempotency_key=values["idempotency_key"],
        status="uncertain",
        failure_class="executor_exception",
        safe_result={"status": "uncertain", "reason": "executor_exception"},
        completed_at=datetime.now(UTC),
    )

    counters = await repository.complete_run(
        run_id=world["run_id"],
        reason="safety_stop",
        completed_at=datetime.now(UTC),
    )

    assert counters == {
        "considered": 1,
        "confirmed": 0,
        "failed": 0,
        "denied": 0,
        "gated": 0,
        "uncertain": 1,
    }


@pytest.mark.asyncio
async def test_complete_run_separates_text_gates_from_other_denials(pool, world) -> None:
    repository = ExecutionMcpRepository(pool)
    values = _mutation(world)
    await repository.begin_mutation(**values)
    await repository.finish_mutation(
        run_id=world["run_id"],
        idempotency_key=values["idempotency_key"],
        status="denied",
        failure_class="text_gate_banned_phrase",
        safe_result={"status": "denied", "reason": "text_gate_banned_phrase"},
        completed_at=datetime.now(UTC),
    )

    counters = await repository.complete_run(
        run_id=world["run_id"],
        reason="completed",
        completed_at=datetime.now(UTC),
    )

    assert counters == {
        "considered": 1,
        "confirmed": 0,
        "failed": 0,
        "denied": 0,
        "gated": 1,
        "uncertain": 0,
    }


@pytest.mark.asyncio
async def test_retry_safety_allows_only_runs_without_external_action_receipts(pool, world) -> None:
    repository = ExecutionMcpRepository(pool)
    assert await repository.can_retry_without_external_write(world["run_id"]) is True

    denied = _mutation(world)
    await repository.begin_mutation(**{**denied, "execution_mode": "disabled"})
    assert await repository.can_retry_without_external_write(world["run_id"]) is True

    confirmed = _mutation(world)
    await repository.begin_mutation(**confirmed)
    await repository.finish_mutation(
        run_id=world["run_id"],
        idempotency_key=confirmed["idempotency_key"],
        status="confirmed",
        failure_class=None,
        safe_result={"status": "confirmed"},
        completed_at=datetime.now(UTC),
    )
    assert await repository.can_retry_without_external_write(world["run_id"]) is False
