"""PostgreSQL contracts for tenant-safe daily growth snapshots."""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import date, datetime, timezone

import psycopg
import pytest

from engagemate.db.pool import create_pool
from engagemate.growth.metrics import ProfileMetrics
from engagemate.growth.repository import DueAccount, GrowthSnapshotRepository


SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY
);

CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    username text NOT NULL,
    follower_count integer,
    health_status text NOT NULL DEFAULT 'active',
    paused_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, user_id)
);

CREATE TABLE agent_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    timezone text NOT NULL DEFAULT 'UTC',
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE,
    UNIQUE (ig_account_id)
);

CREATE TABLE account_growth_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    snapshot_date date NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    follower_count integer,
    following_count integer,
    media_count integer,
    engagement_rate_bps integer,
    posts_sampled integer NOT NULL DEFAULT 0,
    source text NOT NULL DEFAULT 'hypercrawl',
    status text NOT NULL,
    failure_code text,
    CONSTRAINT account_growth_snapshots_account_user_fk
        FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE,
    CONSTRAINT account_growth_snapshots_status_check
        CHECK (status IN ('complete', 'partial', 'unavailable')),
    CONSTRAINT account_growth_snapshots_source_check
        CHECK (source = 'hypercrawl'),
    CONSTRAINT account_growth_snapshots_counts_check
        CHECK ((follower_count IS NULL OR follower_count >= 0)
            AND (following_count IS NULL OR following_count >= 0)
            AND (media_count IS NULL OR media_count >= 0)
            AND (engagement_rate_bps IS NULL OR engagement_rate_bps >= 0)
            AND posts_sampled >= 0),
    CONSTRAINT account_growth_snapshots_follower_status_check
        CHECK ((status = 'unavailable' AND follower_count IS NULL)
            OR (status IN ('complete', 'partial') AND follower_count IS NOT NULL)),
    CONSTRAINT account_growth_snapshots_failure_code_check
        CHECK (failure_code IS NULL OR char_length(failure_code) <= 64),
    UNIQUE (ig_account_id, snapshot_date)
);
"""


@pytest.fixture(scope="module")
def database_url() -> str:
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode != 0:
        pytest.skip("local PostgreSQL is unavailable")

    database = f"engagemate_growth_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    created = subprocess.run(
        ["createdb", database], capture_output=True, text=True, check=False
    )
    if created.returncode != 0:
        pytest.fail(f"could not create disposable PostgreSQL database: {created.stderr}")

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
    pool = create_pool(database_url, min_size=1, max_size=4)
    try:
        with pool.connection() as connection:
            connection.execute(
                "TRUNCATE account_growth_snapshots, agent_configs, "
                "ig_accounts, users CASCADE"
            )
        yield pool
    finally:
        pool.close()


def _seed_account(
    pool,
    *,
    user_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    username: str = "managed.account",
    enabled: bool = True,
    timezone_name: str = "UTC",
    health_status: str = "active",
    paused: bool = False,
) -> DueAccount:
    owner_id = user_id or uuid.uuid4()
    managed_account_id = account_id or uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING", (owner_id,)
        )
        connection.execute(
            """
            INSERT INTO ig_accounts (
                id, user_id, username, health_status, paused_at
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (
                managed_account_id,
                owner_id,
                username,
                health_status,
                datetime(2026, 8, 1, tzinfo=timezone.utc) if paused else None,
            ),
        )
        connection.execute(
            """
            INSERT INTO agent_configs (
                user_id, ig_account_id, enabled, timezone
            ) VALUES (%s, %s, %s, %s)
            """,
            (owner_id, managed_account_id, enabled, timezone_name),
        )
    return DueAccount(
        owner_id,
        managed_account_id,
        username,
        timezone_name,
        date(2026, 8, 14),
    )


def _snapshot(pool, account_id: uuid.UUID) -> dict:
    with pool.connection() as connection:
        return connection.execute(
            """
            SELECT snapshot_date, captured_at, follower_count, following_count,
                   media_count, engagement_rate_bps, posts_sampled, source,
                   status, failure_code
            FROM account_growth_snapshots
            WHERE ig_account_id = %s
            """,
            (account_id,),
        ).fetchone()


def _text(value: str | bytes) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else value


COMPLETE = ProfileMetrics(1200, 300, 50, 425, 12, "complete")
PARTIAL = ProfileMetrics(1100, None, 48, None, 0, "partial")
UNAVAILABLE = ProfileMetrics(None, None, None, None, 0, "unavailable")


def test_list_due_uses_each_enabled_accounts_local_date(pool) -> None:
    owner_id = uuid.uuid4()
    los_angeles = _seed_account(
        pool,
        user_id=owner_id,
        username="la.account",
        timezone_name="America/Los_Angeles",
    )
    kolkata = _seed_account(
        pool,
        user_id=owner_id,
        username="kolkata.account",
        timezone_name="Asia/Kolkata",
    )
    other_tenant = _seed_account(pool, username="other.account")
    _seed_account(pool, user_id=owner_id, username="disabled", enabled=False)
    _seed_account(
        pool,
        user_id=owner_id,
        username="paused",
        paused=True,
    )
    _seed_account(
        pool,
        user_id=owner_id,
        username="challenged",
        health_status="challenged",
    )
    _seed_account(
        pool,
        user_id=owner_id,
        username="degraded",
        health_status="degraded",
    )
    _seed_account(
        pool,
        user_id=owner_id,
        username="bad-zone",
        timezone_name="Not/A_Zone",
    )

    repository = GrowthSnapshotRepository(pool)
    captured_at = datetime(2026, 8, 14, 1, tzinfo=timezone.utc)
    repository.upsert(
        DueAccount(
            los_angeles.user_id,
            los_angeles.ig_account_id,
            los_angeles.username,
            los_angeles.timezone,
            date(2026, 8, 13),
        ),
        PARTIAL,
        captured_at,
    )
    repository.upsert(kolkata, UNAVAILABLE, captured_at, "provider_unavailable")

    due = repository.list_due(captured_at, 25, user_id=owner_id)

    assert due == [
        DueAccount(
            kolkata.user_id,
            kolkata.ig_account_id,
            "kolkata.account",
            "Asia/Kolkata",
            date(2026, 8, 14),
        )
    ]
    assert all(account.user_id != other_tenant.user_id for account in due)


def test_upsert_inserts_one_snapshot_and_updates_account_followers(pool) -> None:
    account = _seed_account(pool)
    captured_at = datetime(2026, 8, 14, 2, 30, tzinfo=timezone.utc)

    accepted = GrowthSnapshotRepository(pool).upsert(account, COMPLETE, captured_at)

    assert accepted is True
    row = _snapshot(pool, account.ig_account_id)
    assert row["snapshot_date"] == date(2026, 8, 14)
    assert row["captured_at"] == captured_at
    assert row["follower_count"] == 1200
    assert row["following_count"] == 300
    assert row["media_count"] == 50
    assert row["engagement_rate_bps"] == 425
    assert row["posts_sampled"] == 12
    assert _text(row["source"]) == "hypercrawl"
    assert _text(row["status"]) == "complete"
    assert row["failure_code"] is None
    with pool.connection() as connection:
        follower_count = connection.execute(
            "SELECT follower_count FROM ig_accounts WHERE id = %s",
            (account.ig_account_id,),
        ).fetchone()["follower_count"]
    assert follower_count == 1200


def test_same_day_complete_snapshot_is_idempotent(pool) -> None:
    account = _seed_account(pool)
    repository = GrowthSnapshotRepository(pool)
    first_capture = datetime(2026, 8, 14, 2, tzinfo=timezone.utc)
    second_capture = datetime(2026, 8, 14, 3, tzinfo=timezone.utc)
    assert repository.upsert(account, COMPLETE, first_capture) is True

    accepted = repository.upsert(
        account,
        ProfileMetrics(9999, 1, 1, 1, 1, "complete"),
        second_capture,
    )

    assert accepted is False
    row = _snapshot(pool, account.ig_account_id)
    assert row["captured_at"] == first_capture
    assert row["follower_count"] == 1200
    with pool.connection() as connection:
        follower_count = connection.execute(
            "SELECT follower_count FROM ig_accounts WHERE id = %s",
            (account.ig_account_id,),
        ).fetchone()["follower_count"]
    assert follower_count == 1200


def test_complete_replaces_same_day_unavailable_snapshot(pool) -> None:
    account = _seed_account(pool)
    repository = GrowthSnapshotRepository(pool)
    assert repository.upsert(
        account,
        UNAVAILABLE,
        datetime(2026, 8, 14, 2, tzinfo=timezone.utc),
        "provider_unavailable",
    )

    accepted = repository.upsert(
        account,
        COMPLETE,
        datetime(2026, 8, 14, 3, tzinfo=timezone.utc),
    )

    assert accepted is True
    row = _snapshot(pool, account.ig_account_id)
    assert _text(row["status"]) == "complete"
    assert row["follower_count"] == 1200
    assert row["failure_code"] is None


@pytest.mark.parametrize(
    ("existing", "incoming"),
    [(UNAVAILABLE, PARTIAL), (PARTIAL, COMPLETE)],
)
def test_higher_rank_valid_snapshot_replaces_lower_rank_snapshot(
    pool,
    existing: ProfileMetrics,
    incoming: ProfileMetrics,
) -> None:
    account = _seed_account(pool)
    repository = GrowthSnapshotRepository(pool)
    assert repository.upsert(
        account,
        existing,
        datetime(2026, 8, 14, 2, tzinfo=timezone.utc),
        "invalid_profile" if existing.status == "unavailable" else None,
    )

    accepted = repository.upsert(
        account,
        incoming,
        datetime(2026, 8, 14, 3, tzinfo=timezone.utc),
    )

    assert accepted is True
    row = _snapshot(pool, account.ig_account_id)
    assert _text(row["status"]) == incoming.status
    assert row["follower_count"] == incoming.follower_count


@pytest.mark.parametrize("existing", [PARTIAL, COMPLETE])
def test_unavailable_does_not_replace_same_day_partial_or_complete_snapshot(
    pool, existing: ProfileMetrics
) -> None:
    account = _seed_account(pool)
    repository = GrowthSnapshotRepository(pool)
    assert repository.upsert(
        account,
        existing,
        datetime(2026, 8, 14, 2, tzinfo=timezone.utc),
    )

    accepted = repository.upsert(
        account,
        UNAVAILABLE,
        datetime(2026, 8, 14, 3, tzinfo=timezone.utc),
        "provider_unavailable",
    )

    assert accepted is False
    row = _snapshot(pool, account.ig_account_id)
    assert _text(row["status"]) == existing.status
    assert row["follower_count"] == existing.follower_count
    assert row["failure_code"] is None


def test_cross_tenant_account_write_is_rejected(pool) -> None:
    account = _seed_account(pool)
    other_owner = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute("INSERT INTO users (id) VALUES (%s)", (other_owner,))
    mismatched = DueAccount(
        other_owner,
        account.ig_account_id,
        account.username,
        account.timezone,
        account.snapshot_date,
    )

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        GrowthSnapshotRepository(pool).upsert(
            mismatched,
            COMPLETE,
            datetime(2026, 8, 14, 2, tzinfo=timezone.utc),
        )

    assert _snapshot(pool, account.ig_account_id) is None


def test_list_due_prioritizes_first_attempts_then_rotates_oldest_retries(pool) -> None:
    owner_id = uuid.uuid4()
    fresh = [
        _seed_account(
            pool,
            user_id=owner_id,
            account_id=uuid.UUID(f"10000000-0000-0000-0000-00000000010{index}"),
            username=f"fresh.{index}",
        )
        for index in range(1, 4)
    ]
    retries = [
        _seed_account(
            pool,
            user_id=owner_id,
            account_id=uuid.UUID(f"20000000-0000-0000-0000-00000000020{index}"),
            username=f"retry.{index}",
        )
        for index in range(1, 4)
    ]
    repository = GrowthSnapshotRepository(pool)
    retry_times = [
        datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
        datetime(2026, 8, 14, 3, tzinfo=timezone.utc),
        datetime(2026, 8, 14, 4, tzinfo=timezone.utc),
    ]
    for account, captured_at in zip(retries, retry_times, strict=True):
        assert repository.upsert(
            account,
            UNAVAILABLE,
            captured_at,
            "provider_unavailable",
        )

    now = datetime(2026, 8, 14, 6, tzinfo=timezone.utc)
    first_batch = repository.list_due(now, 2, user_id=owner_id)

    assert [account.ig_account_id for account in first_batch] == [
        fresh[0].ig_account_id,
        fresh[1].ig_account_id,
    ]

    for account in fresh[:2]:
        assert repository.upsert(account, PARTIAL, now)
    second_batch = repository.list_due(now, 2, user_id=owner_id)

    assert [account.ig_account_id for account in second_batch] == [
        fresh[2].ig_account_id,
        retries[1].ig_account_id,
    ]

    assert repository.upsert(fresh[2], PARTIAL, now)
    retry_batch = repository.list_due(now, 2, user_id=owner_id)

    assert [account.ig_account_id for account in retry_batch] == [
        retries[1].ig_account_id,
        retries[2].ig_account_id,
    ]

    assert repository.upsert(
        retries[1],
        UNAVAILABLE,
        datetime(2026, 8, 14, 7, tzinfo=timezone.utc),
        "provider_unavailable",
    )
    rotated_batch = repository.list_due(now, 2, user_id=owner_id)

    assert [account.ig_account_id for account in rotated_batch] == [
        retries[2].ig_account_id,
        retries[0].ig_account_id,
    ]
