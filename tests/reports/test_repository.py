"""PostgreSQL contracts for weekly report aggregation and delivery leases."""

from __future__ import annotations

import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from threading import Barrier

import psycopg
import pytest
from engagemate.db.pool import create_pool
from engagemate.reports.aggregate import previous_week
from engagemate.reports.repository import (
    _DUE_MANAGERS_SQL,
    _ENSURE_PREFERENCES_SQL,
    DeliveryClaim,
    DueManager,
    ReportRepository,
)
from engagemate.worker.wiring import AsyncRunSummaryRepository

from tests.worker import test_canary_scripts as canary_fixtures
from tests.worker.test_runner import (
    ACCOUNT as WORKER_ACCOUNT,
)
from tests.worker.test_runner import (
    JOB as WORKER_JOB,
)
from tests.worker.test_runner import (
    USER as WORKER_USER,
)
from tests.worker.test_runner import (
    WORKER,
    FakeHermes,
    build_runner,
)

worker_database_url = canary_fixtures.database_url

SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY,
    email text NOT NULL
);

CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    username text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, user_id)
);

CREATE TABLE agent_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    timezone text NOT NULL DEFAULT 'UTC',
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);

CREATE TABLE account_growth_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    snapshot_date date NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    follower_count integer,
    engagement_rate_bps integer,
    status text NOT NULL CHECK (status IN ('complete', 'partial', 'unavailable')),
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE,
    UNIQUE (ig_account_id, snapshot_date)
);

CREATE TABLE run_summaries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    job_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
    opportunities_found integer NOT NULL DEFAULT 0,
    skipped integer NOT NULL DEFAULT 0,
    gated integer NOT NULL DEFAULT 0,
    failed integer NOT NULL DEFAULT 0,
    input_tokens integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    images_analyzed integer NOT NULL DEFAULT 0,
    video_frames_analyzed integer NOT NULL DEFAULT 0,
    retries integer NOT NULL DEFAULT 0,
    runtime_ms integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);

CREATE TABLE story_engagements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    target_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('like', 'reply', 'dm', 'view')),
    generated_text text,
    status text NOT NULL CHECK (status IN ('sent', 'skipped', 'gated', 'failed')),
    executed_at timestamptz,
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);

CREATE TABLE relationship_follows (id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid, ig_account_id uuid, confirmed_follow_at timestamptz, unfollowed_at timestamptz, evidence jsonb);

CREATE TABLE weekly_report_preferences (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled boolean NOT NULL DEFAULT true,
    timezone text NOT NULL DEFAULT 'UTC',
    send_hour integer NOT NULL DEFAULT 9 CHECK (send_hour BETWEEN 0 AND 23),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE weekly_report_deliveries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    week_start date NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    recipient_email text NOT NULL,
    provider_message_id text,
    last_error_code text CHECK (
        last_error_code IS NULL OR char_length(last_error_code) <= 64
    ),
    lease_token uuid,
    lease_expires_at timestamptz,
    payload_hash text CHECK (
        payload_hash IS NULL OR payload_hash ~ '^[0-9a-f]{64}$'
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    sent_at timestamptz,
    CHECK (
        (status = 'sending'
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL)
        OR
        (status <> 'sending' AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    UNIQUE (user_id, week_start)
);
"""


@pytest.fixture(scope="module")
def database_url() -> str:
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode != 0:
        pytest.skip("local PostgreSQL is unavailable")

    database = f"engagemate_reports_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    created = subprocess.run(
        ["createdb", database], capture_output=True, text=True, check=False
    )
    if created.returncode != 0:
        pytest.fail(
            f"could not create disposable PostgreSQL database: {created.stderr}"
        )

    url = f"postgresql://localhost/{database}"
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(SCHEMA_SQL)
        yield url
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", database],
            capture_output=True,
            check=False,
        )


@pytest.fixture()
def pool(database_url: str):
    pool = create_pool(database_url, min_size=1, max_size=8)
    try:
        with pool.connection() as connection:
            connection.execute(
                "TRUNCATE weekly_report_deliveries, weekly_report_preferences, "
                "story_engagements, run_summaries, account_growth_snapshots, "
                "agent_configs, ig_accounts, users CASCADE"
            )
        yield pool
    finally:
        pool.close()


def _seed_user(
    pool,
    email: str = "manager@example.test",
    *,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    manager_id = user_id or uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO users (id, email) VALUES (%s, %s)", (manager_id, email)
        )
    return manager_id


def _seed_account(
    pool,
    user_id: uuid.UUID,
    username: str,
    *,
    account_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
    timezone_name: str | None = None,
    config_created_at: datetime | None = None,
) -> uuid.UUID:
    managed_account_id = account_id or uuid.uuid4()
    created = created_at or datetime(2026, 1, 1, tzinfo=timezone.utc)
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO ig_accounts (id, user_id, username, created_at)
            VALUES (%s, %s, %s, %s)
            """,
            (managed_account_id, user_id, username, created),
        )
        if timezone_name is not None:
            connection.execute(
                """
                INSERT INTO agent_configs (
                    user_id, ig_account_id, timezone, created_at
                ) VALUES (%s, %s, %s, %s)
                """,
                (
                    user_id,
                    managed_account_id,
                    timezone_name,
                    config_created_at or created,
                ),
            )
    return managed_account_id


def _snapshot(
    pool,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    snapshot_date: date,
    follower_count: int | None,
    engagement_rate_bps: int | None,
    status: str = "complete",
) -> None:
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO account_growth_snapshots (
                user_id, ig_account_id, snapshot_date, follower_count,
                engagement_rate_bps, status
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                account_id,
                snapshot_date,
                follower_count,
                engagement_rate_bps,
                status,
            ),
        )


def _run_summary(
    pool,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    completed_at: datetime | None,
    *,
    opportunities: int,
    skipped: int = 0,
    gated: int = 0,
    failed: int = 0,
) -> None:
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO run_summaries (
                user_id, ig_account_id, opportunities_found,
                skipped, gated, failed, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                account_id,
                opportunities,
                skipped,
                gated,
                failed,
                completed_at,
            ),
        )


def _engagement(
    pool,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    action: str,
    status: str,
    executed_at: datetime | None,
) -> None:
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO story_engagements (
                user_id, ig_account_id, target_id, action,
                generated_text, status, executed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                account_id,
                uuid.uuid4(),
                action,
                "private generated fixture text",
                status,
                executed_at,
            ),
        )


def _delivery_row(pool, user_id: uuid.UUID, week_start: date) -> dict:
    with pool.connection() as connection:
        row = connection.execute(
            """
            SELECT id, status, attempts, recipient_email, provider_message_id,
                   last_error_code, lease_token, lease_expires_at, payload_hash,
                   created_at, updated_at, sent_at
            FROM weekly_report_deliveries
            WHERE user_id = %s AND week_start = %s
            """,
            (user_id, week_start),
        ).fetchone()
    assert row is not None
    for key in (
        "status",
        "recipient_email",
        "provider_message_id",
        "last_error_code",
        "payload_hash",
    ):
        if isinstance(row[key], bytes):
            row[key] = row[key].decode("utf-8")
    return row


def test_build_report_aggregates_every_owned_account_at_local_boundaries(pool) -> None:
    manager_id = _seed_user(pool)
    alpha_id = _seed_account(pool, manager_id, "alpha.account")
    beta_id = _seed_account(pool, manager_id, "beta.account")
    other_id = _seed_user(pool, "other@example.test")
    other_account_id = _seed_account(pool, other_id, "other.account")
    period = previous_week(
        datetime(2026, 3, 9, 14, tzinfo=timezone.utc), "America/New_York"
    )

    _snapshot(pool, manager_id, alpha_id, date(2026, 2, 28), 90, 190)
    _snapshot(pool, manager_id, alpha_id, date(2026, 3, 1), 100, 200)
    _snapshot(
        pool,
        manager_id,
        alpha_id,
        date(2026, 3, 2),
        None,
        None,
        "unavailable",
    )
    _snapshot(pool, manager_id, alpha_id, date(2026, 3, 8), 112, 245, "partial")
    _snapshot(pool, manager_id, beta_id, date(2026, 3, 9), 55, None, "partial")
    _snapshot(pool, other_id, other_account_id, date(2026, 3, 2), 9_999, 9_999)
    _snapshot(pool, other_id, other_account_id, date(2026, 3, 9), 10_999, 10_999)

    _run_summary(
        pool,
        manager_id,
        alpha_id,
        period.start_utc,
        opportunities=4,
        skipped=1,
        gated=2,
        failed=3,
    )
    _run_summary(
        pool,
        manager_id,
        alpha_id,
        period.start_utc - timedelta(microseconds=1),
        opportunities=100,
        skipped=100,
    )
    _run_summary(
        pool,
        manager_id,
        alpha_id,
        period.end_utc,
        opportunities=100,
        gated=100,
    )
    _run_summary(
        pool,
        manager_id,
        alpha_id,
        None,
        opportunities=100,
        failed=100,
    )
    _run_summary(
        pool,
        manager_id,
        beta_id,
        period.end_utc - timedelta(microseconds=1),
        opportunities=2,
        skipped=2,
        failed=1,
    )
    _run_summary(
        pool,
        other_id,
        other_account_id,
        period.start_utc,
        opportunities=9_999,
        skipped=9_999,
        gated=9_999,
        failed=9_999,
    )

    for action in ("like", "reply", "dm", "view"):
        _engagement(
            pool,
            manager_id,
            alpha_id,
            action,
            "sent",
            period.start_utc,
        )
    _engagement(
        pool,
        manager_id,
        alpha_id,
        "reply",
        "skipped",
        period.start_utc,
    )
    _engagement(
        pool,
        manager_id,
        alpha_id,
        "dm",
        "failed",
        period.start_utc,
    )
    _engagement(
        pool,
        manager_id,
        alpha_id,
        "view",
        "gated",
        period.start_utc,
    )
    _engagement(
        pool,
        manager_id,
        alpha_id,
        "like",
        "sent",
        period.end_utc,
    )
    _engagement(
        pool,
        manager_id,
        beta_id,
        "view",
        "sent",
        period.end_utc - timedelta(microseconds=1),
    )
    _engagement(
        pool,
        other_id,
        other_account_id,
        "reply",
        "sent",
        period.start_utc,
    )

    report = ReportRepository(pool).build_report(manager_id, period)

    assert report.period is period
    by_username = {account.username: account for account in report.accounts}
    assert set(by_username) == {"alpha.account", "beta.account"}
    alpha = by_username["alpha.account"]
    beta = by_username["beta.account"]
    assert (
        alpha.follower_start,
        alpha.follower_end,
        alpha.engagement_rate_bps_start,
        alpha.engagement_rate_bps_end,
    ) == (100, 112, 200, 245)
    assert (
        alpha.opportunities_reviewed,
        alpha.likes_sent,
        alpha.replies_sent,
        alpha.dms_sent,
        alpha.views_sent,
        alpha.skipped,
        alpha.gated,
        alpha.failed,
    ) == (4, 1, 1, 1, 1, 1, 2, 3)
    assert alpha.estimated_time_saved_seconds == 240
    assert (
        beta.follower_start,
        beta.follower_end,
        beta.engagement_rate_bps_start,
        beta.engagement_rate_bps_end,
    ) == (None, 55, None, None)
    assert (
        beta.opportunities_reviewed,
        beta.likes_sent,
        beta.replies_sent,
        beta.dms_sent,
        beta.views_sent,
        beta.skipped,
        beta.gated,
        beta.failed,
    ) == (2, 0, 0, 0, 1, 2, 0, 1)
    assert beta.estimated_time_saved_seconds == 60
    assert report.estimated_time_saved_seconds == 300


@pytest.fixture
def worker_report_pool(worker_database_url):
    with create_pool(worker_database_url, min_size=1, max_size=4) as full_pool:
        yield full_pool


async def test_worker_persisted_gate_outcome_is_reported_as_gated(worker_report_pool) -> None:
    # Summary persistence joins terminal jobs and execution evidence; use the
    # real migrated schema instead of the reporting-only table fixture.
    pool = worker_report_pool
    manager_id = WORKER_USER
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO users (id, owner_key, email) VALUES (%s, %s, %s)",
            (manager_id, "worker-report-fixture", "worker-report@example.test"),
        )
    _seed_account(
        pool,
        manager_id,
        "gated.account",
        account_id=WORKER_ACCOUNT,
    )
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO jobs (id,user_id,ig_account_id,schedule_key,scheduled_for,status) "
            "VALUES (%s,%s,%s,'worker-report-fixture',now(),'completed')",
            (WORKER_JOB, manager_id, WORKER_ACCOUNT),
        )
    period = previous_week(
        datetime(2026, 8, 3, 9, tzinfo=timezone.utc),
        "UTC",
    )
    runner, _ = build_runner(
        hermes=FakeHermes(
            counters={
                "considered": 1,
                "confirmed": 0,
                "failed": 0,
                "denied": 0,
                "gated": 1,
                "uncertain": 0,
            }
        ),
        summaries=AsyncRunSummaryRepository(pool),
    )

    summary = await runner.run_once(WORKER)
    account = ReportRepository(pool).build_report(manager_id, period).accounts[0]

    assert summary is not None
    assert summary.gated == 1
    assert summary.skipped == 0
    assert account.opportunities_reviewed == 1
    assert account.gated == 1
    assert account.skipped == 0


def test_due_selection_backfills_preferences_and_returns_only_safe_inputs(pool) -> None:
    manager_id = _seed_user(pool, "manager@example.test")
    _seed_account(
        pool,
        manager_id,
        "later.account",
        account_id=uuid.UUID("00000000-0000-4000-8000-000000000012"),
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        timezone_name="UTC",
        config_created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    _seed_account(
        pool,
        manager_id,
        "first.account",
        account_id=uuid.UUID("00000000-0000-4000-8000-000000000011"),
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        timezone_name="Asia/Makassar",
        config_created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    utc_default_id = _seed_user(pool, "utc@example.test")
    _seed_account(pool, utc_default_id, "no.config")
    no_account_id = _seed_user(pool, "no-account@example.test")
    disabled_id = _seed_user(pool, "disabled@example.test")
    _seed_account(pool, disabled_id, "disabled.account")
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO weekly_report_preferences (
                user_id, enabled, timezone, send_hour
            ) VALUES (%s, false, 'UTC', 0)
            """,
            (disabled_id,),
        )

    now = datetime(2026, 8, 17, 1, tzinfo=timezone.utc)
    repository = ReportRepository(pool)
    due = repository.list_due_managers(now, 10)

    assert due == [
        DueManager(
            user_id=manager_id,
            recipient_email="manager@example.test",
            timezone="Asia/Makassar",
            period=previous_week(now, "Asia/Makassar"),
        )
    ]
    assert no_account_id not in {manager.user_id for manager in due}
    assert {field.name for field in fields(DueManager)} == {
        "user_id",
        "recipient_email",
        "timezone",
        "period",
    }
    with pool.connection() as connection:
        preferences = connection.execute(
            """
            SELECT user_id, timezone FROM weekly_report_preferences
            ORDER BY user_id
            """
        ).fetchall()
    by_user = {
        row["user_id"]: (
            row["timezone"].decode("utf-8")
            if isinstance(row["timezone"], bytes)
            else row["timezone"]
        )
        for row in preferences
    }
    assert by_user[manager_id] == "Asia/Makassar"
    assert by_user[utc_default_id] == "UTC"
    assert no_account_id not in by_user


def test_preference_backfill_uses_stable_account_id_tie_break(pool) -> None:
    manager_id = _seed_user(pool, "tie-break@example.test")
    same_created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    lower_account_id = uuid.UUID("00000000-0000-4000-8000-000000000021")
    higher_account_id = uuid.UUID("00000000-0000-4000-8000-000000000022")
    _seed_account(
        pool,
        manager_id,
        "lower.id",
        account_id=lower_account_id,
        created_at=same_created_at,
        timezone_name="Europe/Paris",
        config_created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    _seed_account(
        pool,
        manager_id,
        "higher.id",
        account_id=higher_account_id,
        created_at=same_created_at,
        timezone_name="Europe/Berlin",
        config_created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert ReportRepository(pool).ensure_missing_preferences() == 1
    with pool.connection() as connection:
        preference = connection.execute(
            """
            SELECT timezone FROM weekly_report_preferences WHERE user_id = %s
            """,
            (manager_id,),
        ).fetchone()

    timezone_name = preference["timezone"]
    if isinstance(timezone_name, bytes):
        timezone_name = timezone_name.decode("utf-8")
    assert timezone_name == "Europe/Paris"
    assert "config.ig_account_id = account.id" in _ENSURE_PREFERENCES_SQL
    assert "config.user_id = manager.id" in _ENSURE_PREFERENCES_SQL
    assert "account.user_id = manager.id" in _ENSURE_PREFERENCES_SQL
    assert "ORDER BY account.created_at, account.id" in _ENSURE_PREFERENCES_SQL


def test_due_selection_waits_until_local_monday_send_hour_and_skips_sent(pool) -> None:
    due_id = _seed_user(pool, "due@example.test")
    _seed_account(pool, due_id, "due.account")
    early_id = _seed_user(pool, "early@example.test")
    _seed_account(pool, early_id, "early.account")
    sent_id = _seed_user(pool, "sent@example.test")
    _seed_account(pool, sent_id, "sent.account")
    with pool.connection() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO weekly_report_preferences (
                    user_id, enabled, timezone, send_hour
                ) VALUES (%s, true, %s, %s)
                """,
                [
                    (due_id, "UTC", 9),
                    (early_id, "America/Los_Angeles", 9),
                    (sent_id, "UTC", 9),
                ],
            )
        connection.execute(
            """
            INSERT INTO weekly_report_deliveries (
                user_id, week_start, status, recipient_email, sent_at
            ) VALUES (%s, %s, 'sent', %s, %s)
            """,
            (
                sent_id,
                date(2026, 8, 10),
                "sent@example.test",
                datetime(2026, 8, 17, 9, tzinfo=timezone.utc),
            ),
        )

    due = ReportRepository(pool).list_due_managers(
        datetime(2026, 8, 17, 15, 59, tzinfo=timezone.utc), 10
    )

    assert [manager.user_id for manager in due] == [due_id]


def test_two_overlapping_transactions_cannot_claim_the_same_manager_week(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    barrier = Barrier(2)

    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO weekly_report_deliveries (
                user_id, week_start, status, attempts, recipient_email,
                created_at, updated_at
            ) VALUES (%s, %s, 'pending', 0, %s, %s, %s)
            """,
            (user_id, week_start, "manager@example.test", now, now),
        )

    def attempt(connection) -> DeliveryClaim | None:
        barrier.wait()
        return repository._claim_with_connection(
            connection,
            user_id,
            week_start,
            "manager@example.test",
            now,
        )

    with (
        pool.connection() as first_connection,
        pool.connection() as second_connection,
    ):
        first_transaction = first_connection.execute(
            "SELECT txid_current() AS id"
        ).fetchone()["id"]
        second_transaction = second_connection.execute(
            "SELECT txid_current() AS id"
        ).fetchone()["id"]
        assert first_transaction != second_transaction
        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(
                executor.map(
                    lambda connection: attempt(connection),
                    (first_connection, second_connection),
                )
            )

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    row = _delivery_row(pool, user_id, week_start)
    assert row["id"] == winners[0].delivery_id
    assert row["status"] == "sending"
    assert row["attempts"] == 1
    assert row["lease_token"] == winners[0].lease_token
    assert row["lease_expires_at"] == now + timedelta(minutes=15)


@pytest.mark.parametrize(
    ("status", "attempts", "created_at"),
    [
        ("failed", 1, datetime(2026, 8, 17, 8, tzinfo=timezone.utc)),
        ("pending", 5, datetime(2026, 8, 17, 8, tzinfo=timezone.utc)),
        ("pending", 1, datetime(2026, 8, 16, 10, tzinfo=timezone.utc)),
    ],
    ids=("permanent-failed", "attempt-limit", "exact-23-hour-cutoff"),
)
def test_due_manager_sql_excludes_unclaimable_delivery_states(
    pool,
    status: str,
    attempts: int,
    created_at: datetime,
) -> None:
    user_id = _seed_user(pool)
    _seed_account(pool, user_id, "managed.account")
    week_start = date(2026, 8, 10)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO weekly_report_preferences (
                user_id, enabled, timezone, send_hour
            ) VALUES (%s, true, 'UTC', 9)
            """,
            (user_id,),
        )
        connection.execute(
            """
            INSERT INTO weekly_report_deliveries (
                user_id, week_start, status, attempts, recipient_email,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                user_id,
                week_start,
                status,
                attempts,
                "manager@example.test",
                created_at,
                created_at,
            ),
        )

    with pool.connection() as connection:
        due = connection.execute(
            _DUE_MANAGERS_SQL,
            {
                "now": now,
                "limit": 10,
                "retry_cutoff": now - timedelta(hours=23),
                "max_attempts": 5,
            },
        ).fetchall()

    assert due == []


def test_expired_sending_lease_can_be_reclaimed(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    first_at = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    first = repository.claim(user_id, week_start, "manager@example.test", first_at)
    assert first is not None

    second = repository.claim(
        user_id,
        week_start,
        "changed@example.test",
        first_at + timedelta(minutes=15),
    )

    assert second is not None
    assert second.delivery_id == first.delivery_id
    assert second.lease_token != first.lease_token
    assert second.recipient_email == "manager@example.test"
    assert second.attempt_number == 2


def test_sent_delivery_is_never_reclaimed(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    claim = repository.claim(user_id, week_start, "manager@example.test", now)
    assert claim is not None
    assert repository.lock_payload(claim.delivery_id, claim.lease_token, "a" * 64, now)
    assert repository.mark_sent(
        claim.delivery_id,
        claim.lease_token,
        "provider-message-1",
        now + timedelta(minutes=1),
    )

    assert (
        repository.claim(
            user_id,
            week_start,
            "manager@example.test",
            now + timedelta(hours=1),
        )
        is None
    )
    row = _delivery_row(pool, user_id, week_start)
    assert row["status"] == "sent"
    assert row["provider_message_id"] == "provider-message-1"
    assert row["sent_at"] == now + timedelta(minutes=1)


def test_retryable_failure_releases_the_claim_with_a_bounded_code(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    claim = repository.claim(user_id, week_start, "manager@example.test", now)
    assert claim is not None

    assert repository.mark_failed(
        claim.delivery_id,
        claim.lease_token,
        "provider_timeout",
        True,
        now + timedelta(minutes=1),
    )
    row = _delivery_row(pool, user_id, week_start)
    assert row["status"] == "pending"
    assert row["last_error_code"] == "provider_timeout"
    assert row["lease_token"] is None
    assert row["lease_expires_at"] is None
    with pytest.raises(ValueError):
        repository.mark_failed(
            claim.delivery_id,
            claim.lease_token,
            "x" * 65,
            True,
            now + timedelta(minutes=2),
        )


def test_permanent_failure_stops_after_one_address_rejection(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    claim = repository.claim(user_id, week_start, "manager@example.test", now)
    assert claim is not None

    assert repository.mark_failed(
        claim.delivery_id,
        claim.lease_token,
        "address_rejected",
        False,
        now + timedelta(minutes=1),
    )

    assert (
        repository.claim(
            user_id,
            week_start,
            "manager@example.test",
            now + timedelta(minutes=2),
        )
        is None
    )
    row = _delivery_row(pool, user_id, week_start)
    assert row["status"] == "failed"
    assert row["attempts"] == 1


def test_attempt_limit_stops_retry_loop(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    first_at = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)

    for attempt in range(1, 6):
        now = first_at + timedelta(minutes=attempt - 1)
        claim = repository.claim(user_id, week_start, "manager@example.test", now)
        assert claim is not None
        assert claim.attempt_number == attempt
        assert repository.mark_failed(
            claim.delivery_id,
            claim.lease_token,
            "provider_timeout",
            True,
            now + timedelta(seconds=1),
        )

    assert (
        repository.claim(
            user_id,
            week_start,
            "manager@example.test",
            first_at + timedelta(minutes=6),
        )
        is None
    )
    row = _delivery_row(pool, user_id, week_start)
    assert row["attempts"] == 5
    assert row["status"] == "failed"


def test_payload_hash_change_stops_retry_before_provider_call(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    first = repository.claim(user_id, week_start, "manager@example.test", now)
    assert first is not None
    assert repository.lock_payload(first.delivery_id, first.lease_token, "a" * 64, now)
    assert repository.mark_failed(
        first.delivery_id,
        first.lease_token,
        "provider_timeout",
        True,
        now + timedelta(minutes=1),
    )
    second = repository.claim(
        user_id,
        week_start,
        "manager@example.test",
        now + timedelta(minutes=2),
    )
    assert second is not None

    assert not repository.lock_payload(
        second.delivery_id,
        second.lease_token,
        "b" * 64,
        now + timedelta(minutes=2),
    )
    row = _delivery_row(pool, user_id, week_start)
    assert row["payload_hash"] == "a" * 64
    assert row["status"] == "failed"
    assert row["last_error_code"] == "payload_hash_mismatch"
    assert row["lease_token"] is None
    assert (
        repository.claim(
            user_id,
            week_start,
            "manager@example.test",
            now + timedelta(minutes=3),
        )
        is None
    )


def test_ambiguous_delivery_is_not_retried_after_idempotency_window(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    first_at = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    claim = repository.claim(user_id, week_start, "manager@example.test", first_at)
    assert claim is not None

    assert (
        repository.claim(
            user_id,
            week_start,
            "manager@example.test",
            first_at + timedelta(hours=23),
        )
        is None
    )
    row = _delivery_row(pool, user_id, week_start)
    assert row["status"] == "failed"
    assert row["last_error_code"] == "idempotency_window_expired"
    assert row["lease_token"] is None


def test_payload_and_finalization_require_current_unexpired_lease(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    week_start = date(2026, 8, 10)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    claim = repository.claim(user_id, week_start, "manager@example.test", now)
    assert claim is not None
    wrong_token = uuid.uuid4()

    assert not repository.lock_payload(claim.delivery_id, wrong_token, "a" * 64, now)
    assert not repository.mark_sent(
        claim.delivery_id, wrong_token, "provider-message-1", now
    )
    assert not repository.mark_failed(
        claim.delivery_id, wrong_token, "provider_timeout", True, now
    )
    assert not repository.lock_payload(
        claim.delivery_id,
        claim.lease_token,
        "a" * 64,
        now + timedelta(minutes=15),
    )
    row = _delivery_row(pool, user_id, week_start)
    assert row["status"] == "sending"
    assert row["payload_hash"] is None


def test_payload_hash_must_be_lowercase_sha256(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    claim = repository.claim(
        user_id, date(2026, 8, 10), "manager@example.test", now
    )
    assert claim is not None

    for invalid in ("A" * 64, "a" * 63, "not-a-digest"):
        with pytest.raises(ValueError):
            repository.lock_payload(
                claim.delivery_id, claim.lease_token, invalid, now
            )


def test_claim_values_do_not_reveal_recipient_or_lease_in_repr(pool) -> None:
    user_id = _seed_user(pool)
    repository = ReportRepository(pool)
    now = datetime(2026, 8, 17, 9, tzinfo=timezone.utc)
    claim = repository.claim(
        user_id, date(2026, 8, 10), "secret-recipient@example.test", now
    )
    assert claim is not None

    rendered = repr(claim)
    assert "secret-recipient@example.test" not in rendered
    assert str(claim.lease_token) not in rendered
    assert rendered == "DeliveryClaim(<redacted>)"
