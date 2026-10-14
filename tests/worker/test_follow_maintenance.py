import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from engagemate.worker.follow_maintenance import FollowMaintenance, InterruptedFollowResult, SequencedWorker


def setup(outcome='confirmed', arm=True):
    claim = SimpleNamespace(id='ledger', user_id='user', ig_account_id='account', action='follow')
    store = SimpleNamespace(enqueue_confirmed=AsyncMock(), claim_next=AsyncMock(return_value=claim),
                            mark_attempting=AsyncMock(return_value=True), finish=AsyncMock(return_value=True))

    async def perform(claim, before):
        if arm:
            assert await before()
            assert not await before()
        return outcome, {}

    return store, FollowMaintenance(store, perform, execution_mode='autonomous')


@pytest.mark.parametrize('mode', ['disabled', 'canary'])
async def test_non_autonomous_never_accesses_queue(mode):
    store, worker = setup()
    worker.execution_mode = mode
    assert await worker.run_once('worker') is None
    store.enqueue_confirmed.assert_not_called()


@pytest.mark.parametrize(('outcome', 'arm', 'expected'), [
    ('confirmed', True, 'confirmed'), ('confirmed', False, 'uncertain'),
    ('deferred', True, 'uncertain'), ('deferred', False, 'deferred'),
    ('already_following', False, 'already_following'),
])
async def test_attempt_boundary(outcome, arm, expected):
    store, worker = setup(outcome, arm)
    summary = await worker.run_once('worker')
    assert store.finish.call_args.args[1] == expected
    assert summary.engaged == int(expected == 'confirmed')
    assert store.mark_attempting.await_count == int(arm)


@pytest.mark.parametrize('arm', [False, True])
async def test_errors_never_replay_possible_write(arm):
    store, worker = setup()

    async def fail(claim, before):
        if arm:
            await before()
        raise RuntimeError('sensitive details must not enter evidence')

    worker.performer = fail
    await worker.run_once('worker')
    assert store.finish.call_args.args[1] == ('uncertain' if arm else 'deferred')
    assert 'sensitive' not in str(store.finish.call_args)


async def test_denied_attempt_cannot_confirm():
    store, worker = setup(arm=False)
    store.mark_attempting.return_value = False

    async def perform(claim, before):
        assert not await before()
        return 'confirmed', {}

    worker.performer = perform
    await worker.run_once('worker')
    assert store.finish.call_args.args[1] == 'uncertain'


async def test_lease_lost_not_reported_success():
    store, worker = setup()
    store.finish.return_value = False
    result = await worker.run_once('worker')
    assert result.engaged == 0
    assert result.stop_reason == 'follow_lease_lost'


@pytest.mark.parametrize('arm', [False, True])
async def test_cancel_persists_attempt_state(arm):
    store, worker = setup()

    async def cancel(claim, before):
        if arm:
            await before()
        raise asyncio.CancelledError

    worker.performer = cancel
    with pytest.raises(asyncio.CancelledError):
        await worker.run_once('worker')
    assert store.finish.call_args.args[1] == ('uncertain' if arm else 'deferred')


@pytest.mark.parametrize('arm', [False, True])
@pytest.mark.parametrize('outcome,evidence', [(['confirmed'], {}), ('confirmed', []), ('invalid', {})])
@pytest.mark.parametrize('timed_out', [False, True])
async def test_invalid_retained_result_keeps_unknown_attempt_state(monkeypatch, arm, outcome, evidence, timed_out):
    store, worker = setup()
    real_timeout = asyncio.timeout
    deadline = real_timeout(None)
    monkeypatch.setattr(asyncio, 'timeout',
        lambda seconds: deadline if seconds == 105 else real_timeout(seconds))

    async def interrupted(claim, before):
        if arm:
            await before()
        try:
            if timed_out:
                deadline.reschedule(asyncio.get_running_loop().time())
                await asyncio.sleep(10)
            raise asyncio.CancelledError()
        except asyncio.CancelledError as error:
            error._engagemate_follow_result = InterruptedFollowResult(outcome, evidence)
            raise

    worker.performer = interrupted
    if timed_out:
        result = await worker.run_once('worker')
        assert result.engaged == 0 and deadline.expired()
    else:
        with pytest.raises(asyncio.CancelledError):
            await worker.run_once('worker')
    store.finish.assert_awaited_once()
    assert store.finish.call_args.args[1] == ('uncertain' if arm else 'deferred')
    assert store.finish.call_args.args[3]['invalid_retained_result'] is True


@pytest.mark.parametrize('arm', [False, True])
async def test_immediate_confirmation_still_requires_attempt(arm):
    store, worker = setup()

    async def perform(claim, before, on_result):
        if arm:
            await before()
        await on_result('confirmed', {'receipt_matches': True})
        assert not await on_result('confirmed', {'different': True})
        return 'confirmed', {'receipt_matches': True, 'cleanup_failed': True}

    worker.performer = SimpleNamespace(perform_with_result=perform)
    result = await worker.run_once('worker')
    store.finish.assert_awaited_once()
    assert store.finish.call_args.args[1] == ('confirmed' if arm else 'uncertain')
    assert result.engaged == int(arm)
    assert 'cleanup_failed' not in store.finish.call_args.args[3]


async def test_alternates_busy_queues_and_drains_idle_one():
    stories = SimpleNamespace(run_once=AsyncMock(return_value='story'))
    relationships = SimpleNamespace(run_once=AsyncMock(return_value='relationship'))
    worker = SequencedWorker(stories, relationships)
    assert [await worker.run_once('w') for _ in range(4)] == [
        'relationship', 'story', 'relationship', 'story',
    ]
    relationships.run_once.return_value = None
    assert await worker.run_once('w') == 'story'
