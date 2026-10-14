from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from engagemate.db.pool import create_pool
from engagemate.mcp.control_repository import ControlRepository, _safe_json

USER_A = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_B = UUID("22222222-2222-4222-8222-222222222222")


POSTGRES_SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY
);
CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    health_status text NOT NULL,
    UNIQUE (id, user_id)
);
CREATE TABLE ig_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    captured_at timestamptz NOT NULL,
    expires_at timestamptz,
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
CREATE TABLE proxies (
    id uuid PRIMARY KEY,
    health_status text NOT NULL DEFAULT 'active'
);
INSERT INTO proxies (id) VALUES ('00000000-0000-4000-8000-000000000001');
CREATE TABLE proxy_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    proxy_id uuid NOT NULL DEFAULT '00000000-0000-4000-8000-000000000001'
        REFERENCES proxies(id),
    bound_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
CREATE TABLE agent_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    version integer NOT NULL DEFAULT 1,
    persona text,
    launched_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE,
    UNIQUE (ig_account_id)
);
CREATE TABLE brand_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'complete',
    follower_audience_approved_at timestamptz
);
CREATE TABLE brand_reference_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_profile_id uuid NOT NULL REFERENCES brand_profiles(id),
    username text NOT NULL,
    profile_url text NOT NULL,
    position integer NOT NULL
);
CREATE TABLE targets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    username text NOT NULL DEFAULT 'fixture-target',
    ig_user_id text DEFAULT 'fixture-target-id',
    tier text NOT NULL DEFAULT 'discovery',
    source text NOT NULL DEFAULT 'test',
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
CREATE TABLE agent_config_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    version integer NOT NULL,
    snapshot jsonb NOT NULL,
    actor_type text NOT NULL,
    actor_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
CREATE TABLE mcp_audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid,
    client_id text,
    tool_name text NOT NULL,
    outcome text NOT NULL,
    before_state jsonb,
    after_state jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
"""


class Result:
    def __init__(self, *, row=None, rows=None) -> None:
        self.row = row
        self.rows = rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, results) -> None:
        self.results = list(results)
        self.calls = []
        self.transaction_entries = 0

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        return self.results.pop(0)

    def transaction(self):
        connection = self

        class Transaction(Context):
            def __enter__(self):
                connection.transaction_entries += 1
                return super().__enter__()

        return Transaction(self)


class Context:
    def __init__(self, value) -> None:
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return None


def repository_with(*results):
    connection = Connection(results)
    pool = SimpleNamespace(connection=lambda: Context(connection))
    return ControlRepository(pool), connection


class _ObservedPool:
    """Expose the worker backend so tests can prove PostgreSQL is blocking it."""

    def __init__(self, pool, entered: Event, backend_pid: list[int]) -> None:
        self.pool = pool
        self.entered = entered
        self.backend_pid = backend_pid

    @contextmanager
    def connection(self):
        with self.pool.connection() as connection:
            self.backend_pid.append(connection.info.backend_pid)
            self.entered.set()
            yield connection


def _wait_for_postgres_blocker(connection, worker_pid: int, blocker_pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        blocked = connection.execute(
            "SELECT %s = ANY(pg_blocking_pids(%s)) AS blocked",
            (blocker_pid, worker_pid),
        ).fetchone()["blocked"]
        if blocked:
            return
        Event().wait(0.01)
    pytest.fail("resume worker never entered a PostgreSQL lock wait")


@pytest.fixture(scope="module")
def postgres_database_url() -> str:
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode:
        pytest.skip("local PostgreSQL is unavailable")
    database = f"engagemate_mcp_control_{os.getpid()}_{uuid4().hex[:8]}"
    created = subprocess.run(["createdb", database], capture_output=True, text=True, check=False)
    if created.returncode:
        pytest.fail(f"could not create disposable PostgreSQL database: {created.stderr}")
    url = os.environ.get("TEST_DATABASE_URL_TEMPLATE", "postgresql:///%s") % database
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(POSTGRES_SCHEMA_SQL)
        yield url
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", database],
            capture_output=True,
            check=False,
        )


@pytest.fixture()
def postgres_resume_world(postgres_database_url: str):
    pool = create_pool(postgres_database_url, min_size=2, max_size=3)
    now = datetime(2026, 9, 5, tzinfo=UTC)
    ids = {
        "user_id": uuid4(),
        "account_id": uuid4(),
        "config_id": uuid4(),
        "target_id": uuid4(),
    }
    with pool.connection() as connection:
        connection.execute("UPDATE proxies SET health_status = 'active'")
        connection.execute("INSERT INTO users (id) VALUES (%s)", (ids["user_id"],))
        connection.execute(
            """
            INSERT INTO ig_accounts (id, user_id, health_status)
            VALUES (%s, %s, 'active')
            """,
            (ids["account_id"], ids["user_id"]),
        )
        connection.execute(
            """
            INSERT INTO ig_sessions (
                user_id, ig_account_id, captured_at, expires_at
            ) VALUES (%s, %s, %s, %s)
            """,
            (
                ids["user_id"],
                ids["account_id"],
                now,
                now + timedelta(hours=1),
            ),
        )
        connection.execute(
            """
            INSERT INTO proxy_bindings (user_id, ig_account_id)
            VALUES (%s, %s)
            """,
            (ids["user_id"], ids["account_id"]),
        )
        connection.execute(
            """
            INSERT INTO agent_configs (
                id, user_id, ig_account_id, enabled, version, persona, launched_at
            ) VALUES (%s, %s, %s, false, 4, 'Direct and useful', %s)
            """,
            (
                ids["config_id"],
                ids["user_id"],
                ids["account_id"],
                now - timedelta(days=1),
            ),
        )
        connection.execute(
            """
            INSERT INTO targets (id, user_id, ig_account_id, enabled)
            VALUES (%s, %s, %s, true)
            """,
            (ids["target_id"], ids["user_id"], ids["account_id"]),
        )
    try:
        yield pool, ids, now
    finally:
        pool.close()


def test_account_lookup_cannot_cross_tenants() -> None:
    repository, connection = repository_with(Result(row=None))

    assert repository.account_status(USER_A, ACCOUNT_B) is None

    sql, params = connection.calls[0]
    assert "account.user_id = %s" in sql
    assert params == (USER_A, ACCOUNT_B)


def test_stats_overview_preserves_missing_snapshot_and_counts_activity() -> None:
    captured = datetime(2026, 9, 1, tzinfo=UTC)
    repository, _ = repository_with(
        Result(
            row={
                "timezone": "America/New_York",
                "snapshot_captured_at": None,
                "followers": None,
                "following": None,
                "likes": 3,
                "replies": 2,
                "dms": 1,
                "views": 8,
                "failed": 4,
            }
        )
    )

    result = repository.stats_overview(USER_A, ACCOUNT_B, captured, captured)

    assert result is not None
    assert result.followers is None
    assert result.following is None
    assert result.likes == 3
    assert result.failed == 4


def test_target_and_source_queries_keep_user_scope() -> None:
    repository, connection = repository_with(Result(rows=[]), Result(rows=[]))

    assert repository.list_targets(USER_A, ACCOUNT_B) == []
    assert repository.list_sources(USER_A, ACCOUNT_B) == []

    for sql, params in connection.calls:
        assert "user_id" in sql
        assert USER_A in params
        assert ACCOUNT_B in params
    source_sql = connection.calls[1][0]
    assert "profile.ig_account_id = %s" in source_sql


def _ready_config(*, enabled: bool = False) -> dict:
    return {
        "id": UUID("33333333-3333-4333-8333-333333333333"),
        "user_id": USER_A,
        "ig_account_id": ACCOUNT_B,
        "enabled": enabled,
        "version": 4,
        "persona": "Direct and useful",
        "launched_at": datetime(2026, 9, 1, tzinfo=UTC),
    }


def _ready_resume_row(*, enabled: bool = False, **overrides) -> dict:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    config = _ready_config(enabled=enabled)
    row = {
        "health_status": "active",
        "config_snapshot": config,
        "launched_at": config["launched_at"],
        "persona": config["persona"],
        "session_id": UUID("66666666-6666-4666-8666-666666666666"),
        "captured_at": now,
        "expires_at": now + timedelta(hours=1),
        "binding_id": UUID("44444444-4444-4444-8444-444444444444"),
        "target_id": UUID("55555555-5555-4555-8555-555555555555"),
    }
    row.update(overrides)
    return row


def test_safe_json_normalizes_sql_ascii_bytes_for_audit_records() -> None:
    assert _safe_json({"persona": b"Direct and useful"}) == {
        "persona": "Direct and useful"
    }


def test_resume_checks_and_updates_under_one_locked_transaction() -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    updated = {**_ready_config(enabled=True), "version": 5}
    repository, connection = repository_with(
        Result(row=_ready_resume_row()),
        Result(row=updated),
        Result(),
        Result(),
    )

    assert repository.resume_automation(USER_A, ACCOUNT_B, "client-test", now) is None
    assert connection.transaction_entries == 1

    readiness_sql, readiness_params = connection.calls[0]
    assert readiness_sql.count("FOR UPDATE") == 6
    assert USER_A in readiness_params and ACCOUNT_B in readiness_params
    assert "UPDATE agent_configs" in connection.calls[1][0]
    assert "INSERT INTO agent_config_versions" in connection.calls[2][0]
    assert "INSERT INTO mcp_audit_events" in connection.calls[3][0]
    assert connection.calls[3][1][2:5] == (
        "client-test",
        "automation.resume",
        "updated",
    )


def test_resume_does_not_write_when_locked_readiness_fails() -> None:
    now = datetime(2026, 9, 5, tzinfo=UTC)
    repository, connection = repository_with(
        Result(row=_ready_resume_row(target_id=None)),
    )

    with pytest.raises(
        ValueError, match="approve your source audience or enable at least one target before resume"
    ):
        repository.resume_automation(USER_A, ACCOUNT_B, "client-test", now)

    assert connection.transaction_entries == 1
    assert not any("UPDATE agent_configs" in sql for sql, _params in connection.calls)


@pytest.mark.parametrize(
    ("mutation_sql", "message"),
    [
        (
            "UPDATE targets SET enabled = false WHERE id = %s",
            "approve your source audience or enable at least one target before resume",
        ),
        (
            ("UPDATE proxies SET health_status = 'degraded' WHERE id = "
            "(SELECT proxy_id FROM proxy_bindings WHERE ig_account_id = %s)"),
            "account proxy is unavailable",
        ),
    ],
)
def test_resume_waits_for_concurrent_readiness_change_then_rejects_without_writes(
    postgres_resume_world, mutation_sql, message
) -> None:
    pool, ids, now = postgres_resume_world
    worker_entered = Event()
    worker_pid: list[int] = []
    repository = ControlRepository(_ObservedPool(pool, worker_entered, worker_pid))

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pool.connection() as blocker:
            mutation_id = (
                ids["target_id"] if mutation_sql.startswith("UPDATE targets")
                else ids["account_id"]
            )
            blocker.execute(mutation_sql, (mutation_id,))
            future = executor.submit(
                repository.resume_automation,
                ids["user_id"],
                ids["account_id"],
                "client-test",
                now,
            )
            assert worker_entered.wait(timeout=2)
            _wait_for_postgres_blocker(
                blocker,
                worker_pid[0],
                blocker.info.backend_pid,
            )

        with pytest.raises(ValueError, match=message):
            future.result(timeout=2)

    with pool.connection() as connection:
        config = connection.execute(
            """
            SELECT enabled, version
              FROM agent_configs
             WHERE user_id = %s AND ig_account_id = %s
            """,
            (ids["user_id"], ids["account_id"]),
        ).fetchone()
        version_count = connection.execute(
            """
            SELECT count(*) AS count
              FROM agent_config_versions
             WHERE user_id = %s AND ig_account_id = %s
            """,
            (ids["user_id"], ids["account_id"]),
        ).fetchone()["count"]
        audit_count = connection.execute(
            """
            SELECT count(*) AS count
              FROM mcp_audit_events
             WHERE user_id = %s AND ig_account_id = %s
               AND client_id = 'client-test'
               AND tool_name = 'automation.resume'
            """,
            (ids["user_id"], ids["account_id"]),
        ).fetchone()["count"]

    assert config == {"enabled": False, "version": 4}
    assert version_count == 0
    assert audit_count == 0


def test_resume_rejects_a_session_older_than_the_worker_limit(
    postgres_resume_world,
) -> None:
    pool, ids, now = postgres_resume_world
    with pool.connection() as connection:
        connection.execute(
            "UPDATE ig_sessions SET captured_at = %s WHERE user_id = %s AND ig_account_id = %s",
            (now - timedelta(days=31), ids["user_id"], ids["account_id"]),
        )

    with pytest.raises(ValueError, match="Instagram session is unavailable"):
        ControlRepository(pool).resume_automation(
            ids["user_id"], ids["account_id"], "client-test", now
        )


def test_resume_rejects_an_enabled_target_without_instagram_identity(
    postgres_resume_world,
) -> None:
    pool, ids, now = postgres_resume_world
    with pool.connection() as connection:
        connection.execute(
            "UPDATE targets SET ig_user_id = NULL WHERE id = %s",
            (ids["target_id"],),
        )

    with pytest.raises(ValueError, match="approve your source audience or enable at least one target before resume"):
        ControlRepository(pool).resume_automation(
            ids["user_id"], ids["account_id"], "client-test", now
        )


def test_resume_rejects_a_binding_to_an_unhealthy_proxy(
    postgres_resume_world,
) -> None:
    pool, ids, now = postgres_resume_world
    with pool.connection() as connection:
        connection.execute(
            "UPDATE proxies SET health_status = 'degraded' WHERE id = "
            "(SELECT proxy_id FROM proxy_bindings WHERE ig_account_id = %s)",
            (ids["account_id"],),
        )

    with pytest.raises(ValueError, match="account proxy is unavailable"):
        ControlRepository(pool).resume_automation(
            ids["user_id"], ids["account_id"], "client-test", now
        )


def test_mcp_created_target_stays_disabled_until_identity_resolution(
    postgres_resume_world,
) -> None:
    pool, ids, _now = postgres_resume_world
    repository = ControlRepository(pool)
    with pool.connection() as connection, connection.transaction():
        result = repository._confirm_audience(
            connection,
            ids["user_id"],
            ids["account_id"],
            "client-test",
            {"username": "unresolved.creator", "tier": "vip"},
            {
                "before_state": ["fixture-target"],
                "after_state": ["fixture-target", "unresolved.creator"],
            },
        )
        stored = connection.execute(
            "SELECT enabled, ig_user_id FROM targets "
            "WHERE user_id = %s AND ig_account_id = %s AND username = %s",
            (ids["user_id"], ids["account_id"], "unresolved.creator"),
        ).fetchone()

    assert result["target"]["enabled"] is False
    assert stored == {"enabled": False, "ig_user_id": None}


@pytest.mark.parametrize(
    ("readiness", "error_type", "message"),
    [
        (
            None,
            LookupError,
            "account not found",
        ),
        (
            _ready_resume_row(health_status="degraded"),
            ValueError,
            "account health does not allow resume",
        ),
        (
            _ready_resume_row(session_id=None, captured_at=None),
            ValueError,
            "Instagram session is unavailable",
        ),
        (
            _ready_resume_row(
                captured_at=datetime(2026, 9, 4, tzinfo=UTC),
                expires_at=datetime(2026, 9, 5, tzinfo=UTC),
            ),
            ValueError,
            "Instagram session is unavailable",
        ),
        (
            _ready_resume_row(binding_id=None),
            ValueError,
            "account proxy is unavailable",
        ),
        (
            _ready_resume_row(config_snapshot=None),
            LookupError,
            "account configuration not found",
        ),
        (
            _ready_resume_row(launched_at=None),
            ValueError,
            "complete onboarding launch before using resume",
        ),
        (
            _ready_resume_row(persona=" "),
            ValueError,
            "save the account voice before resume",
        ),
        (
            _ready_resume_row(persona=b"   "),
            ValueError,
            "save the account voice before resume",
        ),
    ],
)
def test_resume_returns_stable_errors_without_writing(
    readiness, error_type, message
) -> None:
    repository, connection = repository_with(Result(row=readiness))

    with pytest.raises(error_type, match=message):
        repository.resume_automation(
            USER_A,
            ACCOUNT_B,
            "client-test",
            datetime(2026, 9, 5, tzinfo=UTC),
        )

    assert not any("UPDATE agent_configs" in sql for sql, _params in connection.calls)


def test_resume_accepts_approved_sources_and_rejects_revoked_dynamic_targets(postgres_resume_world):
    pool, ids, now = postgres_resume_world
    repository = ControlRepository(pool)
    with pool.connection() as connection:
        connection.execute("UPDATE targets SET source = 'source-followers' WHERE id = %s", (ids["target_id"],))
        profile_id = connection.execute(
            "INSERT INTO brand_profiles (user_id, ig_account_id, follower_audience_approved_at) VALUES (%s, %s, now()) RETURNING id",
            (ids["user_id"], ids["account_id"]),
        ).fetchone()["id"]
        connection.execute(
            "INSERT INTO brand_reference_accounts (brand_profile_id, username, profile_url, position) VALUES (%s, 'source', 'https://www.instagram.com/source/', 0)",
            (profile_id,),
        )
    repository.resume_automation(ids["user_id"], ids["account_id"], "test-client", now)
    with pool.connection() as connection:
        connection.execute("UPDATE brand_profiles SET follower_audience_approved_at = NULL WHERE id = %s", (profile_id,))
    with pytest.raises(ValueError, match="approve your source audience"):
        repository.resume_automation(ids["user_id"], ids["account_id"], "test-client", now)
