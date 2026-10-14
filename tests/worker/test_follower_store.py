"""Exercise follower persistence against a disposable PostgreSQL database."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from engagemate.db.pool import create_pool
from engagemate.worker.follower_store import PostgresFollowerStore
from engagemate.worker.story_types import DiscoveryContext

NOW = datetime(2026, 9, 13, tzinfo=UTC)
SCHEMA = """
CREATE TABLE hermes_execution_runs (id uuid PRIMARY KEY DEFAULT gen_random_uuid());
CREATE TABLE ig_accounts (id uuid PRIMARY KEY, user_id uuid NOT NULL, UNIQUE(id, user_id));
CREATE TABLE brand_profiles (
 id uuid PRIMARY KEY, user_id uuid NOT NULL, ig_account_id uuid NOT NULL,
 status text NOT NULL DEFAULT 'complete',
 FOREIGN KEY(ig_account_id,user_id) REFERENCES ig_accounts(id,user_id) ON DELETE CASCADE
);
CREATE TABLE brand_reference_accounts (
 id uuid PRIMARY KEY, brand_profile_id uuid NOT NULL REFERENCES brand_profiles(id) ON DELETE CASCADE
);
CREATE TABLE targets (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL, ig_account_id uuid NOT NULL,
 username text NOT NULL, ig_user_id text, tier text NOT NULL, source text NOT NULL,
 enabled boolean NOT NULL DEFAULT true, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(),
 UNIQUE(ig_account_id, username),
 FOREIGN KEY(ig_account_id,user_id) REFERENCES ig_accounts(id,user_id) ON DELETE CASCADE
);
"""


@pytest.fixture(scope="module")
def database_url():
    if (
        not shutil.which("pg_isready")
        or subprocess.run(["pg_isready"], capture_output=True, check=False).returncode
    ):
        pytest.skip("local PostgreSQL is unavailable")
    database = f"engagemate_followers_{os.getpid()}_{uuid4().hex[:8]}"
    subprocess.run(
        ["createdb", "--template=template0", "--encoding=UTF8", database],
        check=True,
        capture_output=True,
    )
    url = f"postgresql://localhost/{database}"
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(SCHEMA)
            migration = (
                Path(__file__).resolve().parents[2]
                / "app/src/db/migrations/0018_follower_audience.sql"
            )
            connection.execute(migration.read_text())
        yield url
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", database],
            check=True,
            capture_output=True,
        )


@pytest.fixture
def world(database_url):
    pool = create_pool(database_url, min_size=1, max_size=4)
    context = DiscoveryContext(uuid4(), uuid4(), NOW)
    other = DiscoveryContext(uuid4(), uuid4(), NOW)
    profile, other_profile, source, source2, other_source = (uuid4() for _ in range(5))
    with pool.connection() as connection:
        for ctx, profile_id in [(context, profile), (other, other_profile)]:
            connection.execute(
                "INSERT INTO ig_accounts VALUES (%s,%s)",
                (ctx.ig_account_id, ctx.user_id),
            )
            connection.execute(
                "INSERT INTO brand_profiles (id,user_id,ig_account_id,follower_audience_approved_at) VALUES (%s,%s,%s,%s)",
                (profile_id, ctx.user_id, ctx.ig_account_id, NOW),
            )
        for source_id, profile_id in [
            (source, profile),
            (source2, profile),
            (other_source, other_profile),
        ]:
            connection.execute(
                "INSERT INTO brand_reference_accounts VALUES (%s,%s)",
                (source_id, profile_id),
            )
    yield (
        pool,
        PostgresFollowerStore(pool),
        context,
        other,
        profile,
        source,
        source2,
        other_source,
    )
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM ig_accounts WHERE id = ANY(%s)",
            ([context.ig_account_id, other.ig_account_id],),
        )
    pool.close()


async def test_page_cursor_cas_and_provenance_survive_repository_restart(world):
    pool, store, ctx, _, _, source, source2, _ = world
    assert await store.source_state(ctx, source) == {
        "cursor": None,
        "exhausted": False,
        "initialized": False,
    }
    assert await store.save_page(
        ctx, source, None, "page2", False, [{"owner_id": "123", "handle": "Alpha"}]
    )
    assert not await store.save_page(
        ctx, source, None, "stale", False, [{"owner_id": "456", "handle": "stale"}]
    )
    restarted = PostgresFollowerStore(pool)
    assert await restarted.source_state(ctx, source) == {
        "cursor": "page2",
        "exhausted": False,
        "initialized": True,
    }
    assert await restarted.next_candidate(ctx, [source], NOW) == {
        "owner_id": "123",
        "handle": "alpha",
    }
    assert await restarted.next_candidate(ctx, [source2], NOW) is None
    assert await restarted.save_page(
        ctx, source2, None, None, True, [{"owner_id": "123", "handle": "alpha"}]
    )
    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM follower_candidates WHERE user_id=%s",
                (ctx.user_id,),
            ).fetchone()["count"]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM follower_candidate_sources WHERE user_id=%s",
                (ctx.user_id,),
            ).fetchone()["count"]
            == 2
        )


async def test_denies_cross_tenant_unapproved_and_removed_sources(world):
    pool, store, ctx, other, profile, source, _, other_source = world
    assert await store.source_state(ctx, other_source) == {
        "cursor": None,
        "exhausted": True,
        "initialized": False,
    }
    assert not await store.save_page(ctx, other_source, None, None, True, [])
    assert await store.save_page(
        ctx, source, None, "next", False, [{"owner_id": "123", "handle": "alpha"}]
    )
    assert await store.next_candidate(other, [source], NOW) is None
    assert await store.admit_target(other, [source], "123", "alpha", NOW) is None
    with pool.connection() as connection:
        connection.execute(
            "UPDATE brand_profiles SET follower_audience_approved_at=NULL WHERE id=%s",
            (profile,),
        )
    assert not await store.save_page(ctx, source, "next", None, True, [])
    assert await store.next_candidate(ctx, [source], NOW) is None
    assert await store.admit_target(ctx, [source], "123", "alpha", NOW) is None
    with pool.connection() as connection:
        connection.execute(
            "UPDATE brand_profiles SET follower_audience_approved_at=%s WHERE id=%s",
            (NOW, profile),
        )
        connection.execute(
            "DELETE FROM brand_reference_accounts WHERE id=%s", (source,)
        )
    assert await store.next_candidate(ctx, [source], NOW) is None
    assert await store.admit_target(ctx, [source], "123", "alpha", NOW) is None


async def test_new_first_and_no_story_and_exclusion_cooldowns(world):
    _, store, ctx, _, _, source, _, _ = world
    await store.save_page(
        ctx, source, None, "two", False, [{"owner_id": "123", "handle": "alpha"}]
    )
    await store.mark_checked(ctx, "123", "no_story", NOW)
    assert await store.next_candidate(ctx, [source], NOW) is None
    await store.save_page(
        ctx, source, "two", None, True, [{"owner_id": "456", "handle": "beta"}]
    )
    assert (await store.next_candidate(ctx, [source], NOW + timedelta(hours=2)))[
        "owner_id"
    ] == "456"
    await store.mark_checked(ctx, "456", "excluded", NOW + timedelta(hours=2))
    assert (await store.next_candidate(ctx, [source], NOW + timedelta(hours=2)))[
        "owner_id"
    ] == "123"
    await store.mark_checked(ctx, "123", "unknown", NOW + timedelta(hours=2))
    assert await store.next_candidate(ctx, [source], NOW + timedelta(hours=2)) is None
    assert (await store.next_candidate(ctx, [source], NOW + timedelta(hours=26)))[
        "owner_id"
    ] == "123"


async def test_admission_requires_provenance_and_respects_disabled_and_collision(world):
    pool, store, ctx, _, _, source, source2, _ = world
    await store.save_page(
        ctx, source, None, None, True, [{"owner_id": "123", "handle": "alpha"}]
    )
    assert await store.admit_target(ctx, [source2], "123", "alpha", NOW) is None
    target = await store.admit_target(ctx, [source], "123", "alpha", NOW)
    assert target is not None and target.enabled and target.tier == "discovery"
    assert (
        await store.admit_target(ctx, [source], "123", "alpha", NOW)
    ).id == target.id
    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT source FROM targets WHERE id=%s", (target.id,)
            ).fetchone()["source"]
            == "source-followers"
        )
        connection.execute("UPDATE targets SET enabled=false WHERE id=%s", (target.id,))
    assert await store.admit_target(ctx, [source], "123", "renamed", NOW) is None
    await store.save_page(
        ctx, source2, None, None, True, [{"owner_id": "456", "handle": "alpha"}]
    )
    assert await store.admit_target(ctx, [source2], "456", "alpha", NOW) is None
    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM targets WHERE user_id=%s", (ctx.user_id,)
            ).fetchone()["count"]
            == 1
        )


async def test_exhaustion_refreshes_after_one_day(world):
    _, store, ctx, _, _, source, _, _ = world
    await store.save_page(ctx, source, None, None, True, [])
    assert (await store.source_state(ctx, source))["exhausted"]
    later = DiscoveryContext(ctx.user_id, ctx.ig_account_id, NOW + timedelta(days=1))
    assert await store.source_state(later, source) == {
        "cursor": None,
        "exhausted": False,
        "initialized": True,
    }
    assert await store.save_page(later, source, None, "next", False, [])


async def test_invalid_page_rolls_back_without_advancing_cursor(world):
    _, store, ctx, _, _, source, _, _ = world
    await store.source_state(ctx, source)
    with pytest.raises(ValueError):
        await store.save_page(
            ctx,
            source,
            None,
            "next",
            False,
            [
                {"owner_id": "123", "handle": "alpha"},
                {"owner_id": "invalid", "handle": "beta"},
            ],
        )
    assert await store.source_state(ctx, source) == {
        "cursor": None,
        "exhausted": False,
        "initialized": True,
    }
    assert await store.next_candidate(ctx, [source], NOW) is None


async def test_concurrent_page_writers_commit_only_one_page(world):
    import asyncio

    pool, store, ctx, _, _, source, _, _ = world
    await store.source_state(ctx, source)
    outcomes = await asyncio.gather(
        store.save_page(
            ctx, source, None, "a", False, [{"owner_id": "123", "handle": "alpha"}]
        ),
        store.save_page(
            ctx, source, None, "b", False, [{"owner_id": "456", "handle": "beta"}]
        ),
    )
    assert sorted(outcomes) == [False, True]
    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM follower_candidates WHERE user_id=%s",
                (ctx.user_id,),
            ).fetchone()["count"]
            == 1
        )


async def test_database_failure_rolls_back_page_and_cursor(world):
    pool, store, ctx, _, _, source, _, _ = world
    await store.source_state(ctx, source)
    with pool.connection() as connection:
        connection.execute(
            "ALTER TABLE follower_candidates ADD CONSTRAINT test_reject_beta CHECK (handle <> 'reject_beta')"
        )
    try:
        with pytest.raises(psycopg.errors.CheckViolation):
            await store.save_page(
                ctx,
                source,
                None,
                "next",
                False,
                [
                    {"owner_id": "123", "handle": "alpha"},
                    {"owner_id": "456", "handle": "reject_beta"},
                ],
            )
        assert await store.source_state(ctx, source) == {
            "cursor": None,
            "exhausted": False,
            "initialized": True,
        }
        assert await store.next_candidate(ctx, [source], NOW) is None
    finally:
        with pool.connection() as connection:
            connection.execute(
                "ALTER TABLE follower_candidates DROP CONSTRAINT test_reject_beta"
            )


def test_migration_initializes_report_and_rejects_non_object(database_url):
    with psycopg.connect(database_url) as connection:
        row = connection.execute(
            "INSERT INTO hermes_execution_runs DEFAULT VALUES RETURNING id, discovery_report"
        ).fetchone()
        assert row[1] == {}
        with pytest.raises(psycopg.errors.CheckViolation), connection.transaction():
            connection.execute(
                "UPDATE hermes_execution_runs SET discovery_report='[]'::jsonb WHERE id=%s",
                (row[0],),
            )


async def test_admission_checks_ambiguous_and_unbound_existing_handles(world):
    pool, store, ctx, _, _, source, _, _ = world
    await store.save_page(
        ctx, source, None, None, True, [{"owner_id": "123", "handle": "alpha"}]
    )
    with pool.connection() as connection:
        connection.execute(
            "INSERT INTO targets (user_id,ig_account_id,username,ig_user_id,tier,source) VALUES (%s,%s,'alpha',NULL,'broad','manual')",
            (ctx.user_id, ctx.ig_account_id),
        )
    assert await store.admit_target(ctx, [source], "123", "alpha", NOW) is None
    with pool.connection() as connection:
        connection.execute(
            "UPDATE targets SET ig_user_id='123' WHERE ig_account_id=%s",
            (ctx.ig_account_id,),
        )
        connection.execute(
            "INSERT INTO targets (user_id,ig_account_id,username,ig_user_id,tier,source) VALUES (%s,%s,'old_alias','123','broad','manual')",
            (ctx.user_id, ctx.ig_account_id),
        )
    assert await store.admit_target(ctx, [source], "123", "alpha", NOW) is None
