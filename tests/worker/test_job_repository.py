from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from pydantic import ValidationError


ROOT_DIR = Path(__file__).resolve().parents[2]
API_DIR = ROOT_DIR / "api"
sys.path.insert(0, str(API_DIR))

from engagemate.config import Settings
from engagemate.db.jobs import JobRepository
from engagemate.db.pool import create_pool
from engagemate.domain.models import JobStatus


SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY
);

CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id)
);

CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL REFERENCES ig_accounts(id),
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

CREATE INDEX jobs_status_scheduled_for_idx
    ON jobs (status, scheduled_for);
"""


@pytest.fixture(scope="module")
def database_url() -> str:
    if subprocess.run(
        ["pg_isready"], capture_output=True, check=False
    ).returncode != 0:
        pytest.skip("local PostgreSQL is unavailable")

    database = f"engagemate_jobs_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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
def repository_pair(database_url: str):
    pool_a = create_pool(database_url, min_size=1, max_size=2)
    pool_b = create_pool(database_url, min_size=1, max_size=2)
    try:
        with pool_a.connection() as connection:
            connection.execute("TRUNCATE jobs, ig_accounts, users CASCADE")
            user_id = uuid.uuid4()
            account_id = uuid.uuid4()
            connection.execute("INSERT INTO users (id) VALUES (%s)", (user_id,))
            connection.execute(
                "INSERT INTO ig_accounts (id, user_id) VALUES (%s, %s)",
                (account_id, user_id),
            )
        yield (
            JobRepository(pool_a),
            JobRepository(pool_b),
            user_id,
            account_id,
        )
    finally:
        pool_a.close()
        pool_b.close()


def _insert_job(
    repository: JobRepository,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    schedule_key: str,
    *,
    status: JobStatus = JobStatus.QUEUED,
    claimed_by: str | None = None,
    attempts: int = 0,
    lease: str = "1 minute",
    scheduled: str = "-1 minute",
) -> uuid.UUID:
    job_id = uuid.uuid4()
    with repository.pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO jobs (
                id, user_id, ig_account_id, schedule_key, scheduled_for,
                status, claimed_by, lease_expires_at, heartbeat_at, attempts
            )
            VALUES (
                %(id)s, %(user_id)s, %(account_id)s, %(schedule_key)s,
                now() + %(scheduled)s::interval, %(status)s, %(claimed_by)s,
                CASE
                    WHEN %(status)s = 'running' THEN now() + %(lease)s::interval
                    ELSE NULL
                END,
                CASE WHEN %(status)s = 'running' THEN now() ELSE NULL END,
                %(attempts)s
            )
            """,
            {
                "id": job_id,
                "user_id": user_id,
                "account_id": account_id,
                "schedule_key": schedule_key,
                "status": status.value,
                "claimed_by": claimed_by,
                "lease": lease,
                "scheduled": scheduled,
                "attempts": attempts,
            },
        )
    return job_id


def test_settings_require_all_runtime_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DATABASE_URL", "SESSION_ENCRYPTION_KEY", "INTERNAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)

    missing = {item["loc"][0] for item in error.value.errors()}
    assert missing == {
        "DATABASE_URL",
        "SESSION_ENCRYPTION_KEY",
        "INTERNAL_API_KEY",
    }


def test_settings_use_locked_worker_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/engagemate")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", "not-a-real-key")
    monkeypatch.setenv("INTERNAL_API_KEY", "not-a-real-api-key")

    settings = Settings(_env_file=None)

    assert settings.job_lease_seconds == 120
    assert settings.heartbeat_interval_seconds == 30
    assert settings.triage_model == "claude-haiku-4-5"
    assert settings.decision_model == "claude-opus-4-8"
    assert "not-a-real" not in repr(settings)


def test_settings_reject_heartbeat_that_can_outlive_the_job_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/engagemate")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", "not-a-real-key")
    monkeypatch.setenv("INTERNAL_API_KEY", "not-a-real-api-key")
    monkeypatch.setenv("JOB_LEASE_SECONDS", "120")
    monkeypatch.setenv("HEARTBEAT_INTERVAL_SECONDS", "75")

    with pytest.raises(ValidationError, match="HEARTBEAT_INTERVAL_SECONDS"):
        Settings(_env_file=None)


def test_concurrent_claims_are_distinct_and_enforce_worker_ownership(
    repository_pair,
) -> None:
    repo_a, repo_b, user_id, account_id = repository_pair
    _insert_job(repo_a, user_id, account_id, "due-a")
    _insert_job(repo_a, user_id, account_id, "due-b")
    barrier = threading.Barrier(2)

    def claim(repository: JobRepository, worker_id: str):
        barrier.wait()
        return repository.claim_next(worker_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(claim, repo_a, "worker-a")
        second_future = executor.submit(claim, repo_b, "worker-b")
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    assert first is not None
    assert second is not None
    assert first.id != second.id
    assert first.status is JobStatus.RUNNING
    assert second.status is JobStatus.RUNNING

    assert repo_a.heartbeat(first.id, "worker-a") is True
    assert repo_b.heartbeat(first.id, "worker-b") is False
    assert repo_b.finish(first.id, "worker-b", JobStatus.FAILED) is False
    assert repo_a.finish(first.id, "worker-a", JobStatus.COMPLETED) is True
    assert repo_a.heartbeat(first.id, "worker-a") is False


def test_claim_recovers_an_expired_lease_and_increments_attempts(
    repository_pair,
) -> None:
    repo_a, _, user_id, account_id = repository_pair
    expired_id = _insert_job(
        repo_a,
        user_id,
        account_id,
        "expired",
        status=JobStatus.RUNNING,
        claimed_by="dead-worker",
        attempts=1,
        lease="-1 minute",
    )

    recovered = repo_a.claim_next("replacement-worker")

    assert recovered is not None
    assert recovered.id == expired_id
    assert recovered.claimed_by == "replacement-worker"
    assert recovered.attempts == 2
    assert recovered.status is JobStatus.RUNNING


def test_normal_worker_skips_operator_canary_jobs(repository_pair) -> None:
    repo_a, _, user_id, account_id = repository_pair
    repository = JobRepository(repo_a.pool)
    operator_id = _insert_job(
        repository, user_id, account_id, "operator-reply-live", scheduled="-2 minutes"
    )
    campaign_id = _insert_job(
        repository, user_id, account_id, "campaign-live", scheduled="-1 minute"
    )

    claimed = repository.claim_next("campaign-worker")

    assert claimed is not None and claimed.id == campaign_id
    with repo_a.pool.connection() as connection:
        operator = connection.execute(
            "SELECT status, attempts, claimed_by FROM jobs WHERE id=%s",
            (operator_id,),
        ).fetchone()
    status = operator["status"].decode() if isinstance(operator["status"], bytes) else operator["status"]
    assert (status, operator["attempts"], operator["claimed_by"]) == ("queued", 0, None)


def test_expired_owner_cannot_finish_before_or_after_recovery(
    repository_pair,
) -> None:
    repo_a, repo_b, user_id, account_id = repository_pair
    expired_id = _insert_job(
        repo_a,
        user_id,
        account_id,
        "expired-owner",
        status=JobStatus.RUNNING,
        claimed_by="original-worker",
        attempts=1,
        lease="-1 minute",
    )

    assert (
        repo_a.finish(expired_id, "original-worker", JobStatus.COMPLETED)
        is False
    )

    recovered = repo_b.claim_next("replacement-worker")
    assert recovered is not None
    assert recovered.id == expired_id
    assert recovered.claimed_by == "replacement-worker"
    assert repo_a.finish(expired_id, "original-worker", JobStatus.FAILED) is False
    assert (
        repo_b.finish(expired_id, "replacement-worker", JobStatus.COMPLETED)
        is True
    )


def test_claim_skips_a_locked_first_candidate_without_waiting(
    repository_pair,
) -> None:
    repo_a, repo_b, user_id, account_id = repository_pair
    first_id = _insert_job(
        repo_a,
        user_id,
        account_id,
        "locked-first",
        scheduled="-2 minutes",
    )
    second_id = _insert_job(
        repo_a,
        user_id,
        account_id,
        "available-second",
        scheduled="-1 minute",
    )

    with repo_a.pool.connection() as locking_connection:
        with locking_connection.transaction():
            locking_connection.execute(
                "SELECT id FROM jobs WHERE id = %s FOR UPDATE",
                (first_id,),
            )
            with repo_b.pool.connection() as bounded_connection:
                bounded_connection.execute("SET statement_timeout = '750ms'")

            started = time.monotonic()
            claimed = repo_b.claim_next("worker-b")
            elapsed = time.monotonic() - started

            assert claimed is not None
            assert claimed.id == second_id
            assert elapsed < 0.75


def test_claim_uses_configured_lease_duration(repository_pair) -> None:
    repo_a, _, user_id, account_id = repository_pair
    configured = JobRepository(repo_a.pool, lease_seconds=7)
    _insert_job(configured, user_id, account_id, "configured-lease")

    claimed = configured.claim_next("worker-a")

    assert claimed is not None
    duration = (claimed.lease_expires_at - claimed.heartbeat_at).total_seconds()
    assert duration == pytest.approx(7, abs=0.1)


def test_finish_rejects_non_terminal_status(repository_pair) -> None:
    repo_a, _, user_id, account_id = repository_pair
    _insert_job(repo_a, user_id, account_id, "due")
    claimed = repo_a.claim_next("worker-a")
    assert claimed is not None

    with pytest.raises(ValueError, match="terminal"):
        repo_a.finish(claimed.id, "worker-a", JobStatus.RUNNING)
