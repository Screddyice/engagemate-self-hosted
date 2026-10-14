import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from engagemate.hosted_login.protocol import ConnectError, SignIn
from engagemate.hosted_login.service import LoginService


@pytest.fixture
def setup():
    owner = uuid4()
    reservation = SimpleNamespace(id=uuid4(), user_id=owner, account_id=uuid4())
    repo = Mock()
    repo.completed.return_value = None
    repo.reserve.return_value = reservation
    repo.complete.return_value = {'id': str(reservation.account_id), 'username': 'demo_user', 'health': 'active'}
    browser = SimpleNamespace(step=AsyncMock(return_value={'kind':'login','options':[]}), input=AsyncMock(),
                              capture=AsyncMock(return_value=SimpleNamespace(cookies=[])), close=AsyncMock())
    svc = LoginService(repo, AsyncMock(return_value=browser))
    return owner, reservation, repo, browser, svc


@pytest.mark.asyncio
async def test_other_tenant_cannot_read_control_finish_or_cancel(setup):
    owner, res, repo, browser, svc = setup
    await svc.start(owner, 'demo_user', 'UTC')
    stranger = uuid4()
    for operation in (
        svc.status(stranger, res.id), svc.step(stranger, res.id),
        svc.input(stranger, res.id, SignIn(type='sign_in', username='demo_user', password='fixture')),
        svc.complete(stranger, res.id), svc.cancel(stranger, res.id),
    ):
        with pytest.raises(ConnectError) as error:
            await operation
        assert error.value.status == 404
    browser.input.assert_not_called()
    repo.complete.assert_not_called()
    await svc.close()


@pytest.mark.asyncio
async def test_sign_in_only_accepts_selected_username(setup):
    owner, res, repo, browser, svc = setup
    await svc.start(owner, 'demo_user', 'UTC')
    assert (await svc.status(owner, res.id))['username'] == 'demo_user'
    with pytest.raises(ConnectError) as error:
        await svc.input(owner, res.id, SignIn(type='sign_in', username='other', password='fixture'))
    assert error.value.code == 'identity_mismatch'
    browser.input.assert_not_called()
    await svc.input(owner, res.id, SignIn(type='sign_in', username='demo_user', password='fixture'))
    browser.input.assert_awaited_once()
    await svc.close()


@pytest.mark.asyncio
async def test_cancelled_sign_in_waits_for_browser_and_keeps_session(setup):
    owner, res, repo, browser, svc = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_input(_command):
        entered.set()
        await release.wait()

    browser.input.side_effect = slow_input
    await svc.start(owner, 'demo_user', 'UTC')
    task = asyncio.create_task(svc.input(
        owner, res.id, SignIn(type='sign_in', username='demo_user', password='fixture'),
    ))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await svc.status(owner, res.id))['state'] == 'active'
    repo.release.assert_not_called()
    browser.close.assert_not_called()
    await svc.close()


@pytest.mark.asyncio
async def test_failed_sign_in_input_keeps_session_for_retry(setup):
    owner, res, repo, browser, svc = setup
    browser.input.side_effect = RuntimeError('transient browser failure')
    await svc.start(owner, 'demo_user', 'UTC')
    with pytest.raises(ConnectError) as error:
        await svc.input(
            owner, res.id, SignIn(type='sign_in', username='demo_user', password='fixture'),
        )
    assert error.value.code == 'connection_unavailable'
    assert (await svc.status(owner, res.id))['state'] == 'active'
    browser.close.assert_not_called()
    repo.release.assert_not_called()
    await svc.close()


@pytest.mark.asyncio
async def test_one_session_per_user_and_finish_retries_do_not_recapture(setup):
    owner, res, repo, browser, svc = setup
    first = await svc.start(owner, 'demo_user', 'UTC')
    assert await svc.start(owner, 'demo_user', 'UTC') == first
    repo.reserve.assert_called_once()
    result = await svc.complete(owner, res.id)
    assert result['state'] == 'connected'
    assert (await svc.complete(owner, res.id))['account'] == result['account']
    repo.complete.assert_called_once()
    browser.close.assert_awaited_once()
    assert 'cookie' not in str(result)
    await svc.close()


@pytest.mark.asyncio
async def test_expiry_closes_browser_and_releases_reservation(setup):
    owner, res, repo, browser, svc = setup
    svc.ttl = 0.02
    await svc.start(owner, 'demo_user', 'UTC')
    await asyncio.sleep(0.05)
    assert (await svc.status(owner, res.id))['code'] == 'session_expired'
    browser.close.assert_awaited_once()
    repo.release.assert_called_with(res.id, owner)


@pytest.mark.asyncio
async def test_browser_failure_releases_slot_and_reservation(setup):
    owner, res, repo, browser, svc = setup
    svc.open_browser.side_effect = RuntimeError('proxy-secret')
    with pytest.raises(RuntimeError):
        await svc.start(owner, 'demo_user', 'UTC')
    assert svc.sessions[res.id].state == 'error'
    repo.release.assert_called_once_with(res.id, owner)


@pytest.mark.asyncio
async def test_cancelled_start_cleans_browser(setup):
    owner, res, repo, browser, svc = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(_):
        entered.set()
        await release.wait()
        return browser
    svc.open_browser = slow
    task = asyncio.create_task(svc.start(owner, 'demo_user', 'UTC'))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    browser.close.assert_awaited_once()
    repo.release.assert_called_once()


@pytest.mark.asyncio
async def test_identity_mismatch_leaves_browser_for_user_to_correct(setup):
    owner, res, repo, browser, svc = setup
    browser.capture.side_effect = ConnectError('identity_mismatch', 422)
    await svc.start(owner, 'demo_user', 'UTC')
    with pytest.raises(ConnectError):
        await svc.complete(owner, res.id)
    assert (await svc.status(owner, res.id))['state'] == 'active'
    repo.complete.assert_not_called()
    await svc.close()


@pytest.mark.asyncio
async def test_failed_browser_close_quarantines_service_and_keeps_reservation(setup):
    owner, res, repo, browser, svc = setup
    browser.close.side_effect = RuntimeError('private process error')
    await svc.start(owner, 'demo_user', 'UTC')
    await svc.cancel(owner, res.id)
    repo.release.assert_not_called()
    assert svc.cleanup_failed
    with pytest.raises(ConnectError):
        await svc.start(owner, 'demo_user', 'UTC')
    browser.close.side_effect = None
    await svc.close()


@pytest.mark.asyncio
async def test_repeated_disconnect_during_commit_keeps_input_and_ownership(setup):
    import threading
    owner, res, repo, browser, svc = setup
    entered, release = threading.Event(), threading.Event()
    capture = SimpleNamespace(cookies=['fixture-secret'])
    browser.capture.return_value = capture
    def commit(*args):
        entered.set()
        assert release.wait(3)
        assert capture.cookies == ['fixture-secret']
        return {'id':str(res.account_id),'username':'demo_user','health':'active'}
    repo.complete.side_effect = commit
    await svc.start(owner, 'demo_user', 'UTC')
    task = asyncio.create_task(svc.complete(owner, res.id))
    await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    assert capture.cookies == ['fixture-secret']
    repo.release.assert_not_called()
    browser.close.assert_awaited_once()
    release.set()
    assert (await task)['state'] == 'connected'
    assert capture.cookies == []
    await svc.close()


@pytest.mark.asyncio
async def test_shutdown_waits_for_start_and_rejects_new_starts(setup):
    owner, res, repo, browser, svc = setup
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(_):
        entered.set()
        await release.wait()
        return browser
    svc.open_browser = slow
    start = asyncio.create_task(svc.start(owner, 'demo_user', 'UTC'))
    await entered.wait()
    shutdown = asyncio.create_task(svc.close())
    await asyncio.sleep(0)
    repo.release.assert_not_called()
    release.set()
    await start
    await shutdown
    browser.close.assert_awaited_once()
    repo.release.assert_called_once()
    with pytest.raises(ConnectError):
        await svc.start(owner, 'demo_user', 'UTC')


@pytest.mark.asyncio
async def test_closing_browser_still_counts_against_capacity(setup):
    owner, res, repo, browser, svc = setup
    svc.max_sessions = 1
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_close():
        entered.set()
        await release.wait()
    browser.close.side_effect = slow_close
    await svc.start(owner, 'demo_user', 'UTC')
    task = asyncio.create_task(svc.cancel(owner, res.id))
    await entered.wait()
    with pytest.raises(ConnectError) as error:
        await svc.start(uuid4(), 'other', 'UTC')
    assert error.value.code == 'connection_capacity'
    release.set()
    await task
    await svc.close()


@pytest.mark.asyncio
async def test_disconnect_during_precommit_close_waits_without_fatal_cleanup(setup):
    owner, res, repo, browser, svc = setup
    fatal = Mock()
    svc.cleanup_failure = fatal
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_close():
        entered.set()
        await release.wait()
    browser.close.side_effect = slow_close
    await svc.start(owner, 'demo_user', 'UTC')
    task = asyncio.create_task(svc.complete(owner, res.id))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    assert (await task)['state'] == 'connected'
    fatal.assert_not_called()
    await svc.close()


@pytest.mark.asyncio
async def test_incomplete_start_cleanup_never_releases_a_live_browser(setup):
    from engagemate.hosted_login.protocol import BrowserCleanupError
    owner, res, repo, browser, svc = setup
    fatal = Mock()
    svc.cleanup_failure = fatal
    svc.open_browser.side_effect = BrowserCleanupError(browser)
    browser.close.side_effect = RuntimeError('close failed')
    with pytest.raises(BrowserCleanupError):
        await svc.start(owner, 'demo_user', 'UTC')
    fatal.assert_called_once()
    repo.release.assert_not_called()
    assert svc.sessions[res.id].browser is browser
    browser.close.side_effect = None
    await svc.close()


@pytest.mark.asyncio
async def test_lease_cleanup_failure_preserves_committed_success(setup):
    owner, res, repo, browser, svc = setup
    lease = Mock()
    lease.__enter__ = Mock()
    lease.__exit__ = Mock(side_effect=RuntimeError('lost database connection'))
    svc.lease_factory = lambda account_id: lease
    svc.cleanup_failure = Mock()
    await svc.start(owner, 'demo_user', 'UTC')
    result = await svc.complete(owner, res.id)
    assert result['state'] == 'connected'
    assert (await svc.status(owner, res.id))['state'] == 'connected'
    assert (await svc.complete(owner, res.id))['state'] == 'connected'
    browser.close.assert_awaited_once()
    repo.complete.assert_called_once()
    svc.cleanup_failure.assert_not_called()
    assert svc.cleanup_failed
    with pytest.raises(ConnectError):
        await svc.start(owner, 'demo_user', 'UTC')


@pytest.mark.asyncio
async def test_completed_receipt_recovers_after_restart_without_browser(setup):
    owner, res, repo, browser, svc = setup
    receipt = {"id": str(res.id), "state": "connected", "account": {"username": "demo_user"}}
    repo.completed.return_value = receipt
    assert await svc.status(owner, res.id) == receipt
    assert await svc.complete(owner, res.id) == receipt
    repo.completed.assert_called_with(res.id, owner)
    browser.capture.assert_not_called()
    repo.complete.assert_not_called()
