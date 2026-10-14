"""Relationship persistence against the complete migration chain."""

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from engagemate.mcp.control_repository import ControlRepository
from engagemate.worker.follow_store import PostgresFollowStore

from scripts.reconcile_uncertain_follow import record_confirmation
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401

NOW = datetime.now(UTC)


def pin_relationship_clock(monkeypatch, now):
    """Keep imported fixture clock aliases aligned for this test only."""
    modules = tuple(sys.modules.values())
    # Pytest also collects this namespace-package file under its short name.
    source = Path(__file__).resolve()
    previous = [module.NOW for module in modules if module
                and str(getattr(module, "__file__", "")).endswith("/test_follow_store.py")
                and Path(module.__file__).resolve() == source]
    for module in modules:
        if module and any(getattr(module, "NOW", None) is clock for clock in previous):
            monkeypatch.setattr(module, "NOW", now)


def sql(world, query, args=()):  # noqa: F811
    with world["pool"].connection() as conn:
        result = conn.execute(query, args)
        return (
            [
                tuple(v.decode() if isinstance(v, bytes) else v for v in row.values())
                for row in result.fetchall()
            ]
            if result.description
            else None
        )


@pytest.fixture
def ready(world, monkeypatch):  # noqa: F811
    # A real database lease must not start at module import or collection time.
    pin_relationship_clock(monkeypatch, datetime.now(UTC))
    sql(
        world,
        "UPDATE targets SET ig_user_id='123456' WHERE id=%s",
        (world["target_id"],),
    )
    sql(
        world,
        "UPDATE agent_configs SET follow_after_engagement=true,auto_unfollow_enabled=true,follow_enabled_at=%s WHERE ig_account_id=%s",
        (NOW - timedelta(hours=1), world["account_id"]),
    )
    sql(world, "UPDATE jobs SET status='completed' WHERE id=%s", (world["job_id"],))
    sql(
        world,
        "UPDATE hermes_execution_runs SET status='completed',completed_at=%s WHERE id=%s",
        (NOW, world["run_id"]),
    )
    proxy = uuid4()
    sql(
        world,
        "INSERT INTO proxies (id,provider,provider_reference,proxy_type,country,host,port,encrypted_username,encrypted_password) VALUES (%s,'test',%s,'residential','US','proxy.test',1234,'test','test')",
        (proxy, str(proxy)),
    )
    sql(
        world,
        "INSERT INTO proxy_bindings (user_id,ig_account_id,proxy_id) VALUES (%s,%s,%s)",
        (world["user_id"], world["account_id"], proxy),
    )
    engagement = uuid4()
    sql(
        world,
        "INSERT INTO story_engagements (id,user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at) VALUES (%s,%s,%s,%s,%s,'like','sent',%s)",
        (
            engagement,
            world["user_id"],
            world["account_id"],
            world["target_id"],
            "a" * 64,
            NOW,
        ),
    )
    sql(
        world,
        "INSERT INTO hermes_tool_calls (run_id,tool_name,target_id,story_fingerprint,idempotency_key,status) VALUES (%s,'like',%s,%s,%s,'confirmed')",
        (world["run_id"], world["target_id"], "a" * 64, str(uuid4())),
    )
    world["engagement_id"] = engagement
    world["store"] = PostgresFollowStore(world["pool"])
    return world


async def following(w):
    store = w["store"]
    assert await store.enqueue_confirmed(NOW) == 1
    claim = await store.claim_next("worker", NOW)
    assert claim and claim.action == "follow"
    assert await store.mark_attempting(claim, NOW)
    assert await store.finish(claim, "confirmed", NOW, {"confirmed": True})
    return claim


async def test_story_reply_follow_reservation_belongs_to_current_running_job(ready):
    sql(
        ready,
        "UPDATE ig_accounts SET ig_user_id='999999' WHERE id=%s",
        (ready["account_id"],),
    )
    sql(
        ready,
        "UPDATE story_engagements SET action='reply',status='skipped',executed_at=NULL WHERE id=%s",
        (ready["engagement_id"],),
    )
    sql(
        ready,
        """UPDATE jobs SET status='running',claimed_by='story-worker',
           lease_expires_at=%s WHERE id=%s""",
        (NOW + timedelta(hours=1), ready["job_id"]),
    )
    sql(
        ready,
        "UPDATE hermes_execution_runs SET status='running',completed_at=NULL WHERE id=%s",
        (ready["run_id"],),
    )
    sql(
        ready,
        """UPDATE hermes_tool_calls
           SET tool_name='reply',status='started',completed_at=NULL,
               safe_result='{"status":"started","phase":"execution_started"}'::jsonb
           WHERE run_id=%s""",
        (ready["run_id"],),
    )
    claim = await ready["store"].reserve_story_reply_follow(
        user_id=ready["user_id"],
        account_id=ready["account_id"],
        target_id=ready["target_id"],
        owner_id="123456",
        engagement_id=ready["engagement_id"],
        job_id=ready["job_id"],
        worker_id="story-worker",
        now=NOW,
    )

    assert claim is not None and claim.action == "follow"
    assert claim.story_reply_first is True
    assert await ready["store"].mark_attempting(claim, NOW)
    assert sql(
        ready,
        """SELECT status,prerequisite_engagement_id,claimed_by,follow_reserved_at
           FROM relationship_follows WHERE id=%s""",
        (claim.id,),
    ) == [("follow_running", ready["engagement_id"], "story-worker", NOW)]
    assert await ready["store"].finish(claim, "confirmed", NOW, {"receipt_matches": True})
    assert await ready["store"].reply_ready(claim, NOW)
    control = uuid4()
    sql(
        ready,
        """INSERT INTO instagram_execution_controls
           (id,scope,action,blocked,reason_code) VALUES (%s,'action','reply',true,'test')""",
        (control,),
    )
    assert not await ready["store"].reply_ready(claim, NOW)
    assert await ready["store"].reserve_story_reply_follow(
        user_id=ready["user_id"], account_id=ready["account_id"],
        target_id=ready["target_id"], owner_id="123456",
        engagement_id=ready["engagement_id"], job_id=ready["job_id"],
        worker_id="story-worker", now=NOW,
    ) is None



@pytest.mark.parametrize("source", ["target", "engagement"])
async def test_source_cleanup_cannot_erase_follow_or_unfollow_date(ready, source):
    claim = await following(ready)
    query = "SELECT owner_id,confirmed_follow_at,unfollow_due_at FROM relationship_follows WHERE id=%s"
    before = sql(ready, query, (claim.id,))
    table, identifier = (
        ("targets", ready["target_id"])
        if source == "target"
        else ("story_engagements", ready["engagement_id"])
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sql(ready, f"DELETE FROM {table} WHERE id=%s", (identifier,))
    assert sql(ready, query, (claim.id,)) == before
    sql(ready, "UPDATE targets SET enabled=false WHERE id=%s", (ready["target_id"],))
    restarted = PostgresFollowStore(ready["pool"])
    unfollow = await restarted.claim_next("restarted-worker", NOW + timedelta(hours=120))
    assert unfollow and unfollow.id == claim.id and unfollow.action == "unfollow"
    assert unfollow.owner_id == claim.owner_id


@pytest.mark.parametrize("source", ["target", "engagement"])
async def test_source_cleanup_retains_uncertain_follow_evidence(ready, source):
    store = ready["store"]
    assert await store.enqueue_confirmed(NOW) == 1
    claim = await store.claim_next("worker", NOW)
    assert claim and await store.mark_attempting(claim, NOW)
    evidence = {"mutation_attempted": True, "request_forwarded": True}
    assert await store.finish(claim, "uncertain", NOW, evidence)
    query = "SELECT status,owner_id,follow_attempted_at,evidence FROM relationship_follows WHERE id=%s"
    before = sql(ready, query, (claim.id,))
    table, identifier = (
        ("targets", ready["target_id"])
        if source == "target"
        else ("story_engagements", ready["engagement_id"])
    )
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        sql(ready, f"DELETE FROM {table} WHERE id=%s", (identifier,))
    assert sql(ready, query, (claim.id,)) == before
    restarted = PostgresFollowStore(ready["pool"])
    assert await restarted.claim_next("restarted-worker", NOW + timedelta(days=6)) is None


async def test_customer_target_removal_explains_retained_follow_history(ready):
    claim = await following(ready)
    repo = ControlRepository(ready["pool"])
    with pytest.raises(ValueError, match="follow history"):
        repo.remove_target(ready["user_id"], ready["account_id"], ready["target_id"])
    assert sql(
        ready, "SELECT status FROM relationship_follows WHERE id=%s", (claim.id,)
    ) == [("following",)]
    assert repo.remove_target(uuid4(), ready["account_id"], ready["target_id"]) is False


async def test_explicit_account_deletion_still_removes_its_scoped_ledger(ready):
    await following(ready)
    sql(ready, "DELETE FROM ig_accounts WHERE id=%s", (ready["account_id"],))
    assert sql(ready, "SELECT count(*) FROM relationship_follows") == [(0,)]


async def uncertain_owned_follow(w):
    sql(w,"UPDATE agent_configs SET follow_reel_message_enabled=true,follow_reel_message_enabled_at=%s WHERE ig_account_id=%s",(NOW-timedelta(hours=1),w['account_id']))
    store=w['store']
    assert await store.enqueue_confirmed(NOW)==1
    claim=await store.claim_next('worker',NOW)
    assert claim and await store.mark_attempting(claim,NOW)
    evidence=dict(identity_matches=True,request_forwarded=True,mutation_attempted=True,
                  following=False,private=False,followed_by=False,reason='action_unverified')
    assert await store.finish(claim,'uncertain',NOW,evidence)
    return claim


async def test_reconcile_owned_follow_retains_receipt_and_resumes_only_unattempted_dm(ready):
    claim=await uncertain_owned_follow(ready)
    before=sql(ready,"SELECT follow_attempted_at,evidence->'follow' FROM relationship_follows WHERE id=%s",(claim.id,))[0]
    with ready['pool'].connection() as c,c.transaction():
        assert record_confirmation(c,claim.user_id,claim.ig_account_id,claim.id,
            dict(handle=claim.handle,owner_id=claim.owner_id,following=True,private=False),datetime.now(UTC))
    after=sql(ready,"SELECT status,follow_attempted_at,evidence->'follow',confirmed_follow_at FROM relationship_follows WHERE id=%s",(claim.id,))[0]
    assert after==('following',before[0],before[1],before[0])
    assert sql(ready,"SELECT unfollow_due_at FROM relationship_follows WHERE id=%s",(claim.id,))==[(before[0]+timedelta(hours=120),)]
    assert sql(ready,"SELECT status,attempted_at,rotation_slot FROM follow_reel_messages WHERE relationship_follow_id=%s",(claim.id,))==[('pending',None,0)]
    assert sql(ready,"SELECT count(*) FROM mcp_audit_events WHERE tool_name='operator_uncertain_follow_reconciliation'")==[(1,)]
    with ready['pool'].connection() as c,c.transaction(),pytest.raises(ValueError):
        record_confirmation(c,claim.user_id,claim.ig_account_id,claim.id,
            dict(handle=claim.handle,owner_id=claim.owner_id,following=True,private=False),datetime.now(UTC))
    # A failed DM, disabled target and a new worker must retain the unfollow.
    sql(ready,"UPDATE follow_reel_messages SET status='cancelled' WHERE relationship_follow_id=%s",(claim.id,))
    sql(ready,"UPDATE targets SET enabled=false WHERE id=%s",(claim.target_id,))
    sql(ready,"UPDATE agent_configs SET follow_after_engagement=false WHERE ig_account_id=%s",(claim.ig_account_id,))
    restarted=PostgresFollowStore(ready['pool'])
    due=before[0]+timedelta(hours=120)
    unfollow=await restarted.claim_next('restarted-worker',due)
    assert unfollow and unfollow.id==claim.id and unfollow.action=='unfollow'
    assert unfollow.owner_id==claim.owner_id and unfollow.followed_at==before[0]


@pytest.mark.parametrize('bad',['foreign_owner','foreign_tenant','absent','private','stale','baseline_missing','attempted_message'])
async def test_reconciliation_rejects_unproven_follows_and_never_replays_message(ready,bad):
    claim=await uncertain_owned_follow(ready)
    profile=dict(handle=claim.handle,owner_id=claim.owner_id,following=True,private=False)
    uid,observed=claim.user_id,datetime.now(UTC)
    if bad=='foreign_owner': profile['owner_id']='999'
    if bad=='foreign_tenant': uid=uuid4()
    if bad=='absent': profile['following']=False
    if bad=='private': profile['private']=True
    if bad=='stale': observed-=timedelta(minutes=6)
    if bad=='baseline_missing': sql(ready,"UPDATE relationship_follows SET evidence='{}' WHERE id=%s",(claim.id,))
    if bad=='attempted_message':
        sql(ready,"UPDATE follow_reel_messages SET attempted_at=%s WHERE relationship_follow_id=%s",(NOW,claim.id))
        with ready['pool'].connection() as c,c.transaction():
            assert record_confirmation(c,uid,claim.ig_account_id,claim.id,profile,observed) is False
        assert sql(ready,"SELECT status,attempted_at FROM follow_reel_messages WHERE relationship_follow_id=%s",(claim.id,))==[('uncertain',NOW)]
    else:
        with pytest.raises(ValueError),ready['pool'].connection() as c,c.transaction():
            record_confirmation(c,uid,claim.ig_account_id,claim.id,profile,observed)
        assert sql(ready,"SELECT status FROM relationship_follows WHERE id=%s",(claim.id,))==[('uncertain',)]



async def test_confirmed_follow_due_exactly_120_hours_and_unfollow(ready):
    store = ready["store"]
    claim = await following(ready)
    assert await store.enqueue_confirmed(NOW) == 0
    assert (
        await store.claim_next(
            "worker", NOW + timedelta(hours=120) - timedelta(microseconds=1)
        )
        is None
    )
    sql(
        ready,
        "UPDATE agent_configs SET follow_after_engagement=false WHERE ig_account_id=%s",
        (ready["account_id"],),
    )
    due = NOW + timedelta(hours=120)
    unfollow = await store.claim_next("worker", due)
    assert unfollow.action == "unfollow" and unfollow.followed_at == NOW
    assert await store.mark_attempting(unfollow, due)
    assert await store.finish(unfollow, "confirmed", due, {"confirmed": True})
    assert sql(
        ready,
        "SELECT status,unfollow_due_at FROM relationship_follows WHERE id=%s",
        (claim.id,),
    ) == [("unfollowed", due)]
    assert sql(ready, "SELECT count(*) FROM engagement_usage_events") == [(0,)]


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE story_engagements SET status='failed'",
        "UPDATE hermes_tool_calls SET status='uncertain'",
        "UPDATE hermes_execution_runs SET status='failed'",
        "UPDATE jobs SET status='failed'",
        "UPDATE agent_configs SET follow_enabled_at=now()+interval '1 day'",
        "UPDATE targets SET ig_user_id='not-numeric'",
    ],
)
async def test_enqueue_requires_confirmed_recent_owned_evidence(ready, mutation):
    sql(ready, mutation)
    assert await ready["store"].enqueue_confirmed(NOW) == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE ig_accounts SET health_status='paused'",
        "UPDATE ig_accounts SET paused_at=now()",
        "UPDATE agent_configs SET enabled=false",
        "UPDATE agent_configs SET follow_after_engagement=false",
        "UPDATE agent_configs SET daily_follow_limit=0",
        "UPDATE proxies SET health_status='degraded'",
        "UPDATE proxy_bindings SET released_at=now()",
        "INSERT INTO instagram_execution_controls(scope,action,reason_code) VALUES ('action','follow','test')",
    ],
)
async def test_attempt_rechecks_policy_after_claim(ready, mutation):
    store = ready["store"]
    await store.enqueue_confirmed(NOW)
    claim = await store.claim_next("worker", NOW)
    sql(ready, mutation)
    assert not await store.mark_attempting(claim, NOW)
    assert await store.finish(claim, "deferred", NOW, {})


async def test_unfollow_off_preserves_due_and_rechecks_after_claim(ready):
    store = ready["store"]
    await following(ready)
    due = NOW + timedelta(hours=120)
    sql(ready, "UPDATE agent_configs SET auto_unfollow_enabled=false")
    assert await store.claim_next("worker", due) is None
    assert sql(ready, "SELECT status,unfollow_due_at FROM relationship_follows") == [
        ("following", due)
    ]
    sql(ready, "UPDATE agent_configs SET auto_unfollow_enabled=true")
    claim = await store.claim_next("worker", due)
    sql(ready, "UPDATE agent_configs SET auto_unfollow_enabled=false")
    assert not await store.mark_attempting(claim, due)
    assert await store.finish(claim, "deferred", due, {})
    sql(ready, "UPDATE agent_configs SET auto_unfollow_enabled=true")
    assert await store.claim_next("worker", due + timedelta(minutes=4)) is None
    assert (
        await store.claim_next("worker", due + timedelta(minutes=5))
    ).action == "unfollow"


async def test_stale_or_foreign_claim_cannot_finish_and_expiry_never_replays(ready):
    store = ready["store"]
    await store.enqueue_confirmed(NOW)
    claim = await store.claim_next("worker", NOW)
    assert not await store.mark_attempting(
        replace(claim, user_id=ready["other_user_id"]), NOW
    )
    assert not await store.finish(
        replace(claim, lease_token=uuid4()), "already_following", NOW, {}
    )
    assert await store.claim_next("other", NOW + timedelta(seconds=121)) is None
    assert not await store.finish(claim, "confirmed", NOW + timedelta(seconds=121), {})
    assert sql(ready, "SELECT status FROM relationship_follows") == [("uncertain",)]


async def test_preexisting_follow_never_creates_owned_unfollow(ready):
    store = ready["store"]
    await store.enqueue_confirmed(NOW)
    claim = await store.claim_next("worker", NOW)
    assert await store.finish(claim, "already_following", NOW, {})
    assert sql(
        ready,
        "SELECT status,confirmed_follow_at,unfollow_due_at FROM relationship_follows",
    ) == [("skipped", None, None)]
    assert await store.enqueue_confirmed(NOW) == 0
    assert await store.claim_next("worker", NOW + timedelta(days=6)) is None


async def test_attempt_once_and_pause_after_send_still_persists(ready):
    store = ready["store"]
    await store.enqueue_confirmed(NOW)
    claim = await store.claim_next("worker", NOW)
    assert not await store.finish(claim, "confirmed", NOW, {})
    assert await store.mark_attempting(claim, NOW)
    assert not await store.mark_attempting(claim, NOW)
    sql(ready, "UPDATE ig_accounts SET health_status='paused'")
    assert await store.finish(claim, "confirmed", NOW, {})
    assert not await store.finish(claim, "confirmed", NOW, {})


async def test_active_campaign_and_scoped_control_block_claim(ready):
    store = ready["store"]
    await store.enqueue_confirmed(NOW)
    sql(
        ready,
        "UPDATE jobs SET status='running',lease_expires_at=%s",
        (NOW + timedelta(minutes=5),),
    )
    assert await store.claim_next("worker", NOW) is None
    sql(ready, "UPDATE jobs SET status='completed'")
    sql(
        ready,
        "INSERT INTO instagram_execution_controls(scope,user_id,reason_code) VALUES ('tenant',%s,'test')",
        (ready["other_user_id"],),
    )
    assert await store.claim_next("worker", NOW)


def more_targets(w, count):
    for index in range(count):
        target, engagement = uuid4(), uuid4()
        sql(
            w,
            "INSERT INTO targets (id,user_id,ig_account_id,username,ig_user_id,tier,source) VALUES (%s,%s,%s,%s,%s,'vip','manual')",
            (target, w["user_id"], w["account_id"], f"new{index}", str(200000 + index)),
        )
        sql(
            w,
            "INSERT INTO story_engagements (id,user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at) VALUES (%s,%s,%s,%s,%s,'like','sent',%s)",
            (
                engagement,
                w["user_id"],
                w["account_id"],
                target,
                str(index).zfill(64),
                NOW,
            ),
        )
        sql(
            w,
            "INSERT INTO hermes_tool_calls (run_id,tool_name,target_id,story_fingerprint,idempotency_key,status) VALUES (%s,'like',%s,%s,%s,'confirmed')",
            (w["run_id"], target, str(index).zfill(64), str(uuid4())),
        )


@pytest.mark.parametrize("age,expected", [(1, 3), (5, 5), (10, 8), (20, 12), (60, 20)])
async def test_daily_cap_respects_warmup_and_persists_after_terminal_states(
    ready, age, expected
):
    store = ready["store"]
    more_targets(ready, 20)
    sql(ready, "UPDATE agent_configs SET daily_follow_limit=20")
    sql(
        ready,
        "UPDATE ig_accounts SET warmup_started_at=%s",
        (NOW - timedelta(days=age),),
    )
    assert await store.enqueue_confirmed(NOW) == 21
    for _ in range(expected):
        claim = await store.claim_next("worker", NOW)
        assert claim is not None
        assert await store.finish(claim, "already_following", NOW, {})
    assert await store.claim_next("worker", NOW) is None
    assert await store.claim_next("worker", NOW + timedelta(days=1)) is not None


async def test_concurrent_claims_and_lowered_cap(ready):
    import asyncio

    store = ready["store"]
    more_targets(ready, 2)
    sql(ready, "UPDATE agent_configs SET daily_follow_limit=1")
    await store.enqueue_confirmed(NOW)
    claims = await asyncio.gather(
        store.claim_next("a", NOW), store.claim_next("b", NOW)
    )
    assert sum(c is not None for c in claims) == 1
    claim = next(c for c in claims if c)
    assert await store.finish(claim, "deferred", NOW, {})
    # A pre-click deferral may retry its same reservation at the next due time.
    claim = await store.claim_next("a", NOW + timedelta(minutes=5))
    assert claim
    assert await store.finish(
        claim, "already_following", NOW + timedelta(minutes=5), {}
    )
    assert await store.claim_next("a", NOW + timedelta(minutes=5)) is None


async def test_unfollow_daily_cap_twenty(ready):
    store = ready["store"]
    more_targets(ready, 20)
    await store.enqueue_confirmed(NOW)
    sql(
        ready,
        "UPDATE relationship_follows SET status='following',confirmed_follow_at=%s,unfollow_due_at=%s",
        (NOW - timedelta(days=5), NOW),
    )
    for _ in range(20):
        claim = await store.claim_next("a", NOW)
        assert claim and claim.action == "unfollow"
        assert await store.finish(claim, "already_absent", NOW, {})
    assert await store.claim_next("a", NOW) is None


async def test_serialized_campaign_claim_waits_for_relationship_lease(ready):
    from engagemate.db.jobs import JobRepository

    # JobRepository compares relationship leases with the database clock, not
    # with the module-level NOW. A lease stamped from NOW expires 120 seconds
    # after import, so this test failed whenever the full suite reached it late
    # (main CI on 2026-09-17). Stamp the lease from the current time instead.
    now = datetime.now(UTC)
    store = ready["store"]
    await store.enqueue_confirmed(now)
    claim = await store.claim_next("a", now)
    sql(
        ready,
        "UPDATE jobs SET status='queued',scheduled_for=%s",
        (now - timedelta(minutes=1),),
    )
    jobs = JobRepository(ready["pool"], serialize_account_actions=True)
    assert jobs.claim_next("campaign") is None
    assert await store.finish(claim, "already_following", now, {})
    assert jobs.claim_next("campaign") is not None
    more_targets(ready, 1)
    await store.enqueue_confirmed(now)
    assert await store.claim_next("a", now) is None


async def test_deferred_after_mutation_is_uncertain_and_cannot_replay(ready):
    store = ready["store"]
    await store.enqueue_confirmed(NOW)
    claim = await store.claim_next("worker", NOW)
    assert await store.mark_attempting(claim, NOW)
    assert await store.finish(claim, "deferred", NOW, {})
    assert sql(ready, "SELECT status FROM relationship_follows") == [("uncertain",)]
    assert await store.claim_next("worker", NOW + timedelta(days=1)) is None


async def test_unfollow_preclick_clock_cannot_precede_due(ready):
    store = ready["store"]
    await following(ready)
    due = NOW + timedelta(hours=120)
    claim = await store.claim_next("a", due)
    assert not await store.mark_attempting(claim, due - timedelta(seconds=1))
    assert await store.mark_attempting(claim, due)


@pytest.mark.parametrize("scope", ["system", "tenant", "account"])
async def test_matching_execution_control_blocks_claim(ready, scope):
    store = ready["store"]
    await store.enqueue_confirmed(NOW)
    user = ready["user_id"] if scope != "system" else None
    account = ready["account_id"] if scope == "account" else None
    sql(
        ready,
        "INSERT INTO instagram_execution_controls(scope,user_id,ig_account_id,reason_code) VALUES (%s,%s,%s,'test')",
        (scope, user, account),
    )
    assert await store.claim_next("a", NOW) is None


async def test_qualifying_reconnect_restarts_follow_warmup(ready):
    store = ready["store"]
    more_targets(ready, 4)
    sql(ready, "UPDATE agent_configs SET daily_follow_limit=20")
    sql(
        ready,
        "INSERT INTO account_health_events(user_id,ig_account_id,event_type,severity,evidence,created_at) VALUES (%s,%s,'warmup_restart','info','{}',%s)",
        (ready["user_id"], ready["account_id"], NOW - timedelta(hours=1)),
    )
    await store.enqueue_confirmed(NOW)
    for _ in range(3):
        claim = await store.claim_next("a", NOW)
        assert claim
        assert await store.finish(claim, "already_following", NOW, {})
    assert await store.claim_next("a", NOW) is None


@pytest.mark.parametrize('baseline,identity,restricted,expected',[
    (False,True,False,1),(True,True,False,0),(False,False,False,0),(False,True,True,0),
])
async def test_followback_observation_requires_baseline_and_verified_identity(ready,baseline,identity,restricted,expected):
    store=ready['store']
    await store.enqueue_confirmed(NOW)
    claim=await store.claim_next('worker',NOW)
    assert await store.mark_attempting(claim,NOW)
    assert await store.finish(claim,'confirmed',NOW,{'followed_by':baseline})
    due=NOW+timedelta(hours=120)
    unfollow=await store.claim_next('worker',due)
    evidence={'identity_matches':identity,'followed_by':True}
    if restricted:
        evidence['health_signal']='rate_limited'
    assert await store.finish(unfollow,'deferred',due,evidence)
    assert sql(ready,'SELECT count(*) FROM relationship_followback_observations')==[(expected,)]
    # Later relationship evidence may change; the first verified event survives.
    sql(ready,"UPDATE relationship_follows SET evidence=evidence-'unfollow'")
    assert sql(ready,'SELECT count(*) FROM relationship_followback_observations')==[(expected,)]
