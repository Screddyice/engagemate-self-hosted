"""Exact operator claims against the complete production migration chain."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest

from engagemate.db.jobs import JobRepository
from engagemate.db.text import text
from engagemate.domain.models import JobStatus
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401


def sql(world, statement, params=()):  # noqa: F811
    with world["pool"].connection() as connection:
        result = connection.execute(statement, params)
        return result.fetchall() if result.description else None


def add_job(world, *, other_account=False):  # noqa: F811
    job_id = uuid4()
    prefix = "other_" if other_account else ""
    sql(world, """INSERT INTO jobs
        (id,user_id,ig_account_id,schedule_key,scheduled_for)
        VALUES (%s,%s,%s,%s,now()-interval '1 minute')""",
        (job_id, world[prefix + "user_id"], world[prefix + "account_id"], str(job_id)))
    return job_id


@pytest.fixture
def ready(world):  # noqa: F811
    sql(world, "UPDATE jobs SET status='completed' WHERE id=%s", (world["job_id"],))
    world["exact_job_id"] = add_job(world)
    world["repository"] = JobRepository(world["pool"], lease_seconds=23)
    return world


def claim(ready, *, job_id=None, worker_id="exact-worker"):
    return ready["repository"].claim_exact(
        job_id or ready["exact_job_id"], ready["user_id"], ready["account_id"], worker_id
    )


def test_exact_claim_leaves_other_queued_job_untouched(ready):
    other = add_job(ready)
    value = claim(ready)
    assert value.id == ready["exact_job_id"]
    assert value.status is JobStatus.RUNNING and value.attempts == 1
    assert value.claimed_by == "exact-worker"
    assert (value.lease_expires_at-value.heartbeat_at).total_seconds() == 23
    row = sql(ready, "SELECT status,attempts,claimed_by FROM jobs WHERE id=%s", (other,))[0]
    assert (text(row["status"]), row["attempts"], row["claimed_by"]) == ("queued", 0, None)
    assert claim(ready) is None


def test_serialized_worker_skips_operator_canary_jobs(ready):
    sql(ready, "UPDATE jobs SET status='completed' WHERE id=%s", (ready["exact_job_id"],))
    operator = add_job(ready)
    sql(ready, "UPDATE jobs SET schedule_key='operator-reply-live' WHERE id=%s", (operator,))
    campaign = add_job(ready)
    repository = JobRepository(ready["pool"], serialize_account_actions=True)

    claimed = repository.claim_next("campaign-worker")

    assert claimed is not None and claimed.id == campaign
    row = sql(ready, "SELECT status,attempts,claimed_by FROM jobs WHERE id=%s", (operator,))[0]
    assert (text(row["status"]), row["attempts"], row["claimed_by"]) == ("queued", 0, None)


@pytest.mark.parametrize("field", ["job_id", "user_id", "account_id"])
def test_exact_claim_refuses_scope_mismatch(ready, field):
    scope = {"job_id": ready["exact_job_id"], "user_id": ready["user_id"],
             "account_id": ready["account_id"], "worker_id": "exact-worker"}
    scope[field] = uuid4() if field == "job_id" else ready["other_" + field]
    assert ready["repository"].claim_exact(**scope) is None
    assert sql(ready, "SELECT attempts FROM jobs WHERE id=%s", (ready["exact_job_id"],))[0]["attempts"] == 0


@pytest.mark.parametrize("condition", [
    "status='running',claimed_by='old',lease_expires_at=now()-interval '1 minute'",
    "status='running',claimed_by='old',lease_expires_at=now()+interval '1 minute'",
    "status='completed'", "status='failed'", "status='cancelled'", "attempts=1",
    "scheduled_for=now()+interval '1 hour'", "claimed_by='old'",
    "lease_expires_at=now()-interval '1 minute'",
])
def test_exact_claim_refuses_attempted_running_or_not_due_job(ready, condition):
    sql(ready, "UPDATE jobs SET " + condition + " WHERE id=%s", (ready["exact_job_id"],))
    before = sql(ready, "SELECT * FROM jobs WHERE id=%s", (ready["exact_job_id"],))[0]
    assert claim(ready) is None
    assert sql(ready, "SELECT * FROM jobs WHERE id=%s", (ready["exact_job_id"],))[0] == before


@pytest.mark.parametrize("health,paused", [
    ("paused", False), ("degraded", False), ("challenged", False),
    ("banned", False), ("active", True),
])
def test_exact_claim_refuses_unhealthy_or_paused_account(ready, health, paused):
    sql(ready, "UPDATE ig_accounts SET health_status=%s,paused_at=CASE WHEN %s THEN now() END WHERE id=%s",
        (health, paused, ready["account_id"]))
    assert claim(ready) is None


def test_exact_claim_refuses_disabled_account(ready):
    sql(ready, "UPDATE agent_configs SET enabled=false WHERE ig_account_id=%s", (ready["account_id"],))
    assert claim(ready) is None


@pytest.mark.parametrize("field", ["job_id", "user_id", "account_id"])
@pytest.mark.parametrize("value", [None, "00000000-0000-0000-0000-000000000000", 1])
def test_exact_claim_validates_uuid_scope_before_database(field, value):
    params = {"job_id": uuid4(), "user_id": uuid4(), "account_id": uuid4(), "worker_id": "w"}
    params[field] = value
    with pytest.raises(ValueError, match="UUID identifiers"):
        JobRepository(None).claim_exact(**params)


@pytest.mark.parametrize("worker", [None, True, 1, "", "has space", "newline\n", "x"*129])
def test_exact_claim_validates_worker_before_database(worker):
    with pytest.raises(ValueError, match="worker id"):
        JobRepository(None).claim_exact(uuid4(), uuid4(), uuid4(), worker)


def test_concurrent_exact_claims_have_one_owner(ready):
    barrier = Barrier(2)

    def attempt(worker):
        barrier.wait()
        return claim(ready, worker_id=worker)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ["first", "second"]))
    assert sum(value is not None for value in results) == 1
    assert sql(ready, "SELECT attempts FROM jobs WHERE id=%s", (ready["exact_job_id"],))[0]["attempts"] == 1


def test_concurrent_exact_jobs_serialize_same_account(ready):
    second_job = add_job(ready)
    barrier = Barrier(2)

    def attempt(job):
        barrier.wait()
        return claim(ready, job_id=job)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, [ready["exact_job_id"], second_job]))
    assert sum(value is not None for value in results) == 1
    rows = sql(ready, "SELECT status,attempts FROM jobs WHERE id=ANY(%s)",
               ([ready["exact_job_id"], second_job],))
    assert sorted((text(row["status"]), row["attempts"]) for row in rows) == [("queued", 0), ("running", 1)]


def add_relationship(ready, status="follow_pending"):
    engagement, relationship = uuid4(), uuid4()
    sql(ready, """INSERT INTO story_engagements
        (id,user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at)
        VALUES (%s,%s,%s,%s,%s,'like','sent',now())""",
        (engagement, ready["user_id"], ready["account_id"], ready["target_id"], "a"*64))
    sql(ready, """INSERT INTO relationship_follows
        (id,user_id,ig_account_id,target_id,owner_id,prerequisite_engagement_id,status,
         claimed_by,lease_token,lease_expires_at,confirmed_follow_at,unfollow_due_at)
        VALUES (%s,%s,%s,%s,'12345',%s,%s,
          CASE WHEN %s THEN 'lane' END, CASE WHEN %s THEN %s::uuid END,
          CASE WHEN %s THEN now()+interval '1 minute' END,
          CASE WHEN %s THEN now()-interval '1 day' END,
          CASE WHEN %s THEN now()+interval '4 days' END)""",
        (relationship, ready["user_id"], ready["account_id"], ready["target_id"], engagement,
         status, status.endswith("running"), status.endswith("running"), uuid4(),
         status.endswith("running"), status == "unfollow_running", status == "unfollow_running"))
    return relationship


@pytest.mark.parametrize("lane", ["job", "follow", "unfollow", "reel", "reconciliation"])
def test_exact_claim_excludes_active_account_lanes(ready, lane):
    if lane == "job":
        other = add_job(ready)
        sql(ready, "UPDATE jobs SET status='running',lease_expires_at=now()+interval '1 minute' WHERE id=%s", (other,))
    else:
        relationship = add_relationship(ready, lane+"_running" if lane in {"follow", "unfollow"} else "follow_pending")
        if lane == "reel":
            sql(ready, """INSERT INTO follow_reel_messages
                (relationship_follow_id,user_id,ig_account_id,target_id,recipient_id,
                 status,claimed_by,lease_token,lease_expires_at)
                VALUES (%s,%s,%s,%s,'12345','running','lane',%s,now()+interval '1 minute')""",
                (relationship, ready["user_id"], ready["account_id"], ready["target_id"], uuid4()))
        elif lane == "reconciliation":
            sql(ready, """INSERT INTO relationship_reconciliation_attempts
                (user_id,ig_account_id,relationship_id,worker_id,reserved_at,lease_expires_at)
                VALUES (%s,%s,%s,'lane',now(),now()+interval '1 minute')""",
                (ready["user_id"], ready["account_id"], relationship))
    assert claim(ready) is None


def test_exact_claim_ignores_other_account_active_job(ready):
    foreign = add_job(ready, other_account=True)
    sql(ready, "UPDATE jobs SET status='running',lease_expires_at=now()+interval '1 minute' WHERE id=%s", (foreign,))
    assert claim(ready) is not None


def test_exact_claim_rechecks_lane_after_account_lock(ready):
    other = add_job(ready)
    original_connection = ready["pool"].connection

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def transaction(self):
            return self.connection.transaction()

        def execute(self, statement, params=None):
            result = self.connection.execute(statement, params)
            if "SELECT a.id FROM ig_accounts a" in statement:
                # A writer that committed after the lock statement's snapshot
                # must still exclude this job in the following statement.
                with original_connection() as writer:
                    writer.execute("UPDATE jobs SET status='running',lease_expires_at=now()+interval '1 minute' WHERE id=%s", (other,))
            return result

    @contextmanager
    def observed_connection():
        with original_connection() as connection:
            yield ObservedConnection(connection)

    repository = JobRepository(SimpleNamespace(connection=observed_connection))
    assert repository.claim_exact(ready["exact_job_id"], ready["user_id"], ready["account_id"], "worker") is None
    assert sql(ready, "SELECT attempts FROM jobs WHERE id=%s", (ready["exact_job_id"],))[0]["attempts"] == 0
