"""Post-follow messages are opt-in, scoped and never replay ambiguous writes."""
from datetime import timedelta
import pytest
from engagemate.worker.follow_reel_store import PostgresFollowReelStore
from tests.worker.test_follow_store import NOW, ready, following, sql
from tests.worker.test_canary_scripts import database_url, world

@pytest.fixture
def enabled(ready):
    sql(ready,"UPDATE agent_configs SET follow_reel_message_enabled=true,follow_reel_message_enabled_at=%s,daily_reply_limit=10",(NOW-timedelta(minutes=1),))
    sql(ready,"UPDATE ig_accounts SET ig_user_id='999',username='sender'")
    return ready

async def claim(w, now=None):
    await following(w)
    store=PostgresFollowReelStore(w['pool'])
    message=await store.claim_next('messages',NOW if now is None else now)
    assert message
    return store,message

async def test_disabled_follows_do_not_reserve(ready):
    sql(ready,'UPDATE agent_configs SET daily_reply_limit=0,daily_dm_limit=0')
    await following(ready)
    assert sql(ready,'SELECT count(*) FROM follow_reel_messages')==[(0,)]
    assert sql(ready,'SELECT count(*) FROM follow_reel_rotation')==[(0,)]

async def test_opt_in_does_not_backfill_old_follow(ready):
    await following(ready)
    sql(ready,'UPDATE agent_configs SET follow_reel_message_enabled=true,follow_reel_message_enabled_at=%s',(NOW,))
    assert await PostgresFollowReelStore(ready['pool']).claim_next('messages',NOW) is None

@pytest.mark.parametrize('mutation',[
    'UPDATE agent_configs SET follow_reel_message_enabled=false',
    'UPDATE agent_configs SET enabled=false',
    'UPDATE agent_configs SET daily_reply_limit=0,daily_dm_limit=0',
    'UPDATE ig_accounts SET paused_at=now()',
    "UPDATE targets SET ig_user_id='987'",
    "UPDATE proxies SET health_status='degraded'",
    "UPDATE agent_configs SET follow_reel_message_enabled_at=now()+interval '1 day'",
    "INSERT INTO instagram_execution_controls(scope,action,reason_code) VALUES ('action','dm','test')",
])
async def test_attempt_and_prepare_recheck_policy(enabled,mutation):
    store,message=await claim(enabled)
    url='https://www.instagram.com/reel/abc123/'
    assert await store.prepare(message,url,'hello\n'+url,NOW)
    sql(enabled,mutation)
    assert not await store.prepare(message,url,'hello\n'+url,NOW)
    assert not await store.mark_attempting(message,NOW)

async def test_frozen_retry_and_receipt_validation(enabled):
    store,message=await claim(enabled)
    url='https://www.instagram.com/reel/abc123/'
    assert await store.prepare(message,url,'hello\n'+url,NOW)
    assert await store.finish(message,'deferred',{},NOW)
    later=NOW+timedelta(minutes=5)
    retry=await store.claim_next('messages',later)
    assert retry.body=='hello\n'+url and retry.reel_url==url
    assert not await store.prepare(retry,url,'changed\n'+url,later)
    assert await store.mark_attempting(retry,later)
    assert not await store.mark_attempting(retry,later)
    assert not await store.finish(retry,'sent',{'server_confirmed':True,'recipient_id':retry.recipient_id},later)
    assert await store.finish(retry,'sent',{'server_confirmed':True,'recipient_id':retry.recipient_id,'sender_id':retry.sender_id,'item_id':'123','thread_id':'456'},later)
    assert await store.claim_next('messages',later) is None


async def test_unknown_preflight_clears_previous_known_diagnostic_in_merged_evidence(enabled):
    from engagemate.proxy.preflight import preflight_failure_evidence

    store, message = await claim(enabled)
    assert await store.finish(message, 'deferred', preflight_failure_evidence('proxy_access_denied'), NOW)
    later = NOW + timedelta(minutes=5)
    retry = await store.claim_next('messages', later)
    assert retry is not None and retry.id == message.id
    unknown = preflight_failure_evidence('http://private_user:private_password@proxy.invalid/')
    assert await store.finish(retry, 'deferred', unknown, later)
    evidence = sql(enabled, 'SELECT evidence FROM follow_reel_messages WHERE id=%s', (message.id,))[0][0]
    assert evidence == unknown
    assert evidence['preflight_reason'] is None and evidence['phase'] is None
    assert evidence['mutation_attempted'] is False
    assert sql(enabled, 'SELECT attempted_at,sent_at FROM follow_reel_messages WHERE id=%s', (message.id,)) == [(None, None)]

@pytest.mark.parametrize('attempted',[True,False])
async def test_expired_lease_recovery_never_replays_attempted_write(enabled,attempted):
    store,message=await claim(enabled)
    url='https://www.instagram.com/reel/abc123/'
    assert await store.prepare(message,url,'hello\n'+url,NOW)
    if attempted: assert await store.mark_attempting(message,NOW)
    assert await store.claim_next('messages',NOW+timedelta(minutes=4)) is None
    expected = 'uncertain' if attempted else 'pending'
    assert sql(enabled,'SELECT status FROM follow_reel_messages')==[(expected,)]
    if attempted:
        assert await store.claim_next('messages',NOW+timedelta(minutes=5)) is None
    else:
        retry = await store.claim_next('messages',NOW+timedelta(minutes=9))
        assert retry is not None and retry.id == message.id


async def test_expired_unattempted_lease_stays_bounded_by_claim_limit(enabled):
    store, message = await claim(enabled)
    url = 'https://www.instagram.com/reel/abc123/'
    assert await store.prepare(message, url, 'hello\n' + url, NOW)

    # Four recovered leases can be retried. The fifth claim is the final
    # allowed claim, and its expiry is retired by the existing claim limit.
    current = NOW + timedelta(minutes=4)
    for _ in range(1, 5):
        assert await store.claim_next('messages', current) is None
        current += timedelta(minutes=5)
        retry = await store.claim_next('messages', current)
        assert retry is not None and retry.id == message.id
        current += timedelta(minutes=4)
    assert await store.claim_next('messages', current) is None
    assert sql(enabled, "SELECT status,claim_count,evidence->>'reason' FROM follow_reel_messages") == [
        ('cancelled', 5, 'retry_exhausted')
    ]


async def test_unattempted_transient_preflight_survives_claim_limit(enabled):
    from engagemate.proxy.preflight import preflight_failure_evidence

    store, message = await claim(enabled)
    evidence = preflight_failure_evidence('proxy_access_denied')
    assert await store.finish(message, 'deferred', evidence, NOW)
    sql(enabled, 'UPDATE follow_reel_messages SET claim_count=5')

    retry = await store.claim_next('messages', NOW + timedelta(minutes=5))
    assert retry is not None and retry.id == message.id
    assert await store.finish(retry, 'deferred', evidence, NOW + timedelta(minutes=5))
    status, next_attempt, saved = sql(
        enabled,
        "SELECT status,next_attempt_at,evidence FROM follow_reel_messages",
    )[0]
    assert status == 'pending'
    assert next_attempt == NOW + timedelta(minutes=35)
    assert saved['reason'] == 'proxy_or_session_unavailable'
    assert saved['preflight_reason'] == 'proxy_access_denied'


async def test_terminal_retry_preserves_last_deferred_reason(enabled):
    store, message = await claim(enabled)
    assert await store.finish(message, 'deferred', {'reason': 'three_sender_reels_required'}, NOW)
    sql(enabled, 'UPDATE follow_reel_messages SET claim_count=5')
    assert await store.claim_next('messages', NOW + timedelta(minutes=5)) is None
    saved = sql(enabled, "SELECT evidence FROM follow_reel_messages")[0][0]
    assert saved['reason'] == 'retry_exhausted'
    assert saved['deferred_reason'] == 'three_sender_reels_required'

async def test_disable_during_follow_cancels_message(enabled):
    store=enabled['store']
    await store.enqueue_confirmed(NOW)
    follow=await store.claim_next('worker',NOW)
    assert await store.mark_attempting(follow,NOW)
    sql(enabled,'UPDATE agent_configs SET follow_reel_message_enabled=false')
    assert await store.finish(follow,'confirmed',NOW,{'confirmed':True})
    assert sql(enabled,'SELECT status FROM follow_reel_messages')==[('cancelled',)]

async def test_bounded_retries_and_pending_ttl(enabled):
    store,message=await claim(enabled)
    assert await store.finish(message,'deferred',{},NOW)
    sql(enabled,'UPDATE follow_reel_messages SET claim_count=5')
    assert await store.claim_next('messages',NOW+timedelta(minutes=5)) is None
    assert sql(enabled,"SELECT status,evidence->>'reason' FROM follow_reel_messages")==[('cancelled','retry_exhausted')]

async def test_identity_change_blocks_send(enabled):
    store,message=await claim(enabled)
    url='https://www.instagram.com/reel/abc123/'
    assert await store.prepare(message,url,'hello\n'+url,NOW)
    sql(enabled,"UPDATE ig_accounts SET ig_user_id='444'")
    assert not await store.mark_attempting(message,NOW)

async def test_active_message_serializes_story_jobs(enabled):
    from engagemate.db.jobs import JobRepository
    # SQL admission uses the database clock. Collection-time NOW can be older
    # than the message lease by the time a long suite reaches this test.
    database_now = sql(enabled, 'SELECT now()')[0][0]
    store,message=await claim(enabled, database_now)
    sql(enabled,"UPDATE jobs SET status='queued',scheduled_for=now()-interval '1 hour'")
    jobs=JobRepository(enabled['pool'],serialize_account_actions=True)
    assert jobs.claim_next('story') is None
