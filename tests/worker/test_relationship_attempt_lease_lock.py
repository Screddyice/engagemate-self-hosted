"""Actual row/config lock waits cannot authorize an expired social attempt."""

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from engagemate.worker.follow_reel_store import PostgresFollowReelStore
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401
from tests.worker import test_follow_store as follow_fixture
from tests.worker.test_follow_store import ready, sql  # noqa: F401
from tests.worker.test_follow_reel_store import enabled  # noqa: F401


async def subject(w, kind):
    now = datetime.now(UTC)
    store = w['store']
    assert await store.enqueue_confirmed(now) == 1
    follow = await store.claim_next('lease-fixture', now)
    assert follow is not None
    if kind == 'follow':
        return store, follow, 'relationship_follows'
    assert await store.mark_attempting(follow, datetime.now(UTC))
    assert await store.finish(follow, 'confirmed', datetime.now(UTC), {})
    if kind == 'unfollow':
        followed_at = datetime.now(UTC)-timedelta(hours=121)
        sql(w,"UPDATE relationship_follows SET confirmed_follow_at=%s,unfollow_due_at=%s WHERE id=%s",(followed_at,followed_at+timedelta(hours=120),follow.id))
        unfollow = await store.claim_next('lease-fixture', datetime.now(UTC))
        assert unfollow is not None and unfollow.action == 'unfollow'
        return store, unfollow, 'relationship_follows'
    store = PostgresFollowReelStore(w['pool'])
    message = await store.claim_next('lease-fixture', datetime.now(UTC))
    assert message is not None
    url = 'https://www.instagram.com/reel/fixture/'
    assert await store.prepare(message, url, 'Hello\n'+url, datetime.now(UTC))
    return store, message, 'follow_reel_messages'


async def observe_blocked(w, holder):
    async with asyncio.timeout(3):
        while True:
            with psycopg.connect(w['pool'].conninfo) as inspect:
                blocked = inspect.execute('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() AND %s=ANY(pg_blocking_pids(pid)))',(holder,)).fetchone()[0]
            if blocked:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize('kind',['follow','unfollow','reel'])
@pytest.mark.parametrize('wait_at',['row','policy'])
async def test_expiry_during_actual_lock_wait_leaves_attempt_unmarked(enabled,kind,wait_at):
    w = enabled
    assert w['pool'].conninfo.startswith('postgresql:///engagemate_canary_')
    store, claim, table = await subject(w,kind)
    sql(w,f"UPDATE {table} SET lease_expires_at=clock_timestamp()+interval '500 milliseconds' WHERE id=%s",(claim.id,))
    with w['pool'].connection() as locked, locked.transaction():
        holder = locked.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        if wait_at == 'row':
            locked.execute(f'SELECT id FROM {table} WHERE id=%s FOR UPDATE',(claim.id,))
        else:
            locked.execute('SELECT id FROM agent_configs WHERE ig_account_id=%s FOR UPDATE',(w['account_id'],))
        arming = asyncio.create_task(store.mark_attempting(claim,datetime.now(UTC)))
        try:
            await observe_blocked(w,holder)
            async with asyncio.timeout(3):
                while True:
                    with psycopg.connect(w['pool'].conninfo) as inspect:
                        expired = inspect.execute(f'SELECT lease_expires_at<clock_timestamp() FROM {table} WHERE id=%s',(claim.id,)).fetchone()[0]
                    if expired:
                        break
                    await asyncio.sleep(0.01)
            assert not arming.done()
        except BaseException:
            arming.cancel()
            raise
    assert not await asyncio.wait_for(arming,3)
    assert sql(w,f'SELECT attempted_at FROM {table} WHERE id=%s',(claim.id,)) == [(None,)]
    if kind != 'reel':
        assert sql(w,f'SELECT {kind}_attempted_at FROM {table} WHERE id=%s',(claim.id,)) == [(None,)]


@pytest.mark.parametrize('kind',['follow','unfollow','reel'])
async def test_old_caller_clock_cannot_revive_expired_lease(enabled,kind):
    store, claim, table = await subject(enabled,kind)
    sql(enabled,f"UPDATE {table} SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(claim.id,))
    assert not await store.mark_attempting(claim,datetime.now(UTC)-timedelta(minutes=1))
    assert sql(enabled,f'SELECT attempted_at FROM {table} WHERE id=%s',(claim.id,)) == [(None,)]


@pytest.mark.parametrize('kind',['follow','unfollow','reel'])
async def test_live_attempt_and_saved_receipt_survive_account_pause(enabled,kind):
    store, claim, table = await subject(enabled,kind)
    now = datetime.now(UTC)
    assert await store.mark_attempting(claim,now)
    assert not await store.mark_attempting(claim,now)
    sql(enabled,"UPDATE ig_accounts SET health_status='paused' WHERE id=%s",(enabled['account_id'],))
    if kind == 'reel':
        receipt = dict(server_confirmed=True,recipient_id=claim.recipient_id,sender_id=claim.sender_id,item_id='123',thread_id='456')
        assert await store.finish(claim,'sent',receipt,now)
    else:
        assert await store.finish(claim,'confirmed',now,{})
    assert sql(enabled,f'SELECT attempted_at IS NOT NULL FROM {table} WHERE id=%s',(claim.id,)) == [(True,)]


@pytest.mark.parametrize('kind',['follow','unfollow','reel'])
@pytest.mark.parametrize('wait_at',['row','policy'])
async def test_attempt_timestamp_follows_actual_lock_wait(enabled,kind,wait_at):
    w = enabled
    store, claim, table = await subject(w,kind)
    with w['pool'].connection() as locked, locked.transaction():
        holder = locked.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        if wait_at == 'row':
            locked.execute(f'SELECT id FROM {table} WHERE id=%s FOR UPDATE',(claim.id,))
        else:
            locked.execute('SELECT id FROM agent_configs WHERE ig_account_id=%s FOR UPDATE',(w['account_id'],))
        arming = asyncio.create_task(store.mark_attempting(claim,datetime.now(UTC)))
        try:
            await observe_blocked(w,holder)
            released_at = locked.execute('SELECT clock_timestamp() AS now').fetchone()['now']
        except BaseException:
            arming.cancel()
            raise
    assert await asyncio.wait_for(arming,3)
    assert sql(w,f'SELECT attempted_at>=%s AND updated_at=attempted_at FROM {table} WHERE id=%s',(released_at,claim.id)) == [(True,)]


async def test_relationship_fixture_refreshes_aged_import_clock(world,monkeypatch):
    follow_fixture.pin_relationship_clock(monkeypatch,datetime.now(UTC)-timedelta(minutes=3))
    before = datetime.now(UTC)
    w = ready.__wrapped__(world,monkeypatch)
    assert follow_fixture.NOW >= before
    claim = await follow_fixture.following(w)
    assert sql(w,'SELECT follow_attempted_at>=%s FROM relationship_follows WHERE id=%s',(before,claim.id)) == [(True,)]


async def test_reel_ttl_expiring_during_policy_lock_preserves_unused_attempt(enabled):
    w = enabled
    store, claim, table = await subject(w,'reel')
    sql(w,"UPDATE follow_reel_messages SET created_at=clock_timestamp()-interval '24 hours'+interval '500 milliseconds' WHERE id=%s",(claim.id,))
    with w['pool'].connection() as locked, locked.transaction():
        holder = locked.execute('SELECT pg_backend_pid() AS pid').fetchone()['pid']
        locked.execute('SELECT id FROM agent_configs WHERE ig_account_id=%s FOR UPDATE',(w['account_id'],))
        arming = asyncio.create_task(store.mark_attempting(claim,datetime.now(UTC)))
        try:
            await observe_blocked(w,holder)
            async with asyncio.timeout(3):
                while True:
                    with psycopg.connect(w['pool'].conninfo) as inspect:
                        expired = inspect.execute("SELECT created_at<clock_timestamp()-interval '24 hours' AND lease_expires_at>clock_timestamp() FROM follow_reel_messages WHERE id=%s",(claim.id,)).fetchone()[0]
                    if expired:
                        break
                    await asyncio.sleep(0.01)
            assert not arming.done()
        except BaseException:
            arming.cancel()
            raise
    assert not await asyncio.wait_for(arming,3)
    assert sql(w,f'SELECT attempted_at FROM {table} WHERE id=%s',(claim.id,)) == [(None,)]
