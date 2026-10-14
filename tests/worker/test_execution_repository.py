import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from engagemate.db.executions import (
    ClaimMetadata,
    ExecutionRepository,
    RepositoryError,
    UsageLimitExceeded,
)
from engagemate.worker.circuit_breaker import (
    AccountHealthStatus,
    BreakerDecision,
)

USER = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000002")
TARGET = UUID("00000000-0000-0000-0000-000000000003")
ENGAGEMENT = UUID("00000000-0000-0000-0000-000000000004")
FINGERPRINT = "ab" * 32
PERIOD_START = datetime(2026, 8, 1, tzinfo=timezone.utc)
PERIOD_END = datetime(2026, 9, 1, tzinfo=timezone.utc)


class Result:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((" ".join(sql.split()), dict(params)))
        return Result(self.rows.pop(0) if self.rows else None)


class ConnectionCM:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc, tb):
        self.connection.exited = exc_type


class Pool:
    def __init__(self, connection):
        self.connection_value = connection

    def connection(self):
        return ConnectionCM(self.connection_value)


def metadata():
    return ClaimMetadata(
        user_id=USER,
        ig_account_id=ACCOUNT,
        target_id=TARGET,
        fingerprint=FINGERPRINT,
        action="reply",
        generated_text="hello",
        model_used="claude-sonnet-4-6",
        rationale="relevant",
        confidence=0.9,
    )


def view_metadata():
    return ClaimMetadata(
        user_id=USER,
        ig_account_id=ACCOUNT,
        target_id=TARGET,
        fingerprint=FINGERPRINT,
        action="view",
        generated_text=None,
        model_used="engagemate-agent",
        rationale="Hermes selected a bounded view",
        confidence=1.0,
    )


@pytest.mark.asyncio
async def test_claim_locks_validates_tenant_dedupes_across_actions_then_inserts():
    connection = Connection([
        {"locked": True},
        {"id": TARGET},
        None,
        {"locked": True},
        {"id": ENGAGEMENT},
        {"engagement_id": ENGAGEMENT},
    ])
    claim = await ExecutionRepository(Pool(connection)).claim(metadata())
    sql = [call[0] for call in connection.calls]
    assert claim.engagement_id == ENGAGEMENT and not claim.duplicate
    assert "pg_advisory_xact_lock" in sql[0]
    assert "user_id = %(user_id)s" in sql[1] and "ig_account_id = %(account_id)s" in sql[1]
    assert "action" not in sql[2].split("WHERE", 1)[1]
    assert "user_id = %(user_id)s" in sql[2]
    assert "pg_advisory_xact_lock" in sql[3]
    assert "status" in sql[4] and "'skipped'" in sql[4]
    assert "INSERT INTO engagement_usage_events" in sql[5]
    assert not any("stripe" in q or "overage" in q for q in sql)


@pytest.mark.asyncio
async def test_view_claim_records_the_story_without_billing_usage() -> None:
    connection = Connection([
        {"locked": True},
        {"id": TARGET},
        None,
        {"id": ENGAGEMENT},
    ])

    claim = await ExecutionRepository(Pool(connection)).claim(view_metadata())

    assert claim == type(claim)(ENGAGEMENT, False)
    sql = [call[0] for call in connection.calls]
    assert len(sql) == 4
    assert "INSERT INTO story_engagements" in sql[-1]
    assert all("engagement_usage_events" not in statement for statement in sql)




@pytest.mark.asyncio
async def test_existing_cross_action_row_returns_duplicate_without_insert():
    connection = Connection([{"locked": True}, {"id": TARGET}, {"id": ENGAGEMENT}])
    claim = await ExecutionRepository(Pool(connection)).claim(metadata())
    assert claim.duplicate and claim.engagement_id == ENGAGEMENT
    assert len(connection.calls) == 3


@pytest.mark.asyncio
async def test_expected_status_scopes_sent_and_failed_updates():
    connection = Connection([{"id": ENGAGEMENT}, {"id": ENGAGEMENT}])
    repository = ExecutionRepository(Pool(connection))
    assert await repository.mark_sent(USER, ACCOUNT, ENGAGEMENT)
    assert await repository.mark_failed(USER, ACCOUNT, ENGAGEMENT, "action_failed")
    for sql, params in connection.calls:
        assert "user_id = %(user_id)s" in sql and "ig_account_id = %(account_id)s" in sql
        assert "status = 'skipped'" in sql
        assert "engagement_usage_events" in sql
        assert "status = 'reserved'" in sql
        assert "traceback" not in repr(params).lower()
    assert connection.calls[0][1]["usage_status"] == "billable"
    assert connection.calls[1][1]["usage_status"] == "released"


@pytest.mark.parametrize("reason", ["story_unavailable", "story_expired"])
async def test_skip_releases_usage_and_keeps_tenant_scoped_claim(reason):
    connection = Connection([{"engagement_id": ENGAGEMENT}])
    assert await ExecutionRepository(Pool(connection)).mark_skipped(
        USER, ACCOUNT, ENGAGEMENT, reason
    )
    sql, params = connection.calls[0]
    assert params["status"] == "skipped"
    assert params["usage_status"] == "released"
    assert params["failure_code"] == reason
    assert params["user_id"] == USER and params["account_id"] == ACCOUNT
    assert "AND failure_code IS NULL" in sql
    assert "DELETE" not in sql


async def test_skip_rejects_arbitrary_reasons_before_database_use():
    connection = Connection([])
    with pytest.raises(ValueError, match="skip reason"):
        await ExecutionRepository(Pool(connection)).mark_skipped(
            USER, ACCOUNT, ENGAGEMENT, "action_failed"
        )
    assert not connection.calls


@pytest.mark.asyncio
async def test_breaker_transaction_orders_lock_event_update_and_account_job_cancel():
    connection = Connection([{"id": ACCOUNT}, None, {"id": ACCOUNT}, None])
    decision = BreakerDecision(
        True,
        AccountHealthStatus.PAUSED,
        "rate_limit",
        datetime(2026, 7, 31, tzinfo=timezone.utc) + timedelta(hours=24),
        datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    await ExecutionRepository(Pool(connection)).persist_breaker(USER, ACCOUNT, decision)
    sql = [call[0] for call in connection.calls]
    assert "FOR UPDATE" in sql[0]
    assert "account_health_events" in sql[1]
    assert "UPDATE ig_accounts" in sql[2]
    assert "UPDATE jobs" in sql[3]
    assert "ig_account_id = %(account_id)s" in sql[3]
    assert connection.calls[2][1]["paused_at"] == datetime(
        2026, 7, 31, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_breaker_rejects_untyped_or_nonstop_decision_before_connection():
    pool = Pool(Connection([]))
    repository = ExecutionRepository(pool)
    with pytest.raises(ValueError, match="breaker decision"):
        await repository.persist_breaker(USER, ACCOUNT, object())
    with pytest.raises(ValueError, match="breaker decision"):
        await repository.persist_breaker(
            USER,
            ACCOUNT,
            BreakerDecision(
                False,
                AccountHealthStatus.ACTIVE,
                "none",
                None,
                datetime(2026, 7, 31, tzinfo=timezone.utc),
            ),
        )
    assert pool.connection_value.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        BreakerDecision(
            True,
            AccountHealthStatus.ACTIVE,
            "challenge",
            None,
            datetime(2026, 7, 31, tzinfo=timezone.utc),
        ),
        BreakerDecision(
            True,
            AccountHealthStatus.PAUSED,
            "rate_limit",
            datetime(2026, 8, 1),  # noqa: DTZ001 - intentional naive input
            datetime(2026, 7, 31, tzinfo=timezone.utc),
        ),
        BreakerDecision(
            True,
            AccountHealthStatus.PAUSED,
            "rate_limit",
            datetime(2026, 8, 1, tzinfo=timezone(timedelta(hours=1))),
            datetime(2026, 7, 31, tzinfo=timezone.utc),
        ),
    ],
)
async def test_breaker_rejects_malformed_mapping_and_clocks_before_pool(decision):
    pool = Pool(Connection([]))
    with pytest.raises(ValueError, match="breaker decision"):
        await ExecutionRepository(pool).persist_breaker(USER, ACCOUNT, decision)
    assert pool.connection_value.calls == []


@pytest.mark.asyncio
async def test_repository_errors_are_bounded_and_async_offloaded(monkeypatch):
    connection = Connection([])
    secret = "Bearer repository-secret"

    def fail_connection():
        raise RuntimeError(secret)

    pool = Pool(connection)
    pool.connection = fail_connection
    calls = []
    original = asyncio.to_thread

    async def recorded(function, *args):
        calls.append(function)
        return await original(function, *args)

    monkeypatch.setattr(asyncio, "to_thread", recorded)
    with pytest.raises(RepositoryError) as caught:
        await ExecutionRepository(pool).claim(metadata())
    assert calls
    assert secret not in repr(caught.value)
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_claim_rejects_malformed_metadata_before_connection():
    pool = Pool(Connection([]))
    bad = metadata()
    object.__setattr__(bad, "fingerprint", "not-a-fingerprint")
    with pytest.raises(ValueError, match="claim metadata"):
        await ExecutionRepository(pool).claim(bad)
    assert pool.connection_value.calls == []


@pytest.mark.asyncio
async def test_claim_text_carrier_and_cancellation_traceback_redact_plaintext(
    monkeypatch,
):
    claim = metadata()
    plaintext = "hello"
    assert plaintext not in repr(claim.generated_text)

    async def cancel(function, *args):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "to_thread", cancel)
    try:
        await ExecutionRepository(Pool(Connection([]))).claim(claim)
    except asyncio.CancelledError as error:
        frames = []
        traceback = error.__traceback__
        while traceback is not None:
            if "/api/engagemate/" in traceback.tb_frame.f_code.co_filename:
                frames.append(
                    {
                        name: repr(value)
                        for name, value in traceback.tb_frame.f_locals.items()
                    }
                )
            traceback = traceback.tb_next
        assert plaintext not in repr(frames)
    else:
        raise AssertionError("cancellation did not propagate")
