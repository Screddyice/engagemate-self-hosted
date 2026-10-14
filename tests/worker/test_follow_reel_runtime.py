from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from engagemate.onboarding.profile_capture import ProfileCaptureError
from engagemate.worker import follow_reel_runtime as module
from engagemate.worker.follow_reel_maintenance import ReelMessageMaintenance
from engagemate.worker.follow_reels import Reel, compose_message


def claim():
    return SimpleNamespace(user_id='user', ig_account_id='account', sender_id='123', sender_handle='sender',
        recipient_id='456', handle='recipient', reel_url=None, body=None, rotation_slot=1, message_template='Hello!')


def setup(monkeypatch):
    worker = module.LiveFollowReelPerformer(None, None, None)
    worker._sender = Mock(return_value=('123', 'sender'))
    worker._pause = Mock()
    monkeypatch.setattr(module.ProfileCaptureService, '_load', Mock(return_value='bound'))
    visits = []
    @asynccontextmanager
    async def opened(bound):
        assert bound == 'bound'
        visits.append('open')
        try:
            yield SimpleNamespace(context='context')
        finally:
            visits.append('close')
    monkeypatch.setattr(module, 'LeasedBrowserFactory', lambda *args: SimpleNamespace(open=opened))
    adapter = SimpleNamespace(verify_accounts=AsyncMock(),
        read_reels=AsyncMock(return_value=[Reel(f'reel{i}', datetime.now(UTC)-timedelta(days=i+1), '123') for i in range(3)]),
        send=AsyncMock(return_value=('sent', {'server_confirmed': True})))
    monkeypatch.setattr(module, 'FollowReelBrowser', lambda context: adapter)
    return worker, adapter, visits


async def test_reads_prepares_sends_under_one_lease(monkeypatch):
    worker, adapter, visits = setup(monkeypatch)
    prepare, before = AsyncMock(return_value=True), AsyncMock(return_value=True)
    result = await worker(claim(), prepare, before)
    assert result[0] == 'sent'
    assert visits == ['open', 'close']
    adapter.verify_accounts.assert_awaited_once_with('123', 'sender', '456', 'recipient')
    prepare.assert_awaited_once_with('https://www.instagram.com/reel/reel1/', 'Hello!\nhttps://www.instagram.com/reel/reel1/')
    assert adapter.send.call_args.kwargs['before_mutation'] is before


async def test_retry_reuses_frozen_payload_without_reading_latest_reels(monkeypatch):
    worker, adapter, visits = setup(monkeypatch)
    job = claim()
    job.reel_url = 'https://www.instagram.com/reel/abcde/'
    job.body = compose_message(job.reel_url, 'Original')
    prepare = AsyncMock(return_value=True)
    await worker(job, prepare, AsyncMock())
    adapter.read_reels.assert_not_called()
    prepare.assert_awaited_once_with(job.reel_url, job.body)
    assert adapter.send.call_args.kwargs['body'] == job.body
    assert visits == ['open', 'close']


@pytest.mark.parametrize('reason', ['reconnect_required', 'proxy_unavailable'])
async def test_bad_profile_never_opens_and_reconnect_pauses(monkeypatch, reason):
    worker, adapter, visits = setup(monkeypatch)
    monkeypatch.setattr(module.ProfileCaptureService, '_load', Mock(side_effect=ProfileCaptureError(reason)))
    result = await worker(claim(), AsyncMock(), AsyncMock())
    assert result[0] == 'deferred' and visits == []
    assert worker._pause.call_count == int(reason == 'reconnect_required')


async def test_wrong_sender_never_opens(monkeypatch):
    worker, adapter, visits = setup(monkeypatch)
    worker._sender.return_value = ('999', 'wrong')
    assert (await worker(claim(), AsyncMock(), AsyncMock()))[1]['reason'] == 'sender_unavailable'
    assert visits == []


@pytest.mark.parametrize('during_send', [False, True])
async def test_rate_limit_pauses_and_preserves_uncertainty(monkeypatch, during_send):
    worker, adapter, visits = setup(monkeypatch)
    job = claim()
    if during_send:
        adapter.send.return_value = ('uncertain', {'health_signal': 'rate_limited'})
    else:
        adapter.verify_accounts.side_effect = module.ReelBrowserError('instagram_rate_limited', 'rate_limited')
    result = await worker(job, AsyncMock(return_value=True), AsyncMock())
    assert result[0] == ('uncertain' if during_send else 'deferred')
    worker._pause.assert_called_once_with(job, 'rate_limited')
    assert visits == ['open', 'close']


@pytest.mark.parametrize('signal', ['authentication_required', 'checkpoint_required', 'rate_limited', 'identity_mismatch'])
async def test_deferred_session_preflight_pauses_without_arming(monkeypatch, signal):
    worker, adapter, visits = setup(monkeypatch)
    job = claim()
    evidence = {'phase': 'session_preflight', 'mutation_attempted': False,
                'server_confirmed': False, 'health_signal': signal}
    adapter.send.return_value = ('deferred', evidence)
    before = AsyncMock(return_value=True)
    assert await worker(job, AsyncMock(return_value=True), before) == ('deferred', evidence)
    worker._pause.assert_called_once_with(job, signal)
    before.assert_not_awaited()
    assert visits == ['open', 'close']


async def test_prepare_refusal_never_sends(monkeypatch):
    worker, adapter, visits = setup(monkeypatch)
    result = await worker(claim(), AsyncMock(return_value=False), AsyncMock())
    assert result[1]['reason'] == 'message_preparation_refused'
    adapter.send.assert_not_called()


async def test_less_than_three_reels_is_actionable(monkeypatch):
    worker, adapter, visits = setup(monkeypatch)
    adapter.read_reels.return_value = []
    result = await worker(claim(), AsyncMock(), AsyncMock())
    assert result[1]['reason'] == 'three_sender_reels_required'
    adapter.send.assert_not_called()


@pytest.mark.parametrize('cleanup', ['adapter', 'browser', 'both'])
async def test_confirmed_receipt_survives_cleanup_failure(monkeypatch, cleanup):
    worker, adapter, _ = setup(monkeypatch)
    receipt = {'server_confirmed': True, 'phase': 'receipt_readback', 'item_id': '789'}
    adapter.send.return_value = ('sent', receipt)
    if cleanup in {'adapter', 'both'}:
        adapter.close = AsyncMock(side_effect=RuntimeError('private cookie detail'))
    if cleanup in {'browser', 'both'}:
        @asynccontextmanager
        async def opened(bound):
            yield SimpleNamespace(context='context')
            raise RuntimeError('private proxy detail')
        monkeypatch.setattr(module, 'LeasedBrowserFactory', lambda *args: SimpleNamespace(open=opened))
    outcome, evidence = await worker(claim(), AsyncMock(return_value=True), AsyncMock())
    assert outcome == 'sent'
    assert evidence == {**receipt,
        **({'adapter_cleanup_failed': True} if cleanup in {'adapter', 'both'} else {}),
        **({'browser_cleanup_failed': True} if cleanup in {'browser', 'both'} else {})}
    assert 'private' not in str(evidence)
    assert 'adapter_cleanup_failed' not in receipt


async def test_uncertain_phase_survives_pause_persistence_failure(monkeypatch):
    worker, adapter, _ = setup(monkeypatch)
    receipt = {'phase': 'send_request', 'http_status': 302, 'mutation_attempted': True,
               'reason': 'instagram_login_required', 'health_signal': 'authentication_required'}
    adapter.send.return_value = ('uncertain', receipt)
    worker._pause.side_effect = RuntimeError('private database detail')
    outcome, evidence = await worker(claim(), AsyncMock(return_value=True), AsyncMock())
    assert outcome == 'uncertain'
    assert evidence == {**receipt, 'pause_persistence_failed': True}
    assert 'private' not in str(evidence)


@pytest.mark.parametrize('stage', ['send', 'pause', 'adapter', 'browser'])
async def test_cancellation_is_not_converted_to_result(monkeypatch, stage):
    worker, adapter, _ = setup(monkeypatch)
    if stage == 'send':
        adapter.send.side_effect = module.asyncio.CancelledError()
        adapter.close = AsyncMock(side_effect=RuntimeError('cleanup also failed'))
    elif stage == 'pause':
        adapter.send.return_value = ('uncertain', {'health_signal': 'authentication_required'})
        worker._pause.side_effect = module.asyncio.CancelledError()
    elif stage == 'adapter':
        adapter.close = AsyncMock(side_effect=module.asyncio.CancelledError())
    else:
        @asynccontextmanager
        async def opened(bound):
            yield SimpleNamespace(context='context')
            raise module.asyncio.CancelledError()
        monkeypatch.setattr(module, 'LeasedBrowserFactory', lambda *args: SimpleNamespace(open=opened))
    with pytest.raises(module.asyncio.CancelledError):
        await worker(claim(), AsyncMock(return_value=True), AsyncMock())


@pytest.mark.parametrize('stage', ['send', 'adapter'])
async def test_context_cleanup_cannot_mask_cancellation(monkeypatch, stage):
    worker, adapter, _ = setup(monkeypatch)
    if stage == 'send':
        adapter.send.side_effect = module.asyncio.CancelledError()
    else:
        adapter.close = AsyncMock(side_effect=module.asyncio.CancelledError())
    @asynccontextmanager
    async def opened(bound):
        try:
            yield SimpleNamespace(context='context')
        finally:
            raise RuntimeError('context cleanup failure')
    monkeypatch.setattr(module, 'LeasedBrowserFactory', lambda *args: SimpleNamespace(open=opened))
    with pytest.raises(module.asyncio.CancelledError):
        await worker(claim(), AsyncMock(return_value=True), AsyncMock())


def maintenance(performer):
    job = claim()
    job.id = 'message'
    store = SimpleNamespace(claim_next=AsyncMock(return_value=job),
        prepare=AsyncMock(return_value=True), mark_attempting=AsyncMock(return_value=True),
        finish=AsyncMock(return_value=True))
    return store, ReelMessageMaintenance(store, performer, execution_mode='autonomous')


def arm_receipt(adapter, outcome='sent', arm=True):
    receipt = {'server_confirmed': outcome == 'sent', 'phase': 'receipt_readback',
        'item_id': '789', 'thread_id': '321', 'sender_id': '123', 'recipient_id': '456'}
    async def send(**kwargs):
        if arm:
            assert await kwargs['before_mutation']()
        return outcome, receipt
    adapter.send.side_effect = send
    return receipt


@pytest.mark.parametrize('cleanup', ['adapter', 'browser', 'masked'])
@pytest.mark.parametrize('outcome', ['sent', 'uncertain'])
async def test_maintenance_persists_known_receipt_then_propagates_cancel(monkeypatch, cleanup, outcome):
    performer, adapter, _ = setup(monkeypatch)
    receipt = arm_receipt(adapter, outcome)
    if cleanup in {'adapter', 'masked'}:
        adapter.close = AsyncMock(side_effect=module.asyncio.CancelledError())
    if cleanup in {'browser', 'masked'}:
        @asynccontextmanager
        async def opened(bound):
            try:
                yield SimpleNamespace(context='context')
            finally:
                if cleanup == 'masked':
                    raise RuntimeError('private cleanup detail')
                raise module.asyncio.CancelledError()
        monkeypatch.setattr(module, 'LeasedBrowserFactory', lambda *args: SimpleNamespace(open=opened))
    store, worker = maintenance(performer)
    with pytest.raises(module.asyncio.CancelledError) as interrupted:
        await worker.run_once('w')
    assert type(interrupted.value) is module.asyncio.CancelledError
    store.finish.assert_awaited_once()
    assert store.finish.call_args.args[1:3] == (outcome, receipt)
    assert 'private' not in str(store.finish.call_args)
    assert 'worker_cancelled' not in receipt


@pytest.mark.parametrize('cleanup', ['adapter', 'browser'])
@pytest.mark.parametrize('accepted', [False, True])
async def test_real_timeout_after_receipt_retains_result_and_store_gate(monkeypatch, cleanup, accepted):
    performer, adapter, _ = setup(monkeypatch)
    receipt = arm_receipt(adapter)
    real_timeout = module.asyncio.timeout
    deadline = real_timeout(None)
    monkeypatch.setattr(module.asyncio, 'timeout',
        lambda seconds: deadline if seconds == 150 else real_timeout(seconds))
    async def timed_close():
        deadline.reschedule(module.asyncio.get_running_loop().time())
        await module.asyncio.sleep(10)
    if cleanup == 'adapter':
        adapter.close = timed_close
    else:
        @asynccontextmanager
        async def opened(bound):
            try:
                yield SimpleNamespace(context='context')
            finally:
                await timed_close()
        monkeypatch.setattr(module, 'LeasedBrowserFactory', lambda *args: SimpleNamespace(open=opened))
    store, worker = maintenance(performer)
    store.finish.return_value = accepted
    summary = await worker.run_once('w')
    assert deadline.expired()
    store.finish.assert_awaited_once()
    assert store.finish.call_args.args[1:3] == ('sent', receipt)
    assert summary.engaged == int(accepted)
    assert summary.stop_reason == ('follow_reel_sent' if accepted else 'follow_reel_lease_lost')


@pytest.mark.parametrize('arm', [False, True])
async def test_cancel_before_receipt_retains_attempt_boundary(monkeypatch, arm):
    performer, adapter, _ = setup(monkeypatch)
    async def send(**kwargs):
        if arm:
            await kwargs['before_mutation']()
        raise module.asyncio.CancelledError()
    adapter.send.side_effect = send
    store, worker = maintenance(performer)
    with pytest.raises(module.asyncio.CancelledError):
        await worker.run_once('w')
    assert store.finish.call_args.args[1:3] == (
        'uncertain' if arm else 'deferred', {'reason': 'worker_cancelled', 'attempted': arm})


async def test_receipt_is_durable_before_cleanup_outlives_claim(monkeypatch):
    performer, adapter, visits = setup(monkeypatch)
    receipt = arm_receipt(adapter)
    store, worker = maintenance(performer)
    started = datetime.now(UTC)
    current = started
    worker.clock = lambda: current
    async def finish(*args):
        assert visits == ['open'] and args[3] == started
        return True
    store.finish.side_effect = finish
    async def slow_close():
        nonlocal current
        store.finish.assert_awaited_once()
        current = started + timedelta(seconds=181)
    adapter.close = slow_close
    summary = await worker.run_once('w')
    assert summary.engaged == 1 and current > started + timedelta(seconds=180)
    assert store.finish.call_args.args[1:3] == ('sent', receipt)
    store.finish.assert_awaited_once()


async def test_cancel_during_immediate_finish_shields_one_commit(monkeypatch):
    performer, adapter, _ = setup(monkeypatch)
    receipt = arm_receipt(adapter)
    store, worker = maintenance(performer)
    committing, committed = module.asyncio.Event(), module.asyncio.Event()
    async def finish(*args):
        committing.set()
        await committed.wait()
        return True
    store.finish.side_effect = finish
    task = module.asyncio.create_task(worker.run_once('w'))
    try:
        await module.asyncio.wait_for(committing.wait(), 1)
        task.cancel()
        await module.asyncio.sleep(0)
        assert not task.done()
        committed.set()
        with pytest.raises(module.asyncio.CancelledError):
            await task
        store.finish.assert_awaited_once()
        assert store.finish.call_args.args[1:3] == ('sent', receipt)
    finally:
        committed.set()
        if not task.done():
            task.cancel()
        await module.asyncio.gather(task, return_exceptions=True)


async def test_cleanup_flags_do_not_rewrite_already_persisted_receipt(monkeypatch):
    performer, adapter, _ = setup(monkeypatch)
    receipt = arm_receipt(adapter)
    adapter.close = AsyncMock(side_effect=RuntimeError('private cleanup detail'))
    store, worker = maintenance(performer)
    summary = await worker.run_once('w')
    assert summary.engaged == 1
    store.finish.assert_awaited_once()
    assert store.finish.call_args.args[1:3] == ('sent', receipt)
    assert 'adapter_cleanup_failed' not in store.finish.call_args.args[2]
