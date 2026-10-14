"""Exact first-attempt LIKE checks against the actual migrated stores."""

from contextlib import asynccontextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from engagemate.worker import production_check
from engagemate.worker.canary import CanaryError
from engagemate.worker.follow_store import PendingLikeFollowScope, PostgresFollowStore
from engagemate.worker.follow_reel_store import ReelMessageStoreScope, PostgresFollowReelStore
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401
from tests.worker.test_follow_store import NOW, ready, sql  # noqa: F401
from tests.worker.test_production_check import BorrowedPool
from tests.worker.test_scoped_follow_reel_stores import add_relationship, add_message


def snapshot(w):
    return (sql(w,'SELECT * FROM relationship_follows ORDER BY id'),
            sql(w,'SELECT * FROM follow_reel_messages ORDER BY id'))


@pytest.fixture
async def pending_like(ready):
    w = ready
    sql(w,"UPDATE ig_accounts SET ig_user_id='999' WHERE id=%s",(w['account_id'],))
    sql(w,"""UPDATE agent_configs SET follow_reel_message_enabled=true,
        follow_reel_message_enabled_at=%s,daily_reply_limit=10 WHERE ig_account_id=%s""",
        (NOW-timedelta(minutes=1),w['account_id']))
    sql(w,"UPDATE hermes_execution_runs SET started_at=%s WHERE id=%s",
        (NOW-timedelta(seconds=2),w['run_id']))
    sql(w,"UPDATE hermes_tool_calls SET started_at=%s,completed_at=%s WHERE run_id=%s",
        (NOW-timedelta(seconds=1),NOW,w['run_id']))
    assert await w['store'].enqueue_confirmed(NOW) == 1
    claim = await w['store'].claim_next('earlier-preflight',NOW)
    assert claim is not None
    assert await w['store'].finish(claim,'deferred',NOW,{})
    # A failed preflight has no social attempt. Preserve its original history.
    sql(w,"UPDATE relationship_follows SET next_attempt_at=NULL WHERE id=%s",(claim.id,))
    w['relationship_id'] = claim.id
    w['message_id'] = sql(w,'SELECT id FROM follow_reel_messages WHERE relationship_follow_id=%s',(claim.id,))[0][0]
    w['scope'] = PendingLikeFollowScope(w['user_id'],w['account_id'],w['engagement_id'],claim.id)
    w['scoped_store'] = PostgresFollowStore(w['pool'],scope=w['scope'])
    return w


def test_like_scopes_require_exact_identifiers_and_boolean():
    scope = PendingLikeFollowScope(uuid4(),uuid4(),uuid4(),uuid4())
    with pytest.raises(FrozenInstanceError):
        scope.relationship_follow_id = uuid4()
    for key in ('user_id','account_id','prerequisite_engagement_id','relationship_follow_id'):
        with pytest.raises(ValueError):
            replace(scope,**{key:str(getattr(scope,key))})
    for value in (None,1,'true'):
        with pytest.raises(ValueError):
            ReelMessageStoreScope(scope.user_id,scope.account_id,message_id=uuid4(),first_attempt_only=value)


async def test_exact_like_revives_only_unattempted_reservation_without_new_history(pending_like):
    w = pending_like
    old = NOW-timedelta(minutes=30)
    sql(w, "UPDATE agent_configs SET follow_reel_message_enabled_at=%s WHERE ig_account_id=%s",
        (NOW-timedelta(hours=1), w['account_id']))
    sql(w, "UPDATE follow_reel_messages SET created_at=%s,reserved_at=%s WHERE id=%s",
        (old, old, w['message_id']))
    assert production_check._require_pending_like(w['pool'],w['scope'],NOW) == w['message_id']
    before = sql(w,'SELECT created_at,claim_count,attempted_at FROM follow_reel_messages WHERE id=%s',(w['message_id'],))
    assert await w['scoped_store'].enqueue_confirmed(NOW) == 0
    claim = await w['scoped_store'].claim_next('exact-like',NOW)
    assert claim.id == w['relationship_id'] and claim.action == 'follow'
    assert sql(w,'SELECT created_at,claim_count,attempted_at FROM follow_reel_messages WHERE id=%s',(w['message_id'],)) == before
    assert await w['scoped_store'].mark_attempting(claim,NOW)
    assert await w['scoped_store'].finish(claim,'confirmed',NOW,{'confirmed':True})
    assert production_check._new_message(w['pool'],w['scope'],claim.id,NOW,expected_message_id=w['message_id']) == w['message_id']
    assert await w['scoped_store'].claim_next('again',NOW) is None


async def test_first_reel_expired_claim_cleanup_recovers_claimed_row(pending_like):
    w = pending_like
    follow = await w['scoped_store'].claim_next('follow',NOW)
    assert await w['scoped_store'].mark_attempting(follow,NOW)
    assert await w['scoped_store'].finish(follow,'confirmed',NOW,{'confirmed':True})
    store = PostgresFollowReelStore(w['pool'], lease_seconds=30, scope=ReelMessageStoreScope(
        w['user_id'], w['account_id'], message_id=w['message_id'], first_attempt_only=True,
    ))
    assert await store.claim_next('reel',NOW)
    assert await store.claim_next('recovery',NOW+timedelta(seconds=31)) is None
    assert sql(w, "SELECT status,evidence->>'reason' FROM follow_reel_messages WHERE id=%s",
        (w['message_id'],)) == [('pending','lease_expired')]


@pytest.mark.parametrize('field',['user_id','account_id','prerequisite_engagement_id','relationship_follow_id'])
async def test_wrong_exact_selector_cannot_read_claim_attempt_finish_or_cleanup(pending_like,field):
    w = pending_like
    wrong = replace(w['scope'],**{field:uuid4()})
    before = snapshot(w)
    with pytest.raises(CanaryError,match='confirmed_like_pending_first_follow'):
        production_check._require_pending_like(w['pool'],wrong,NOW)
    store = PostgresFollowStore(w['pool'],scope=wrong)
    assert await store.enqueue_confirmed(NOW) == 0
    assert await store.claim_next('wrong',NOW+timedelta(minutes=5)) is None
    assert (snapshot(w)) == before
    claim = await w['scoped_store'].claim_next('right',NOW)
    assert not await store.mark_attempting(claim,NOW)
    assert not await store.finish(claim,'confirmed',NOW,{})


@pytest.mark.parametrize('change',[
    "UPDATE story_engagements SET action='reply'",
    "UPDATE hermes_tool_calls SET status='uncertain'",
    "UPDATE hermes_execution_runs SET status='failed'",
    "UPDATE jobs SET status='failed'",
    "UPDATE hermes_tool_calls SET completed_at=started_at",
    "UPDATE story_engagements SET executed_at=executed_at-interval '25 hours'",
    "UPDATE relationship_follows SET follow_attempted_at=now()",
    "UPDATE relationship_follows SET attempted_at=now()",
    "UPDATE relationship_follows SET status='uncertain'",
    "UPDATE relationship_follows SET confirmed_follow_at=now(),unfollow_due_at=now()+interval '120 hours'",
    "UPDATE follow_reel_messages SET attempted_at=now()",
    "UPDATE follow_reel_messages SET claim_count=1",
    "UPDATE follow_reel_messages SET created_at=created_at-interval '25 hours'",
    "UPDATE follow_reel_messages SET status='uncertain'",
    "UPDATE ig_accounts SET health_status='challenged'",
    "UPDATE ig_accounts SET paused_at=now()",
    "UPDATE agent_configs SET follow_after_engagement=false",
    "UPDATE agent_configs SET follow_reel_message_enabled=false",
    "UPDATE targets SET enabled=false",
])
async def test_preflight_refuses_history_or_ineligible_prerequisite_without_writes(pending_like,change):
    w = pending_like
    sql(w,change)
    before = snapshot(w)
    with pytest.raises(CanaryError,match='confirmed_like_pending_first_follow'):
        production_check._require_pending_like(w['pool'],w['scope'],NOW)
    assert (snapshot(w)) == before


@pytest.mark.parametrize('change',[
    "UPDATE relationship_follows SET follow_attempted_at=now()",
    "UPDATE relationship_follows SET status='uncertain'",
    "UPDATE relationship_follows SET status='follow_running',claimed_by='old',lease_token=gen_random_uuid(),lease_expires_at=now()-interval '1 hour'",
    "UPDATE follow_reel_messages SET attempted_at=now()",
    "UPDATE follow_reel_messages SET claim_count=1",
    "UPDATE follow_reel_messages SET created_at=created_at-interval '25 hours'",
    "UPDATE ig_accounts SET paused_at=now()",
    "UPDATE proxies SET health_status='degraded'",
    "UPDATE agent_configs SET daily_follow_limit=0",
    "UPDATE agent_configs SET daily_reply_limit=0,daily_dm_limit=0",
    "UPDATE agent_configs SET follow_reel_message_enabled=false",
    "UPDATE hermes_tool_calls SET completed_at=started_at",
])
async def test_claim_retains_normal_guards_and_never_recovers_attempted_history(pending_like,change):
    w = pending_like
    sql(w,change)
    before = snapshot(w)
    assert await w['scoped_store'].claim_next('blocked',NOW) is None
    assert (snapshot(w)) == before


@pytest.mark.parametrize('change',[
    "UPDATE agent_configs SET follow_reel_message_enabled=false",
    "UPDATE hermes_tool_calls SET status='uncertain'",
    "UPDATE hermes_tool_calls SET completed_at=started_at",
    "UPDATE ig_accounts SET paused_at=now()",
    "UPDATE proxies SET health_status='degraded'",
    "UPDATE agent_configs SET daily_follow_limit=0",
])
async def test_live_policy_is_rechecked_before_first_follow_mutation(pending_like,change):
    w = pending_like
    claim = await w['scoped_store'].claim_next('follow',NOW)
    assert claim is not None
    sql(w,change)
    assert not await w['scoped_store'].mark_attempting(claim,NOW)
    assert sql(w,'SELECT follow_attempted_at,attempted_at FROM relationship_follows WHERE id=%s',(claim.id,)) == [(None,None)]


async def test_exact_scope_does_not_touch_unrelated_pending_or_expired_rows(pending_like):
    w = pending_like
    ids = [add_relationship(w,status=status,expired=True)[0] for status in ('follow_pending','follow_running','unfollow_running')]
    message,_ = add_message(w,status='running',expired=True,attempted=True)
    before = sql(w,'SELECT * FROM relationship_follows WHERE id=ANY(%s) ORDER BY id',(ids,)),sql(w,'SELECT * FROM follow_reel_messages WHERE id=%s',(message,))
    assert await w['scoped_store'].claim_next('exact',NOW) is not None
    assert (sql(w,'SELECT * FROM relationship_follows WHERE id=ANY(%s) ORDER BY id',(ids,)),sql(w,'SELECT * FROM follow_reel_messages WHERE id=%s',(message,))) == before


async def test_first_reel_claim_cannot_claim_again_after_unattempted_deferral(pending_like):
    w = pending_like
    claim = await w['scoped_store'].claim_next('follow',NOW)
    assert await w['scoped_store'].mark_attempting(claim,NOW)
    assert await w['scoped_store'].finish(claim,'confirmed',NOW,{})
    store = PostgresFollowReelStore(w['pool'],scope=ReelMessageStoreScope(w['user_id'],w['account_id'],message_id=w['message_id'],first_attempt_only=True))
    message = await store.claim_next('reel',NOW)
    assert message.id == w['message_id']
    assert await store.prepare(message,'https://www.instagram.com/reel/check/','Hello\nhttps://www.instagram.com/reel/check/',NOW)
    assert await store.finish(message,'deferred',{},NOW)
    before = sql(w,'SELECT * FROM follow_reel_messages WHERE id=%s',(message.id,))
    assert await store.claim_next('replay',NOW+timedelta(minutes=10)) is None
    assert sql(w,'SELECT * FROM follow_reel_messages WHERE id=%s',(message.id,)) == before


@pytest.mark.parametrize('outcome',['confirmed','uncertain','deferred'])
async def test_like_operator_uses_actual_scoped_stores_once(pending_like,monkeypatch,outcome):
    from deploy import healthcheck
    from engagemate import config
    from engagemate.db import pool
    from engagemate.worker import follow_runtime,follow_reel_runtime
    from playwright import async_api
    w = pending_like
    monkeypatch.setattr(healthcheck,'validate_worker_environment',lambda _:None)
    monkeypatch.setattr(config,'Settings',lambda:NS(instagram_execution_mode='autonomous',database_url='fixture'))
    monkeypatch.setattr(pool,'create_pool',lambda *a,**k:BorrowedPool(w['pool']))
    events = []
    @asynccontextmanager
    async def playwright():
        yield object()
    async def follow(claim,before):
        assert claim.id == w['relationship_id']
        events.append('follow')
        if outcome != 'deferred':
            assert await before()
        return outcome,{'identity_matches':True}
    async def reel(claim,prepare,before):
        assert claim.id == w['message_id']
        events.append('reel')
        url = 'https://www.instagram.com/reel/check/'
        assert await prepare(url,'Hello\n'+url) and await before()
        return 'sent',{'server_confirmed':True,'sender_id':claim.sender_id,'recipient_id':claim.recipient_id,'thread_id':'111','item_id':'222'}
    monkeypatch.setattr(async_api,'async_playwright',playwright)
    monkeypatch.setattr(follow_runtime,'LiveFollowPerformer',lambda *a:follow)
    monkeypatch.setattr(follow_reel_runtime,'LiveFollowReelPerformer',lambda *a:reel)
    result = await production_check.run_follow_reel(w['scope'],worker_id='like-check')
    assert result['prerequisite_action'] == 'like'
    assert events == (['follow','reel'] if outcome == 'confirmed' else ['follow'])
    assert (result['reel'] is not None) == (outcome == 'confirmed')
    if outcome != 'deferred':
        with pytest.raises(CanaryError,match='confirmed_like_pending_first_follow'):
            await production_check.run_follow_reel(w['scope'],worker_id='again')


def test_cli_requires_exact_like_relationship_and_preserves_reply_command(monkeypatch,capsys):
    seen = []
    async def run(scope,*,worker_id):
        seen.append((scope,worker_id))
        return {'prerequisite_action':'like','follow':None,'reel':None}
    monkeypatch.setattr(production_check,'run_follow_reel',run)
    args = ['follow-reel-like','--user-id',str(uuid4()),'--account-id',str(uuid4()),'--worker-id','exact','--engagement-id',str(uuid4())]
    with pytest.raises(SystemExit):
        production_check.main(args)
    relationship = uuid4()
    assert production_check.main(args+['--relationship-id',str(relationship)]) == 3
    assert type(seen[0][0]) is PendingLikeFollowScope and seen[0][0].relationship_follow_id == relationship
    assert '"prerequisite_action": "like"' in capsys.readouterr().out
