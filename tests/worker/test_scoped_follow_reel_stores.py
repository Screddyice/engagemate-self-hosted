"""Exact operator scopes retain production admission and uncertain-write gates."""

from dataclasses import FrozenInstanceError
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from engagemate.worker.follow_store import FollowStoreScope, PostgresFollowStore
from engagemate.worker.follow_reel_store import ReelMessageStoreScope, PostgresFollowReelStore
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401
from tests.worker.test_follow_store import NOW, ready, sql  # noqa: F401


@pytest.fixture
def scoped_ready(ready):
    sql(ready, "UPDATE story_engagements SET action='reply'")
    sql(ready, "UPDATE hermes_tool_calls SET tool_name='reply' WHERE tool_name='like'")
    sql(ready, """UPDATE agent_configs SET follow_reel_message_enabled=true,
        follow_reel_message_enabled_at=%s,daily_reply_limit=10""", (NOW-timedelta(minutes=1),))
    sql(ready, "UPDATE ig_accounts SET ig_user_id='999' WHERE id=%s", (ready['account_id'],))
    return ready


def follow_scope(w, engagement=None, **changes):
    return FollowStoreScope(**{
        'user_id': w['user_id'], 'account_id': w['account_id'],
        'prerequisite_engagement_id': engagement or w['engagement_id'], **changes,
    })


def add_reply(w):
    target, engagement = uuid4(), uuid4()
    owner = str(uuid4().int)[:15]
    fingerprint = engagement.hex * 2
    sql(w, """INSERT INTO targets (id,user_id,ig_account_id,username,ig_user_id,tier,source)
        VALUES (%s,%s,%s,%s,%s,'vip','manual')""",
        (target,w['user_id'],w['account_id'],'prospect'+owner,owner))
    sql(w, """INSERT INTO story_engagements
        (id,user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at)
        VALUES (%s,%s,%s,%s,%s,'reply','sent',%s)""",
        (engagement,w['user_id'],w['account_id'],target,fingerprint,NOW))
    sql(w, """INSERT INTO hermes_tool_calls
        (run_id,tool_name,target_id,story_fingerprint,idempotency_key,status)
        VALUES (%s,'reply',%s,%s,%s,'confirmed')""", (w['run_id'],target,fingerprint,str(uuid4())))
    return target, engagement, owner


def add_relationship(w, *, status='follow_pending', expired=False):
    target, engagement, owner = add_reply(w)
    relationship = uuid4()
    running = status in {'follow_running', 'unfollow_running'}
    owned = status in {'following', 'unfollow_running', 'unfollowed'}
    sql(w, """INSERT INTO relationship_follows
        (id,user_id,ig_account_id,target_id,owner_id,prerequisite_engagement_id,status,
         confirmed_follow_at,unfollow_due_at,claimed_by,lease_token,lease_expires_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (relationship,w['user_id'],w['account_id'],target,owner,engagement,status,
         NOW-timedelta(hours=121) if owned else None,
         NOW-timedelta(hours=1) if owned else None,
         'other-worker' if running else None,uuid4() if running else None,
         NOW+timedelta(seconds=-1 if expired else 60) if running else None))
    return relationship, engagement, target, owner


def add_message(w, *, status='pending', expired=False, attempted=False, stale=False):
    relationship, engagement, target, owner = add_relationship(w, status='following')
    message = uuid4()
    running = status == 'running'
    sql(w, """INSERT INTO follow_reel_messages
        (id,relationship_follow_id,user_id,ig_account_id,target_id,recipient_id,
         rotation_slot,status,reserved_at,created_at,claimed_by,lease_token,lease_expires_at,attempted_at)
        VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s,%s,%s,%s)""",
        (message,relationship,w['user_id'],w['account_id'],target,owner,status,NOW,
         NOW-timedelta(hours=25) if stale else NOW,
         'other-worker' if running else None,uuid4() if running else None,
         NOW+timedelta(seconds=-1 if expired else 60) if running else None,
         NOW-timedelta(minutes=1) if attempted else None))
    return message, relationship


async def confirmed_follow(w):
    store = PostgresFollowStore(w['pool'], scope=follow_scope(w))
    assert await store.enqueue_confirmed(NOW) == 1
    claim = await store.claim_next('scoped-follow', NOW)
    assert claim and claim.action == 'follow'
    assert await store.mark_attempting(claim, NOW)
    assert await store.finish(claim, 'confirmed', NOW, {'confirmed': True})
    return claim


def reel_scope(w, relationship, by_message=False):
    selector = {'relationship_follow_id': relationship}
    if by_message:
        message = sql(w, 'SELECT id FROM follow_reel_messages WHERE relationship_follow_id=%s', (relationship,))[0][0]
        selector = {'message_id': message}
    return ReelMessageStoreScope(w['user_id'], w['account_id'], **selector)


def test_scopes_are_validated_and_immutable():
    user, account, selected = uuid4(), uuid4(), uuid4()
    scope = FollowStoreScope(user, account, selected)
    with pytest.raises(FrozenInstanceError):
        scope.account_id = uuid4()
    reel = ReelMessageStoreScope(user, account, message_id=selected)
    with pytest.raises(FrozenInstanceError):
        reel.message_id = uuid4()
    for values in [(str(user),account,selected),(user,str(account),selected),(user,account,str(selected))]:
        with pytest.raises(ValueError):
            FollowStoreScope(*values)
    for values in [{'message_id': str(selected)}, {}, {'message_id': selected, 'relationship_follow_id': selected}]:
        with pytest.raises(ValueError):
            ReelMessageStoreScope(user, account, **values)
    with pytest.raises(ValueError):
        PostgresFollowStore(None, scope='unscoped')
    with pytest.raises(ValueError):
        PostgresFollowReelStore(None, scope={})


async def test_follow_enqueue_selects_only_one_reply(scoped_ready):
    w = scoped_ready
    add_reply(w)
    store = PostgresFollowStore(w['pool'], scope=follow_scope(w))
    assert await store.enqueue_confirmed(NOW) == 1
    assert sql(w, 'SELECT prerequisite_engagement_id FROM relationship_follows') == [(w['engagement_id'],)]
    assert await store.enqueue_confirmed(NOW) == 0


@pytest.mark.parametrize('field', ['user_id','account_id','prerequisite_engagement_id'])
async def test_follow_wrong_owner_or_engagement_cannot_enqueue_or_cleanup(scoped_ready, field):
    w = scoped_ready
    own = PostgresFollowStore(w['pool'], scope=follow_scope(w))
    await own.enqueue_confirmed(NOW)
    claim = await own.claim_next('scoped-follow', NOW)
    before = sql(w, 'SELECT * FROM relationship_follows')
    foreign = w['other_user_id'] if field == 'user_id' else w['other_account_id'] if field == 'account_id' else uuid4()
    wrong = PostgresFollowStore(w['pool'], scope=follow_scope(w, **{field: foreign}))
    assert await wrong.enqueue_confirmed(NOW) == 0
    assert await wrong.claim_next('wrong', NOW+timedelta(minutes=3)) is None
    assert not await wrong.mark_attempting(claim, NOW)
    assert not await wrong.finish(claim, 'deferred', NOW, {})
    assert sql(w, 'SELECT * FROM relationship_follows') == before


async def test_follow_scope_rejects_like_prerequisite(ready):
    await ready['store'].enqueue_confirmed(NOW)
    store = PostgresFollowStore(ready['pool'], scope=follow_scope(ready))
    assert await store.enqueue_confirmed(NOW) == 0
    assert await store.claim_next('scoped-follow', NOW) is None


async def test_follow_scope_preserves_other_pending_expired_and_due_rows(scoped_ready):
    w = scoped_ready
    unrelated = [add_relationship(w, status=status, expired=expired)[0]
                 for status, expired in [('follow_pending',False),('follow_running',True),
                                         ('unfollow_running',True),('following',False)]]
    before = sql(w, 'SELECT * FROM relationship_follows WHERE id=ANY(%s) ORDER BY id', (unrelated,))
    store = PostgresFollowStore(w['pool'], scope=follow_scope(w))
    assert await store.enqueue_confirmed(NOW) == 1
    claim = await store.claim_next('scoped-follow', NOW)
    assert claim and claim.action == 'follow'
    assert sql(w, 'SELECT prerequisite_engagement_id FROM relationship_follows WHERE id=%s', (claim.id,)) == [(w['engagement_id'],)]
    assert sql(w, 'SELECT * FROM relationship_follows WHERE id=ANY(%s) ORDER BY id', (unrelated,)) == before


@pytest.mark.parametrize('status', ['uncertain','cancelled','skipped','following'])
async def test_follow_scope_never_revives_held_or_owned_row(scoped_ready, status):
    claim = await confirmed_follow(scoped_ready)
    sql(scoped_ready, 'UPDATE relationship_follows SET status=%s WHERE id=%s', (status,claim.id))
    before = sql(scoped_ready, 'SELECT * FROM relationship_follows')
    store = PostgresFollowStore(scoped_ready['pool'], scope=follow_scope(scoped_ready))
    assert await store.enqueue_confirmed(NOW) == 0
    assert await store.claim_next('scoped-follow', NOW+timedelta(days=6)) is None
    assert sql(scoped_ready, 'SELECT * FROM relationship_follows') == before


@pytest.mark.parametrize('status', ['uncertain','cancelled'])
async def test_follow_scope_cannot_revive_old_reel_reservation(scoped_ready, status):
    w = scoped_ready
    store = PostgresFollowStore(w['pool'], scope=follow_scope(w))
    await store.enqueue_confirmed(NOW)
    follow = await store.claim_next('scoped-follow', NOW)
    assert await store.finish(follow, 'deferred', NOW, {})
    sql(w, 'UPDATE follow_reel_messages SET status=%s', (status,))
    before = sql(w, 'SELECT * FROM follow_reel_messages')
    assert await store.claim_next('scoped-follow', NOW+timedelta(minutes=5)) is None
    assert sql(w, 'SELECT * FROM follow_reel_messages') == before


async def test_scoped_follow_claims_serialize_and_expire_without_replay(scoped_ready):
    w = scoped_ready
    store = PostgresFollowStore(w['pool'], scope=follow_scope(w))
    await store.enqueue_confirmed(NOW)
    claims = await asyncio.gather(*(store.claim_next(worker,NOW) for worker in ('one','two')))
    assert sum(claim is not None for claim in claims) == 1
    assert await store.claim_next('later',NOW+timedelta(minutes=3)) is None
    assert sql(w,'SELECT status FROM relationship_follows') == [('uncertain',)]


@pytest.mark.parametrize('attempted',[False,True])
async def test_scoped_reel_claims_serialize_and_expire_without_replay(scoped_ready,attempted):
    w = scoped_ready
    follow = await confirmed_follow(w)
    store = PostgresFollowReelStore(w['pool'],scope=reel_scope(w,follow.id))
    claims = await asyncio.gather(*(store.claim_next(worker,NOW) for worker in ('one','two')))
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim)
    url = 'https://www.instagram.com/reel/abc123/'
    assert await store.prepare(claim,url,'Hello\n'+url,NOW)
    if attempted:
        assert await store.mark_attempting(claim,NOW)
    assert await store.claim_next('later',NOW+timedelta(minutes=4)) is None
    assert sql(w,'SELECT status FROM follow_reel_messages') == [('uncertain' if attempted else 'pending',)]


@pytest.mark.parametrize('by_message', [False,True])
async def test_exact_follow_reply_to_reserved_reel_retains_all_gates(scoped_ready, by_message):
    w = scoped_ready
    follow = await confirmed_follow(w)
    store = PostgresFollowReelStore(w['pool'], scope=reel_scope(w,follow.id,by_message))
    claim = await store.claim_next('scoped-reel', NOW)
    assert claim.relationship_follow_id == follow.id and claim.rotation_slot == 0
    url = 'https://www.instagram.com/reel/abc123/'
    body = 'Hello from the account\n'+url
    assert await store.prepare(claim,url,body,NOW)
    assert await store.mark_attempting(claim,NOW)
    assert not await store.finish(claim,'sent',{'server_confirmed':True},NOW)
    receipt = {'server_confirmed':True,'recipient_id':claim.recipient_id,
               'sender_id':claim.sender_id,'item_id':'123','thread_id':'456'}
    assert await store.finish(claim,'sent',receipt,NOW)
    assert await store.claim_next('scoped-reel',NOW) is None
    assert sql(w, 'SELECT status,sent_at FROM follow_reel_messages') == [('sent',NOW)]


@pytest.mark.parametrize('by_message', [False,True])
async def test_reel_scope_preserves_other_eligible_expired_and_stale_rows(scoped_ready, by_message):
    w = scoped_ready
    follow = await confirmed_follow(w)
    unrelated = [add_message(w,**options)[0] for options in [
        {}, {'status':'running','expired':True,'attempted':True},
        {'status':'running','expired':True}, {'stale':True}]]
    before = sql(w, 'SELECT * FROM follow_reel_messages WHERE id=ANY(%s) ORDER BY id', (unrelated,))
    store = PostgresFollowReelStore(w['pool'],scope=reel_scope(w,follow.id,by_message))
    claim = await store.claim_next('scoped-reel',NOW)
    assert claim and claim.relationship_follow_id == follow.id
    assert sql(w, 'SELECT * FROM follow_reel_messages WHERE id=ANY(%s) ORDER BY id', (unrelated,)) == before


@pytest.mark.parametrize('status', ['uncertain','cancelled'])
async def test_reel_scope_holds_uncertain_and_cancelled(scoped_ready,status):
    w = scoped_ready
    follow = await confirmed_follow(w)
    sql(w,'UPDATE follow_reel_messages SET status=%s,attempted_at=%s',(status,NOW))
    before = sql(w,'SELECT * FROM follow_reel_messages')
    store = PostgresFollowReelStore(w['pool'],scope=reel_scope(w,follow.id))
    assert await store.claim_next('scoped-reel',NOW+timedelta(minutes=5)) is None
    assert sql(w,'SELECT * FROM follow_reel_messages') == before


@pytest.mark.parametrize('field',['user_id','account_id','relationship_follow_id','message_id'])
async def test_reel_wrong_scope_cannot_claim_cleanup_prepare_attempt_or_finish(scoped_ready,field):
    w = scoped_ready
    follow = await confirmed_follow(w)
    own = PostgresFollowReelStore(w['pool'],scope=reel_scope(w,follow.id))
    claim = await own.claim_next('scoped-reel',NOW)
    before = sql(w,'SELECT * FROM follow_reel_messages')
    values = {'user_id':w['user_id'],'account_id':w['account_id'],
              'message_id':claim.id} if field == 'message_id' else {
              'user_id':w['user_id'],'account_id':w['account_id'],'relationship_follow_id':follow.id}
    values[field] = w['other_user_id'] if field == 'user_id' else w['other_account_id'] if field == 'account_id' else uuid4()
    wrong = PostgresFollowReelStore(w['pool'],scope=ReelMessageStoreScope(**values))
    assert await wrong.claim_next('wrong',NOW+timedelta(minutes=4)) is None
    url = 'https://www.instagram.com/reel/abc123/'
    assert not await wrong.prepare(claim,url,'Hello\n'+url,NOW)
    assert not await wrong.mark_attempting(claim,NOW)
    assert not await wrong.finish(claim,'deferred',{},NOW)
    assert sql(w,'SELECT * FROM follow_reel_messages') == before
