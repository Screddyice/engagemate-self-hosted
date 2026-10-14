"""Task 13 — schedule due accounts without duplicate jobs."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg
import pytest
from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.scheduler.main import (
    build_capacity_controller,
    interval_seconds,
    run_loop,
)
from engagemate.scheduler.service import (
    Scheduler,
    build_schedule_key,
    build_view_schedule_key,
    resolve_schedule_slot,
    resolve_view_schedule_slots,
)

SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY
);

CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    health_status text NOT NULL DEFAULT 'active',
    paused_at timestamptz,
    UNIQUE (id, user_id)
);

CREATE TABLE agent_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    timezone text NOT NULL DEFAULT 'UTC',
    schedule_hour integer NOT NULL DEFAULT 9,
    daily_view_limit integer NOT NULL DEFAULT 0,
    UNIQUE (ig_account_id)
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    schedule_key text NOT NULL UNIQUE,
    scheduled_for timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    claimed_by text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT jobs_status_check
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled'))
);
CREATE TABLE proxies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    health_status text NOT NULL DEFAULT 'active'
);
CREATE TABLE proxy_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    proxy_id uuid NOT NULL REFERENCES proxies(id),
    released_at timestamptz
);
"""


@pytest.fixture(scope="module")
def database_url() -> str:
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode != 0:
        pytest.skip("local PostgreSQL is unavailable")

    database = f"engagemate_sched_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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
    pool = create_pool(database_url, min_size=1, max_size=2)
    try:
        with pool.connection() as connection:
            connection.execute(
                "TRUNCATE jobs, proxy_bindings, proxies, agent_configs, "
                "ig_accounts, users CASCADE"
            )
        yield pool
    finally:
        pool.close()


def _seed_account(
    pool,
    *,
    health: str = "active",
    enabled: bool = True,
    tz: str = "UTC",
    schedule_hour: int = 9,
    daily_view_limit: int = 0,
    paused: bool = False,
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    with pool.connection() as connection:
        connection.execute("INSERT INTO users (id) VALUES (%s)", (user_id,))
        connection.execute(
            "INSERT INTO ig_accounts (id, user_id, health_status, paused_at) "
            "VALUES (%s, %s, %s, CASE WHEN %s THEN now() ELSE NULL END)",
            (account_id, user_id, health, paused),
        )
        connection.execute(
            "INSERT INTO agent_configs "
            "(user_id, ig_account_id, enabled, timezone, schedule_hour, daily_view_limit) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, account_id, enabled, tz, schedule_hour, daily_view_limit),
        )
    return user_id, account_id


def _text(value: str | bytes) -> str:
    """Normalize text from UTF-8 and SQL_ASCII PostgreSQL clusters."""

    return value.decode("utf-8") if isinstance(value, bytes) else value


def _list_jobs(pool) -> list[dict]:
    with pool.connection() as connection:
        rows = connection.execute(
            "SELECT user_id, ig_account_id, schedule_key, scheduled_for, status "
            "FROM jobs ORDER BY schedule_key"
        ).fetchall()
    return [
        {
            "user_id": row["user_id"],
            "ig_account_id": row["ig_account_id"],
            "schedule_key": _text(row["schedule_key"]),
            "scheduled_for": row["scheduled_for"],
            "status": _text(row["status"]),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_schedule_key_is_stable_and_deterministic() -> None:
    account_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    key = build_schedule_key(account_id, "2026-07-31", 9)
    assert key == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:2026-07-31:9"
    assert build_schedule_key(account_id, "2026-07-31", 9) == key


def test_resolve_slot_when_local_hour_has_arrived() -> None:
    # 17:00 UTC = 10:00 America/Los_Angeles on 2026-07-31 (PDT, UTC-7)
    now = datetime(2026, 7, 31, 17, 0, tzinfo=timezone.utc)
    slot = resolve_schedule_slot(now, "America/Los_Angeles", 9)
    assert slot is not None
    assert slot.local_date_iso == "2026-07-31"
    assert slot.schedule_hour == 9
    assert slot.scheduled_for == datetime(
        2026, 7, 31, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")
    ).astimezone(timezone.utc)


def test_resolve_slot_before_schedule_hour_is_not_due() -> None:
    # 15:00 UTC = 08:00 America/Los_Angeles — before 09:00
    now = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)
    assert resolve_schedule_slot(now, "America/Los_Angeles", 9) is None


def test_resolve_slot_rejects_unknown_timezone() -> None:
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    assert resolve_schedule_slot(now, "Not/A_Zone", 9) is None


def test_resolve_slot_at_exact_schedule_hour_is_due() -> None:
    now = datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)
    slot = resolve_schedule_slot(now, "UTC", 9)
    assert slot is not None
    assert slot.scheduled_for == datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)


def test_view_slots_follow_the_account_local_day() -> None:
    # 20:30 UTC = 13:30 PDT. Midnight, 06:00, and 12:00 local slots are due.
    now = datetime(2026, 7, 31, 20, 30, tzinfo=timezone.utc)
    slots = resolve_view_schedule_slots(now, "America/Los_Angeles", 100)

    assert [slot.schedule_hour for slot in slots] == [0, 6, 12]
    assert all(slot.view_only for slot in slots)
    assert all(slot.local_date_iso == "2026-07-31" for slot in slots)
    assert build_view_schedule_key(
        uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        "2026-07-31",
        12,
    ) == "views:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:2026-07-31:12"


def test_view_slots_are_empty_when_the_saved_ceiling_is_zero() -> None:
    now = datetime(2026, 7, 31, 20, 30, tzinfo=timezone.utc)
    assert resolve_view_schedule_slots(now, "America/Los_Angeles", 0) == ()


# ---------------------------------------------------------------------------
# Integration: enqueue_due
# ---------------------------------------------------------------------------


def test_enabled_active_account_enqueues_at_local_schedule_hour(pool) -> None:
    user_id, account_id = _seed_account(
        pool, health="active", enabled=True, tz="UTC", schedule_hour=9
    )
    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 10, 30, tzinfo=timezone.utc)

    inserted = scheduler.enqueue_due(now)

    assert inserted == 1
    jobs = _list_jobs(pool)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["user_id"] == user_id
    assert job["ig_account_id"] == account_id
    assert job["status"] == "queued"
    assert job["schedule_key"] == build_schedule_key(account_id, "2026-07-31", 9)
    scheduled = job["scheduled_for"]
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    assert scheduled == datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc)


def test_view_ceiling_enqueues_due_local_view_slots(pool) -> None:
    _user_id, account_id = _seed_account(
        pool,
        health="active",
        enabled=True,
        tz="America/Los_Angeles",
        schedule_hour=9,
        daily_view_limit=100,
    )
    now = datetime(2026, 7, 31, 20, 30, tzinfo=timezone.utc)

    assert Scheduler(pool).enqueue_due(now) == 4
    jobs = _list_jobs(pool)
    assert {job["schedule_key"] for job in jobs} == {
        build_schedule_key(account_id, "2026-07-31", 9),
        build_view_schedule_key(account_id, "2026-07-31", 0),
        build_view_schedule_key(account_id, "2026-07-31", 6),
        build_view_schedule_key(account_id, "2026-07-31", 12),
    }
    # A second scheduler tick is idempotent for the same local windows.
    assert Scheduler(pool).enqueue_due(now) == 0


def test_managed_scheduler_waits_for_binding_after_capacity_tick(pool) -> None:
    user_id, account_id = _seed_account(
        pool, health="active", enabled=True, tz="UTC", schedule_hour=9
    )
    ticks: list[str] = []
    scheduler = Scheduler(
        pool,
        capacity_tick=lambda: ticks.append("capacity") or 0,
        require_proxy_binding=True,
    )
    now = datetime(2026, 7, 31, 10, 30, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 0
    assert ticks == ["capacity"]
    with pool.connection() as connection:
        proxy_id = connection.execute(
            "INSERT INTO proxies DEFAULT VALUES RETURNING id"
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id)
            VALUES (%s, %s, %s)
            """,
            (user_id, account_id, proxy_id),
        )

    assert scheduler.enqueue_due(now) == 1
    assert ticks == ["capacity", "capacity"]


def test_managed_scheduler_rejects_degraded_proxy_binding(pool) -> None:
    user_id, account_id = _seed_account(
        pool, health="active", enabled=True, tz="UTC", schedule_hour=9
    )
    with pool.connection() as connection:
        proxy_id = connection.execute(
            "INSERT INTO proxies (health_status) VALUES ('degraded') RETURNING id"
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id)
            VALUES (%s, %s, %s)
            """,
            (user_id, account_id, proxy_id),
        )
    scheduler = Scheduler(pool, require_proxy_binding=True)

    assert (
        scheduler.enqueue_due(datetime(2026, 7, 31, 10, 30, tzinfo=timezone.utc)) == 0
    )


def test_degraded_account_also_enqueues(pool) -> None:
    _seed_account(pool, health="degraded", enabled=True, tz="UTC", schedule_hour=9)
    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 1


def test_paused_account_does_not_enqueue_even_when_health_is_active(pool) -> None:
    _seed_account(pool, health="active", enabled=True, tz="UTC", schedule_hour=9, paused=True)
    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 0
    assert _list_jobs(pool) == []


@pytest.mark.parametrize("health", ["paused", "challenged", "banned"])
def test_unhealthy_accounts_do_not_enqueue(pool, health: str) -> None:
    _seed_account(pool, health=health, enabled=True, tz="UTC", schedule_hour=9)
    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 0
    assert _list_jobs(pool) == []


def test_disabled_agent_config_does_not_enqueue(pool) -> None:
    _seed_account(pool, health="active", enabled=False, tz="UTC", schedule_hour=9)
    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 0
    assert _list_jobs(pool) == []


def test_before_local_schedule_hour_does_not_enqueue(pool) -> None:
    _seed_account(
        pool, health="active", enabled=True, tz="America/Los_Angeles", schedule_hour=9
    )
    scheduler = Scheduler(pool)
    # 08:00 local PDT
    now = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 0
    assert _list_jobs(pool) == []


def test_enqueue_is_idempotent_for_same_schedule_window(pool) -> None:
    _seed_account(pool, health="active", enabled=True, tz="UTC", schedule_hour=9)
    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 1
    assert scheduler.enqueue_due(now) == 0
    assert scheduler.enqueue_due(now) == 0
    assert len(_list_jobs(pool)) == 1


def test_unique_schedule_key_prevents_duplicate_rows(pool) -> None:
    """ON CONFLICT (schedule_key) DO NOTHING keeps a second insert from landing."""
    user_id, account_id = _seed_account(
        pool, health="active", enabled=True, tz="UTC", schedule_hour=9
    )
    key = build_schedule_key(account_id, "2026-07-31", 9)
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO jobs (user_id, ig_account_id, schedule_key, scheduled_for, status)
            VALUES (%s, %s, %s, %s, 'queued')
            """,
            (
                user_id,
                account_id,
                key,
                datetime(2026, 7, 31, 9, 0, tzinfo=timezone.utc),
            ),
        )

    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    assert scheduler.enqueue_due(now) == 0
    assert len(_list_jobs(pool)) == 1


def test_invalid_timezone_is_skipped_without_blocking_others(pool) -> None:
    bad_user, bad_account = _seed_account(
        pool, health="active", enabled=True, tz="Not/A_Real_Zone", schedule_hour=9
    )
    good_user, good_account = _seed_account(
        pool, health="active", enabled=True, tz="UTC", schedule_hour=9
    )
    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 1
    jobs = _list_jobs(pool)
    assert len(jobs) == 1
    assert jobs[0]["ig_account_id"] == good_account
    assert jobs[0]["user_id"] == good_user
    assert jobs[0]["ig_account_id"] != bad_account


def test_local_timezone_date_boundary_uses_account_calendar(pool) -> None:
    # 2026-08-01 02:00 UTC = 2026-07-31 19:00 America/Los_Angeles — still July 31
    user_id, account_id = _seed_account(
        pool,
        health="active",
        enabled=True,
        tz="America/Los_Angeles",
        schedule_hour=9,
    )
    scheduler = Scheduler(pool)
    now = datetime(2026, 8, 1, 2, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 1
    jobs = _list_jobs(pool)
    assert jobs[0]["schedule_key"] == build_schedule_key(account_id, "2026-07-31", 9)
    scheduled = jobs[0]["scheduled_for"]
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    expected = datetime(
        2026, 7, 31, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles")
    ).astimezone(timezone.utc)
    assert scheduled == expected
    assert jobs[0]["user_id"] == user_id


def test_two_due_accounts_each_get_one_job(pool) -> None:
    _seed_account(pool, health="active", enabled=True, tz="UTC", schedule_hour=8)
    _seed_account(pool, health="degraded", enabled=True, tz="UTC", schedule_hour=9)
    scheduler = Scheduler(pool)
    now = datetime(2026, 7, 31, 10, 0, tzinfo=timezone.utc)

    assert scheduler.enqueue_due(now) == 2
    assert len(_list_jobs(pool)) == 2


def test_naive_now_is_rejected() -> None:
    # Pure unit path: Scheduler requires an aware UTC-comparable instant.
    class _FakePool:
        def connection(self):  # pragma: no cover - must not be reached
            raise AssertionError("pool must not be used for naive now")

    scheduler = Scheduler(_FakePool())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.enqueue_due(datetime(2026, 7, 31, 10, 0))


# ---------------------------------------------------------------------------
# CLI loop helpers
# ---------------------------------------------------------------------------


class _CountingScheduler:
    def __init__(self, results: list[int | Exception]) -> None:
        self._results = list(results)
        self.calls = 0
        self.nows: list[datetime] = []

    def enqueue_due(self, now: datetime) -> int:
        self.calls += 1
        self.nows.append(now)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_run_loop_ticks_until_stopped() -> None:
    scheduler = _CountingScheduler([1, 0, 2])
    slept: list[float] = []
    fixed = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)

    run_loop(
        scheduler,  # type: ignore[arg-type]
        interval_seconds=60.0,
        sleep=slept.append,
        # Gate on completed ticks so the mid-loop stop check is safe.
        should_continue=lambda: scheduler.calls < 3,
        now_fn=lambda: fixed,
    )

    assert scheduler.calls == 3
    # Sleep runs only between ticks; the final tick stops without sleeping.
    assert slept == [60.0, 60.0]
    assert scheduler.nows == [fixed, fixed, fixed]


def test_run_loop_survives_a_failing_tick() -> None:
    scheduler = _CountingScheduler([RuntimeError("boom"), 1])
    slept: list[float] = []

    run_loop(
        scheduler,  # type: ignore[arg-type]
        interval_seconds=30.0,
        sleep=slept.append,
        should_continue=lambda: scheduler.calls < 2,
        now_fn=lambda: datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
    )

    assert scheduler.calls == 2
    assert slept == [30.0]


def test_interval_seconds_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHEDULER_INTERVAL_SECONDS", raising=False)
    # 600, not 60: below Neon's 300s suspend threshold the compute never idles
    # and bills ~730 h/month with no users connected.
    assert interval_seconds() == 600.0

    monkeypatch.setenv("SCHEDULER_INTERVAL_SECONDS", "15.5")
    assert interval_seconds() == 15.5

    monkeypatch.setenv("SCHEDULER_INTERVAL_SECONDS", "0")
    with pytest.raises(ValueError):
        interval_seconds()


def test_managed_capacity_builder_requires_scheduler_purchase_token(pool) -> None:
    settings = Settings(
        DATABASE_URL="postgresql://localhost/test",
        SESSION_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32))
        .decode()
        .rstrip("="),
        INTERNAL_API_KEY="internal-test-key",
        IPROYAL_MODE="managed",
        IPROYAL_GEO_CATALOG_JSON=json.dumps(
            [
                {
                    "country": "US",
                    "region": "CA",
                    "city": "San Francisco",
                    "product_id": 9,
                    "plan_id": 4,
                    "location_id": 51,
                }
            ]
        ),
        PROXY_HEALTH_URL="https://example.test/identity",
        _env_file=None,
    )

    with pytest.raises(ValueError, match="purchase token unavailable"):
        build_capacity_controller(settings, pool)
