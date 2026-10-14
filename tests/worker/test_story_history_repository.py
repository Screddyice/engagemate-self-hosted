"""One batch, tenant-scoped dedupe query behind StoryHistoryRepository."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest

ROOT_DIR = Path(__file__).resolve().parents[2]
API_DIR = ROOT_DIR / "api"
sys.path.insert(0, str(API_DIR))

from engagemate.db.pool import create_pool
from engagemate.worker.story_history import PostgresStoryHistoryRepository
from engagemate.worker.story_types import DiscoveryHistory, TargetEngagement

SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY
);

CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    UNIQUE (id, user_id)
);

CREATE TABLE targets (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    UNIQUE (id, ig_account_id, user_id)
);

CREATE TABLE story_engagements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    target_id uuid NOT NULL,
    story_fingerprint text NOT NULL,
    action text NOT NULL,
    generated_text text,
    model_used text,
    rationale text,
    confidence numeric(5, 4),
    status text NOT NULL,
    failure_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    executed_at timestamptz
);

CREATE INDEX story_engagements_dedupe_idx
    ON story_engagements (user_id, ig_account_id, story_fingerprint);
"""

NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)


def fingerprint(seed: str) -> str:
    return (seed * 64)[:64]


FP_A = fingerprint("a")
FP_B = fingerprint("b")
FP_C = fingerprint("c")


@pytest.fixture(scope="module")
def database_url():
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode != 0:
        pytest.skip("local PostgreSQL is unavailable")

    database = f"engagemate_history_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    created = subprocess.run(
        ["createdb", database], capture_output=True, text=True, check=False
    )
    if created.returncode != 0:
        pytest.fail(f"could not create disposable database: {created.stderr}")

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
def tenant(database_url):
    pool = create_pool(database_url, min_size=1, max_size=2)
    try:
        user_id = uuid.uuid4()
        account_id = uuid.uuid4()
        other_user_id = uuid.uuid4()
        other_account_id = uuid.uuid4()
        target_a = uuid.uuid4()
        target_b = uuid.uuid4()
        with pool.connection() as connection:
            connection.execute(
                "TRUNCATE story_engagements, targets, ig_accounts, users CASCADE"
            )
            for identifier in (user_id, other_user_id):
                connection.execute("INSERT INTO users (id) VALUES (%s)", (identifier,))
            connection.execute(
                "INSERT INTO ig_accounts (id, user_id) VALUES (%s, %s)",
                (account_id, user_id),
            )
            connection.execute(
                "INSERT INTO ig_accounts (id, user_id) VALUES (%s, %s)",
                (other_account_id, other_user_id),
            )
            for target_id in (target_a, target_b):
                connection.execute(
                    "INSERT INTO targets (id, user_id, ig_account_id) "
                    "VALUES (%s, %s, %s)",
                    (target_id, user_id, account_id),
                )
        yield {
            "pool": pool,
            "repository": PostgresStoryHistoryRepository(pool),
            "user_id": user_id,
            "account_id": account_id,
            "other_user_id": other_user_id,
            "other_account_id": other_account_id,
            "target_a": target_a,
            "target_b": target_b,
        }
    finally:
        pool.close()


def insert_engagement(
    pool,
    *,
    user_id,
    account_id,
    target_id,
    story_fingerprint,
    status="sent",
    created_at=NOW,
    action="like",
):
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO story_engagements "
            "(user_id, ig_account_id, target_id, story_fingerprint, action, "
            " status, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                user_id,
                account_id,
                target_id,
                story_fingerprint,
                action,
                status,
                created_at,
            ),
        )


async def test_lookup_returns_seen_fingerprints_for_the_tenant(tenant):
    insert_engagement(
        tenant["pool"],
        user_id=tenant["user_id"],
        account_id=tenant["account_id"],
        target_id=tenant["target_a"],
        story_fingerprint=FP_A,
    )

    history = await tenant["repository"].lookup(
        tenant["user_id"],
        tenant["account_id"],
        frozenset({FP_A, FP_B}),
        frozenset({tenant["target_a"]}),
    )

    assert type(history) is DiscoveryHistory
    assert history.seen_fingerprints == frozenset({FP_A})


async def test_unseen_fingerprints_are_absent(tenant):
    history = await tenant["repository"].lookup(
        tenant["user_id"],
        tenant["account_id"],
        frozenset({FP_A, FP_B}),
        frozenset({tenant["target_a"]}),
    )

    assert history.seen_fingerprints == frozenset()
    assert history.most_recent_engagements == ()


async def test_another_tenants_engagement_is_never_seen(tenant):
    insert_engagement(
        tenant["pool"],
        user_id=tenant["other_user_id"],
        account_id=tenant["other_account_id"],
        target_id=tenant["target_a"],
        story_fingerprint=FP_A,
    )

    history = await tenant["repository"].lookup(
        tenant["user_id"],
        tenant["account_id"],
        frozenset({FP_A}),
        frozenset({tenant["target_a"]}),
    )

    assert history.seen_fingerprints == frozenset()


async def test_a_failed_attempt_still_counts_as_seen(tenant):
    insert_engagement(
        tenant["pool"],
        user_id=tenant["user_id"],
        account_id=tenant["account_id"],
        target_id=tenant["target_a"],
        story_fingerprint=FP_A,
        status="failed",
    )

    history = await tenant["repository"].lookup(
        tenant["user_id"],
        tenant["account_id"],
        frozenset({FP_A}),
        frozenset({tenant["target_a"]}),
    )

    assert history.seen_fingerprints == frozenset({FP_A})


async def test_result_never_includes_unqueried_fingerprints(tenant):
    for value in (FP_A, FP_C):
        insert_engagement(
            tenant["pool"],
            user_id=tenant["user_id"],
            account_id=tenant["account_id"],
            target_id=tenant["target_a"],
            story_fingerprint=value,
        )

    history = await tenant["repository"].lookup(
        tenant["user_id"],
        tenant["account_id"],
        frozenset({FP_A}),
        frozenset({tenant["target_a"]}),
    )

    assert history.seen_fingerprints == frozenset({FP_A})


async def test_most_recent_sent_engagement_is_reported_per_target(tenant):
    earlier = NOW - timedelta(days=3)
    for created_at in (earlier, NOW):
        insert_engagement(
            tenant["pool"],
            user_id=tenant["user_id"],
            account_id=tenant["account_id"],
            target_id=tenant["target_a"],
            story_fingerprint=fingerprint(str(created_at.day)),
            created_at=created_at,
        )

    history = await tenant["repository"].lookup(
        tenant["user_id"],
        tenant["account_id"],
        frozenset(),
        frozenset({tenant["target_a"], tenant["target_b"]}),
    )

    assert len(history.most_recent_engagements) == 1
    engagement = history.most_recent_engagements[0]
    assert type(engagement) is TargetEngagement
    assert engagement.target_id == tenant["target_a"]
    assert engagement.engaged_at == NOW


async def test_only_sent_engagements_count_as_prior_contact(tenant):
    insert_engagement(
        tenant["pool"],
        user_id=tenant["user_id"],
        account_id=tenant["account_id"],
        target_id=tenant["target_a"],
        story_fingerprint=FP_A,
        status="failed",
    )

    history = await tenant["repository"].lookup(
        tenant["user_id"],
        tenant["account_id"],
        frozenset(),
        frozenset({tenant["target_a"]}),
    )

    assert history.most_recent_engagements == ()


async def test_engagements_are_limited_to_queried_targets(tenant):
    insert_engagement(
        tenant["pool"],
        user_id=tenant["user_id"],
        account_id=tenant["account_id"],
        target_id=tenant["target_b"],
        story_fingerprint=FP_B,
    )

    history = await tenant["repository"].lookup(
        tenant["user_id"],
        tenant["account_id"],
        frozenset(),
        frozenset({tenant["target_a"]}),
    )

    assert history.most_recent_engagements == ()


async def test_empty_inputs_short_circuit_to_an_empty_history(tenant):
    history = await tenant["repository"].lookup(
        tenant["user_id"], tenant["account_id"], frozenset(), frozenset()
    )

    assert history == DiscoveryHistory(frozenset(), ())


async def test_discovery_can_consume_the_result_directly(tenant):
    """The result must satisfy DiscoveryService's own history validation."""

    insert_engagement(
        tenant["pool"],
        user_id=tenant["user_id"],
        account_id=tenant["account_id"],
        target_id=tenant["target_a"],
        story_fingerprint=FP_A,
    )
    from engagemate.worker.discovery import _validate_history

    queried = frozenset({FP_A, FP_B})
    target_ids = frozenset({tenant["target_a"], tenant["target_b"]})
    history = await tenant["repository"].lookup(
        tenant["user_id"], tenant["account_id"], queried, target_ids
    )

    validated = _validate_history(
        history, target_ids, queried, NOW + timedelta(minutes=1)
    )

    assert validated == {tenant["target_a"]: NOW}


async def test_oversized_fingerprint_batches_are_rejected(tenant):
    too_many = frozenset(f"{index:064x}" for index in range(1_001))

    with pytest.raises(ValueError):
        await tenant["repository"].lookup(
            tenant["user_id"], tenant["account_id"], too_many, frozenset()
        )


async def test_malformed_fingerprints_are_rejected(tenant):
    with pytest.raises(ValueError):
        await tenant["repository"].lookup(
            tenant["user_id"],
            tenant["account_id"],
            frozenset({"not-a-fingerprint"}),
            frozenset(),
        )


async def test_non_uuid_target_ids_are_rejected(tenant):
    with pytest.raises(ValueError):
        await tenant["repository"].lookup(
            tenant["user_id"],
            tenant["account_id"],
            frozenset(),
            frozenset({"not-a-uuid"}),
        )
