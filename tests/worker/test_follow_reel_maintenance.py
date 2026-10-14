import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engagemate.worker.follow_reel_maintenance import InterruptedReelResult, ReelMessageMaintenance
from engagemate.worker.follow_maintenance import SequencedWorker


def setup(outcome='sent', arm=True):
    claim = SimpleNamespace(id='ledger', user_id='user', ig_account_id='account')
    store = SimpleNamespace(claim_next=AsyncMock(return_value=claim), prepare=AsyncMock(return_value=True),
        mark_attempting=AsyncMock(return_value=True), finish=AsyncMock(return_value=True))
    async def perform(claim, prepare, before):
        assert await prepare('url', 'body')
        if arm:
            assert await before()
            assert not await before()
            assert not await prepare('other-url', 'other-body')
        return outcome, {}
    return store, ReelMessageMaintenance(store, perform, execution_mode='autonomous')


@pytest.mark.parametrize('mode', ['disabled', 'canary'])
async def test_only_autonomous_claims(mode):
    store, worker = setup()
    worker.execution_mode = mode
    assert await worker.run_once('w') is None
    store.claim_next.assert_not_called()


@pytest.mark.parametrize('outcome,arm,expected', [('sent', True, 'sent'),
    ('sent', False, 'uncertain'), ('deferred', True, 'uncertain'),
    ('deferred', False, 'deferred'), ('invalid', True, 'uncertain')])
async def test_outcomes_observe_attempt_boundary(outcome, arm, expected):
    store, worker = setup(outcome, arm)
    result = await worker.run_once('w')
    assert store.finish.call_args.args[1] == expected
    assert result.engaged == int(expected == 'sent')
    assert store.mark_attempting.await_count == int(arm)


@pytest.mark.parametrize('arm', [False, True])
@pytest.mark.parametrize('cancel', [False, True])
async def test_failure_and_cancel_preserve_possible_writes(arm, cancel):
    store, worker = setup()
    async def fail(claim, prepare, before):
        if arm:
            await before()
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError('private message must not enter logs')
    worker.performer = fail
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await worker.run_once('w')
    else:
        await worker.run_once('w')
    assert store.finish.call_args.args[1] == ('uncertain' if arm else 'deferred')
    assert 'private' not in str(store.finish.call_args)


async def test_lost_lease_not_reported_sent():
    store, worker = setup()
    store.finish.return_value = False
    result = await worker.run_once('w')
    assert result.engaged == 0
    assert result.stop_reason == 'follow_reel_lease_lost'


async def test_four_busy_lanes_are_fair():
    lanes = [SimpleNamespace(run_once=AsyncMock(return_value=i)) for i in range(4)]
    worker = SequencedWorker(lanes[1], lanes[0], lanes[2], reel_messages=lanes[3])
    assert [await worker.run_once('w') for _ in range(8)] == list(range(4))*2


async def test_empty_queue_does_not_execute():
    store, worker = setup()
    store.claim_next.return_value = None
    worker.performer = AsyncMock()
    assert await worker.run_once('w') is None
    worker.performer.assert_not_called()


async def test_refused_preparation_is_exposed_to_performer():
    store, worker = setup()
    store.prepare.return_value = False
    async def perform(claim, prepare, before):
        assert not await prepare('url', 'body')
        return 'deferred', {'reason': 'preparation_refused'}
    worker.performer = perform
    result = await worker.run_once('w')
    store.mark_attempting.assert_not_called()
    assert result.skipped == 1


async def test_denied_attempt_does_not_confirm_a_send():
    store, worker = setup()
    store.mark_attempting.return_value = False
    async def perform(claim, prepare, before):
        assert not await before()
        return 'sent', {}
    worker.performer = perform
    result = await worker.run_once('w')
    assert result.engaged == 0
    assert store.finish.call_args.args[1] == 'uncertain'


@pytest.mark.parametrize('arm', [False, True])
async def test_timeout_preserves_possible_write(arm):
    from contextlib import asynccontextmanager
    from unittest.mock import patch
    store, worker = setup()
    @asynccontextmanager
    async def timeout(seconds):
        assert seconds == 150
        yield
        raise TimeoutError
    with patch('engagemate.worker.follow_reel_maintenance.asyncio.timeout', timeout):
        store, worker = setup('sent', arm)
        await worker.run_once('w')
    assert store.finish.call_args.args[1] == ('uncertain' if arm else 'deferred')


@pytest.mark.parametrize('outcome,arm,expected', [('sent', False, 'uncertain'),
    ('deferred', True, 'uncertain'), ('invalid', True, 'uncertain')])
async def test_retained_result_cannot_bypass_attempt_guard_or_replace_cancel(outcome, arm, expected):
    store, worker = setup()
    async def interrupted(claim, prepare, before):
        if arm:
            await before()
        error = asyncio.CancelledError()
        error._engagemate_reel_result = InterruptedReelResult(outcome, {'item_id': '123'})
        raise error
    worker.performer = interrupted
    with pytest.raises(asyncio.CancelledError):
        await worker.run_once('w')
    assert store.finish.call_args.args[1] == expected
    assert store.mark_attempting.await_count == int(arm)


@pytest.mark.parametrize('outcome,evidence', [('invalid', {'private': 'detail'}),
    (['sent'], {}), ('sent', ['private detail'])])
@pytest.mark.parametrize('arm', [False, True])
@pytest.mark.parametrize('timed_out', [False, True])
async def test_malformed_retained_results_use_unknown_fallback(monkeypatch, outcome, evidence, arm, timed_out):
    store, worker = setup()
    real_timeout = asyncio.timeout
    deadline = real_timeout(None)
    monkeypatch.setattr(asyncio, 'timeout',
        lambda seconds: deadline if seconds == 150 else real_timeout(seconds))
    async def interrupted(claim, prepare, before):
        if arm:
            await before()
        try:
            if timed_out:
                deadline.reschedule(asyncio.get_running_loop().time())
                await asyncio.sleep(10)
            raise asyncio.CancelledError()
        except asyncio.CancelledError as error:
            error._engagemate_reel_result = InterruptedReelResult(outcome, evidence)
            raise
    worker.performer = interrupted
    if timed_out:
        summary = await worker.run_once('w')
        assert summary.engaged == 0 and deadline.expired()
    else:
        with pytest.raises(asyncio.CancelledError):
            await worker.run_once('w')
    store.finish.assert_awaited_once()
    assert store.finish.call_args.args[1:3] == (
        'uncertain' if arm else 'deferred', {
            'reason': 'reel_message_execution_error' if timed_out else 'worker_cancelled',
            'attempted': arm, 'invalid_retained_result': True})
