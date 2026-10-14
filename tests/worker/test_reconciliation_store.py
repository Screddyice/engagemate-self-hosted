"""Read-only outcome maintenance against the complete migration chain."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from engagemate.worker.reconciliation_store import PostgresReconciliationStore
from psycopg.types.json import Jsonb

from tests.worker.test_canary_scripts import database_url, world  # noqa: F401
from tests.worker.test_follow_store import following, ready, sql  # noqa: F401

NOW = datetime(2026, 9, 23, 20, tzinfo=UTC)


@pytest.fixture
async def setup(ready):  # noqa: F811 - imported pytest fixture
    follow = await following(ready)
    sql(
        ready,
        "UPDATE relationship_follows SET confirmed_follow_at=%s,unfollow_due_at=%s,evidence=%s WHERE id=%s",
        (
            NOW - timedelta(days=5),
            NOW,
            Jsonb(
                {
                    "follow": {"outcome": "confirmed", "followed_by": False},
                    "unfollow": {"note": "preserve"},
                }
            ),
            follow.id,
        ),
    )
    ready["relationship_id"] = follow.id
    ready["reader_store"] = PostgresReconciliationStore(ready["pool"])
    return ready


def observed(claim, followed=True, at=NOW):
    return {
        "owner_id": claim.owner_id,
        "identity_matches": True,
        "followed_by": followed,
        "observed_at": at.isoformat(),
    }


async def test_claim_record_positive_retains_receipts_and_stops_polling(setup):
    store = setup["reader_store"]
    claim = await store.claim_next("w", NOW)
    assert claim
    assert await store.permitted(claim, NOW)
    assert await store.claim_next("other", NOW) is None
    assert await store.finish(claim, NOW, observed(claim))
    assert await store.claim_next("w", NOW + timedelta(days=1)) is None
    evidence = sql(
        setup,
        "SELECT evidence FROM relationship_follows WHERE id=%s",
        (claim.relationship_id,),
    )[0][0]
    assert evidence["unfollow"] == {"note": "preserve"}
    assert evidence["follow"]["outcome"] == "confirmed"
    assert evidence["first_follow_back"]["followed_by"] is True
    assert sql(setup, "SELECT count(*) FROM engagement_usage_events") == [(0,)]
    assert sql(
        setup,
        "SELECT status FROM relationship_follows WHERE id=%s",
        (claim.relationship_id,),
    ) == [("following",)]


async def test_negative_refreshes_after_18h_unknown_preserves_last_snapshot(setup):
    store = setup["reader_store"]
    claim = await store.claim_next("w", NOW)
    assert await store.finish(claim, NOW, observed(claim, False))
    assert await store.claim_next("w", NOW + timedelta(hours=17)) is None
    second = await store.claim_next("w", NOW + timedelta(hours=18))
    assert second
    assert await store.finish(
        second, NOW + timedelta(hours=18), reason="provider secret"
    )
    evidence = sql(
        setup,
        "SELECT evidence FROM relationship_follows WHERE id=%s",
        (claim.relationship_id,),
    )[0][0]
    assert evidence["reconciliation"]["observed_at"] == NOW.isoformat()
    assert sql(
        setup,
        "SELECT failure_code FROM relationship_reconciliation_attempts WHERE id=%s",
        (second.id,),
    ) == [("unknown",)]


@pytest.mark.parametrize(
    "patch",
    [
        {"followed_by": "true"},
        {"owner_id": "999"},
        {"identity_matches": False},
        {"observed_at": "bad"},
        {"observed_at": (NOW + timedelta(seconds=1)).isoformat()},
    ],
)
async def test_unverified_evidence_never_overwrites(setup, patch):
    store = setup["reader_store"]
    claim = await store.claim_next("w", NOW)
    assert await store.finish(claim, NOW, {**observed(claim), **patch})
    evidence = sql(
        setup,
        "SELECT evidence FROM relationship_follows WHERE id=%s",
        (claim.relationship_id,),
    )[0][0]
    assert "reconciliation" not in evidence


async def test_expired_and_wrong_scope_claim_cannot_write(setup):
    store = setup["reader_store"]
    claim = await store.claim_next("w", NOW)
    for changed in (
        replace(claim, user_id=setup["other_user_id"]),
        replace(claim, ig_account_id=setup["other_account_id"]),
        replace(claim, owner_id="789"),
    ):
        assert not await store.permitted(changed, NOW)
        assert not await store.finish(changed, NOW, observed(changed))
    assert not await store.finish(claim, NOW + timedelta(seconds=121), observed(claim))
    assert await store.claim_next("w", NOW + timedelta(hours=1)) is None
    assert await store.claim_next("w", NOW + timedelta(hours=18))


async def test_daily_30_attempt_cap_and_utc_reset(setup):
    for _ in range(30):
        sql(
            setup,
            """INSERT INTO relationship_reconciliation_attempts
 (user_id,ig_account_id,relationship_id,worker_id,reserved_at,lease_expires_at,finished_at,outcome)
 VALUES (%s,%s,%s,'old',%s,%s,%s,'unknown')""",
            (
                setup["user_id"],
                setup["account_id"],
                setup["relationship_id"],
                NOW.replace(hour=0),
                NOW.replace(hour=0) + timedelta(minutes=2),
                NOW.replace(hour=0),
            ),
        )
    assert await setup["reader_store"].claim_next("w", NOW) is None
    assert await setup["reader_store"].claim_next("w", NOW + timedelta(hours=4))


@pytest.mark.parametrize(
    "query",
    [
        "UPDATE ig_accounts SET paused_at=now()",
        "UPDATE ig_accounts SET health_status='degraded'",
        "UPDATE agent_configs SET enabled=false",
        "UPDATE jobs SET status='running',lease_expires_at='2026-10-01T00:00:00Z'",
        "UPDATE proxies SET health_status='degraded'",
        "UPDATE targets SET ig_user_id='999'",
    ],
)
async def test_policy_rechecked_before_read(setup, query):
    claim = await setup["reader_store"].claim_next("w", NOW)
    sql(setup, query)
    assert not await setup["reader_store"].permitted(claim, NOW)
    assert (
        await setup["reader_store"].claim_next("w", NOW + timedelta(hours=18)) is None
    )


@pytest.mark.parametrize("age", [timedelta(hours=71), timedelta(days=31)])
async def test_only_mature_in_window_follows_are_checked(setup, age):
    at = NOW - age
    sql(
        setup,
        "UPDATE relationship_follows SET confirmed_follow_at=%s,unfollow_due_at=%s",
        (at, at + timedelta(hours=120)),
    )
    assert await setup["reader_store"].claim_next("w", NOW) is None


async def test_cached_historical_positive_needs_no_new_browser_observation(setup):
    evidence = {
        "follow": {"outcome": "confirmed", "followed_by": False},
        "first_follow_back": {
            "owner_id": "123456",
            "identity_matches": True,
            "followed_by": True,
            "observed_at": (NOW - timedelta(days=2)).isoformat(),
        },
    }
    sql(setup, "UPDATE relationship_follows SET evidence=%s", (Jsonb(evidence),))
    claim = await setup["reader_store"].claim_next("w", NOW)
    assert claim.cached_observation == {
        **evidence["first_follow_back"],
        "source": "profile_reconciliation",
    }
    assert await setup["reader_store"].finish(claim, NOW, claim.cached_observation)
    assert await setup["reader_store"].claim_next("w", NOW + timedelta(days=1)) is None


@pytest.mark.parametrize("scope", ["system", "action", "tenant", "account"])
async def test_kill_switches_deny_claim_and_read(setup, scope):
    claim = await setup["reader_store"].claim_next("w", NOW)
    sql(
        setup,
        "INSERT INTO instagram_execution_controls (scope,user_id,ig_account_id,action,reason_code) VALUES (%s,%s,%s,%s,'test')",
        (
            scope,
            setup["user_id"] if scope in {"tenant", "account"} else None,
            setup["account_id"] if scope == "account" else None,
            "like" if scope == "action" else None,
        ),
    )
    assert not await setup["reader_store"].permitted(claim, NOW)
    assert (
        await setup["reader_store"].claim_next("w", NOW + timedelta(hours=18)) is None
    )


async def test_other_tenant_kill_switch_does_not_block(setup):
    sql(
        setup,
        "INSERT INTO instagram_execution_controls (scope,user_id,reason_code) VALUES ('tenant',%s,'test')",
        (setup["other_user_id"],),
    )
    assert await setup["reader_store"].claim_next("w", NOW)


async def test_concurrent_workers_reserve_only_once(setup):
    import asyncio

    results = await asyncio.gather(
        *(setup["reader_store"].claim_next(f"w{i}", NOW) for i in range(4))
    )
    assert sum(result is not None for result in results) == 1
    assert sql(setup, "SELECT count(*) FROM relationship_reconciliation_attempts") == [
        (1,)
    ]


async def test_cross_account_attempt_is_rejected_by_database(setup):
    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sql(
            setup,
            "INSERT INTO relationship_reconciliation_attempts (user_id,ig_account_id,relationship_id,worker_id,reserved_at,lease_expires_at) VALUES (%s,%s,%s,'w',%s,%s)",
            (
                setup["other_user_id"],
                setup["other_account_id"],
                setup["relationship_id"],
                NOW,
                NOW + timedelta(minutes=2),
            ),
        )


async def test_fresh_manual_negative_seeds_original_next_check_time(setup):
    at = NOW - timedelta(hours=2)
    evidence = {
        "follow": {"outcome": "confirmed", "followed_by": False},
        "reconciliation": {
            "owner_id": "123456",
            "identity_matches": True,
            "followed_by": False,
            "observed_at": at.isoformat(),
        },
    }
    sql(setup, "UPDATE relationship_follows SET evidence=%s", (Jsonb(evidence),))
    store = setup["reader_store"]
    claim = await store.claim_next("w", NOW)
    assert claim.cached_observation["followed_by"] is False
    assert await store.finish(claim, NOW, claim.cached_observation)
    assert await store.claim_next("w", NOW + timedelta(hours=15)) is None
    due = await store.claim_next("w", NOW + timedelta(hours=16))
    assert due and due.cached_observation is None


async def test_active_read_claim_blocks_follow_claim_without_consuming_action_budget(
    setup,
):
    claim = await setup["reader_store"].claim_next("read", NOW)
    assert await setup["store"].claim_next("follow", NOW) is None
    assert sql(setup, "SELECT unfollow_reserved_at FROM relationship_follows") == [
        (None,)
    ]
    assert await setup["reader_store"].finish(claim, NOW, reason="unknown")
    assert await setup["store"].claim_next("follow", NOW)


async def test_active_read_claim_blocks_serialized_story_claim_until_released(setup):
    from engagemate.db.jobs import JobRepository

    now = sql(setup, "SELECT now()")[0][0]
    sql(setup, "UPDATE relationship_follows SET confirmed_follow_at=%s,unfollow_due_at=%s",
        (now - timedelta(days=5), now))
    claim = await setup["reader_store"].claim_next("read", now)
    assert claim is not None
    sql(
        setup,
        "UPDATE jobs SET status='queued',scheduled_for=%s",
        (now - timedelta(minutes=1),),
    )
    jobs = JobRepository(setup["pool"], serialize_account_actions=True)
    assert jobs.claim_next("story") is None
    assert await setup["reader_store"].finish(claim, now, reason="unknown")
    assert jobs.claim_next("story")
