"""An exact unfollow scope cannot borrow another row or replay an old attempt."""

import asyncio
from dataclasses import FrozenInstanceError
from datetime import timedelta
from uuid import uuid4

import pytest

from engagemate.worker.follow_store import PostgresFollowStore, UnfollowStoreScope
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401
from tests.worker.test_follow_store import NOW, following, ready, sql  # noqa: F401
from tests.worker.test_scoped_follow_reel_stores import add_relationship


def scoped(w, relationship_id, **changes):
    return PostgresFollowStore(w['pool'], scope=UnfollowStoreScope(**{
        'user_id': w['user_id'], 'account_id': w['account_id'],
        'relationship_follow_id': relationship_id, **changes,
    }))


def test_unfollow_scope_is_immutable_and_uuid_only():
    values = (uuid4(), uuid4(), uuid4())
    scope = UnfollowStoreScope(*values)
    with pytest.raises(FrozenInstanceError):
        scope.relationship_follow_id = uuid4()
    for position in range(3):
        invalid = list(values)
        invalid[position] = str(invalid[position])
        with pytest.raises(ValueError):
            UnfollowStoreScope(*invalid)


async def test_unfollow_scope_enqueue_does_not_borrow_a_connection():
    store = PostgresFollowStore(None, scope=UnfollowStoreScope(uuid4(), uuid4(), uuid4()))
    assert await store.enqueue_confirmed(NOW) == 0


async def test_exact_due_unfollow_preserves_other_pending_due_and_expired_rows(ready):
    w = ready
    follow = await following(w)
    follow_attempted_at = sql(w, 'SELECT follow_attempted_at FROM relationship_follows WHERE id=%s', (follow.id,))[0][0]
    assert follow_attempted_at >= NOW
    due = NOW + timedelta(hours=120)
    others = [add_relationship(w, status=status, expired=expired)[0] for status, expired in [
        ('follow_pending', False), ('following', False), ('follow_running', True),
        ('unfollow_running', True), ('uncertain', False),
    ]]
    before = sql(w, 'SELECT * FROM relationship_follows WHERE id=ANY(%s) ORDER BY id', (others,))
    store = scoped(w, follow.id)
    assert await store.enqueue_confirmed(due) == 0
    assert await store.claim_next('early', due-timedelta(microseconds=1)) is None
    claim = await store.claim_next('exact', due)
    assert claim.id == follow.id and claim.action == 'unfollow'
    assert claim.followed_at == NOW
    assert sql(w, 'SELECT attempted_at,follow_attempted_at,unfollow_attempted_at FROM relationship_follows WHERE id=%s',
               (claim.id,)) == [(None, follow_attempted_at, None)]
    assert await store.mark_attempting(claim, due)
    assert not await store.mark_attempting(claim, due)
    assert await store.finish(claim, 'confirmed', due, {'receipt_matches': True, 'fresh_profile_matches': True})
    assert sql(w, 'SELECT status,unfollowed_at,unfollow_due_at FROM relationship_follows WHERE id=%s',
               (claim.id,)) == [('unfollowed', due, due)]
    assert sql(w, 'SELECT * FROM relationship_follows WHERE id=ANY(%s) ORDER BY id', (others,)) == before
    assert sql(w, 'SELECT count(*) FROM engagement_usage_events') == [(0,)]
    assert sql(w, 'SELECT count(*) FROM follow_reel_messages') == [(0,)]


@pytest.mark.parametrize('mutation', [
    "status='uncertain'", "status='follow_pending'", "status='skipped'", "status='unfollowed'",
    "unfollow_attempted_at=confirmed_follow_at",
    "unfollowed_at=confirmed_follow_at",
    "confirmed_follow_at=confirmed_follow_at+interval '1 day',unfollow_due_at=unfollow_due_at+interval '1 day'",
])
async def test_unfollow_scope_does_not_claim_or_change_ineligible_rows(ready, mutation):
    follow = await following(ready)
    sql(ready, 'UPDATE relationship_follows SET '+mutation+' WHERE id=%s', (follow.id,))
    before = sql(ready, 'SELECT * FROM relationship_follows')
    assert await scoped(ready, follow.id).claim_next('check', NOW+timedelta(hours=120)) is None
    assert sql(ready, 'SELECT * FROM relationship_follows') == before


async def test_database_refuses_unowned_following_state(ready):
    from psycopg.errors import CheckViolation
    follow = await following(ready)
    before = sql(ready, 'SELECT * FROM relationship_follows')
    with pytest.raises(CheckViolation):
        sql(ready, 'UPDATE relationship_follows SET confirmed_follow_at=NULL,unfollow_due_at=NULL WHERE id=%s', (follow.id,))
    assert sql(ready, 'SELECT * FROM relationship_follows') == before


@pytest.mark.parametrize('field', ['user_id', 'account_id', 'relationship_follow_id'])
async def test_unfollow_wrong_scope_cannot_claim_attempt_finish_or_expire(ready, field):
    follow = await following(ready)
    due = NOW + timedelta(hours=120)
    own = scoped(ready, follow.id)
    claim = await own.claim_next('own', due)
    wrong = scoped(ready, follow.id, **{field: uuid4()})
    before = sql(ready, 'SELECT * FROM relationship_follows')
    assert await wrong.enqueue_confirmed(due) == 0
    assert not await wrong.mark_attempting(claim, due)
    assert not await wrong.finish(claim, 'deferred', due, {})
    assert await wrong.claim_next('wrong', due+timedelta(minutes=3)) is None
    assert sql(ready, 'SELECT * FROM relationship_follows') == before


async def test_unfollow_scope_cannot_finish_a_follow_claim(ready):
    await ready['store'].enqueue_confirmed(NOW)
    follow = await ready['store'].claim_next('follow', NOW)
    store = scoped(ready, follow.id)
    before = sql(ready, 'SELECT * FROM relationship_follows')
    assert not await store.mark_attempting(follow, NOW)
    assert not await store.finish(follow, 'deferred', NOW, {})
    assert sql(ready, 'SELECT * FROM relationship_follows') == before


@pytest.mark.parametrize('attempted', [False, True])
async def test_exact_unfollow_concurrency_and_expiry_hold_uncertainty(ready, attempted):
    follow = await following(ready)
    due = NOW+timedelta(hours=120)
    store = scoped(ready, follow.id)
    claims = await asyncio.gather(*(store.claim_next(worker, due) for worker in ('one', 'two')))
    assert sum(claim is not None for claim in claims) == 1
    claim = next(claim for claim in claims if claim)
    if attempted:
        assert await store.mark_attempting(claim, due)
    assert await store.claim_next('expired', due+timedelta(minutes=3)) is None
    assert sql(ready, 'SELECT status FROM relationship_follows') == [('uncertain',)]
    assert not await store.finish(claim, 'confirmed', due+timedelta(minutes=3), {})


@pytest.mark.parametrize('mutation', [
    'UPDATE agent_configs SET auto_unfollow_enabled=false',
    "UPDATE proxies SET health_status='degraded'",
    'UPDATE ig_accounts SET paused_at=now()',
    "INSERT INTO instagram_execution_controls(scope,action,reason_code) VALUES ('action','unfollow','test')",
])
async def test_exact_unfollow_rechecks_ordinary_policy_before_attempt(ready, mutation):
    follow = await following(ready)
    due = NOW+timedelta(hours=120)
    store = scoped(ready, follow.id)
    claim = await store.claim_next('check', due)
    sql(ready, mutation)
    assert not await store.mark_attempting(claim, due)
    assert await store.finish(claim, 'deferred', due, {})
    assert sql(ready, 'SELECT status,unfollow_attempted_at FROM relationship_follows') == [('following', None)]


async def test_unfollow_attempt_deferred_result_stays_held(ready):
    follow = await following(ready)
    due = NOW+timedelta(hours=120)
    store = scoped(ready, follow.id)
    claim = await store.claim_next('check', due)
    assert await store.mark_attempting(claim, due)
    assert await store.finish(claim, 'deferred', due, {})
    assert await store.claim_next('again', due+timedelta(hours=1)) is None
    assert sql(ready, 'SELECT status,unfollow_attempted_at FROM relationship_follows') == [('uncertain', due)]


async def test_unfollow_scope_retains_daily_twenty_cap(ready):
    follow = await following(ready)
    due = NOW+timedelta(hours=120)
    for _ in range(20):
        row = add_relationship(ready, status='unfollowed')[0]
        sql(ready, 'UPDATE relationship_follows SET unfollow_reserved_at=%s WHERE id=%s', (due, row))
    before = sql(ready, 'SELECT * FROM relationship_follows ORDER BY id')
    assert await scoped(ready, follow.id).claim_next('capped', due) is None
    assert sql(ready, 'SELECT * FROM relationship_follows ORDER BY id') == before
