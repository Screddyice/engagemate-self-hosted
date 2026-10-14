from contextlib import asynccontextmanager
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from engagemate.onboarding.profile_capture import ProfileCaptureError
from engagemate.worker import follow_runtime as module


@pytest.mark.parametrize('reason', ['reconnect_required', 'proxy_unavailable'])
async def test_unusable_binding_never_opens_browser(monkeypatch, reason):
    worker = module.LiveFollowPerformer(None, None, None)
    worker._sender = Mock(return_value=('123', 'sender_owner'))
    worker._pause = Mock()
    monkeypatch.setattr(module.ProfileCaptureService, '_load', Mock(side_effect=ProfileCaptureError(reason)))
    factory = Mock()
    monkeypatch.setattr(module, 'LeasedBrowserFactory', factory)
    result = await worker(SimpleNamespace(owner_id='456', user_id='user', ig_account_id='account'), AsyncMock())
    assert result[0] == 'deferred'
    factory.assert_not_called()
    assert worker._pause.call_count == int(reason == 'reconnect_required')


@pytest.mark.parametrize('identity', ['123', 'different'])
async def test_sender_identity_checked_before_adapter(monkeypatch, identity):
    from engagemate.worker import relationship_browser
    worker = module.LiveFollowPerformer(None, None, None)
    worker._sender = Mock(return_value=('123', 'sender_owner'))
    worker._pause = Mock()
    monkeypatch.setattr(module.ProfileCaptureService, '_load', Mock(return_value='bound'))
    context = SimpleNamespace(cookies=AsyncMock(return_value=[{'name': 'ds_user_id', 'value': identity}]))

    @asynccontextmanager
    async def opened(bound):
        assert bound == 'bound'
        yield SimpleNamespace(context=context)

    monkeypatch.setattr(module, 'LeasedBrowserFactory', lambda *args: SimpleNamespace(open=opened))
    perform = AsyncMock(return_value=('deferred', {'health_signal': 'rate_limited'}))
    monkeypatch.setattr(relationship_browser, 'RelationshipBrowser', lambda context: SimpleNamespace(perform=perform))
    claim = SimpleNamespace(owner_id='456', handle='target', action='follow', user_id='user', ig_account_id='account')
    callback = AsyncMock()
    result = await worker(claim, callback)
    if identity == '123':
        perform.assert_awaited_once_with('target', '456', 'follow', callback)
        worker._pause.assert_called_once_with(claim, 'rate_limited')
    else:
        assert result[1]['reason'] == 'sender_identity_mismatch'
        perform.assert_not_called()
        worker._pause.assert_called_once_with(claim, 'identity_mismatch')


def test_health_pause_uses_configured_account_repository(monkeypatch):
    factory = Mock(return_value=SimpleNamespace(set_paused=Mock(return_value=None)))
    monkeypatch.setattr(module, "AccountRepository", factory)
    settings = SimpleNamespace(session_encryption_key=SimpleNamespace(get_secret_value=lambda: "test-key"))
    worker = module.LiveFollowPerformer("pool", settings, None)
    claim = SimpleNamespace(user_id="user", ig_account_id="account")
    worker._pause(claim, "rate_limited")
    factory.assert_called_once_with("pool", "test-key")
    factory.return_value.set_paused.assert_called_once_with(
        "user", "account", paused=True, reason="relationship_rate_limit",
    )


def test_login_pause_uses_reconnect_email_reason(monkeypatch):
    factory = Mock(return_value=SimpleNamespace(set_paused=Mock(return_value=None)))
    monkeypatch.setattr(module, "AccountRepository", factory)
    settings = SimpleNamespace(session_encryption_key=SimpleNamespace(get_secret_value=lambda: "test-key"))
    worker = module.LiveFollowPerformer("pool", settings, None)
    worker._pause(SimpleNamespace(user_id="user", ig_account_id="account"), "authentication_required")
    factory.return_value.set_paused.assert_called_once_with(
        "user", "account", paused=True, reason="login_required",
    )


def test_identity_mismatch_pause_is_distinct_from_login_failure(monkeypatch):
    from contextlib import nullcontext

    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    pool = SimpleNamespace(connection=Mock(return_value=nullcontext(connection)))
    factory = Mock(return_value=SimpleNamespace(set_paused=Mock(return_value=True)))
    monkeypatch.setattr(module, "AccountRepository", factory)
    settings = SimpleNamespace(session_encryption_key=SimpleNamespace(get_secret_value=lambda: "test-key"))
    worker = module.LiveFollowPerformer(pool, settings, None)

    worker._pause(SimpleNamespace(user_id="user", ig_account_id="account"), "identity_mismatch")

    factory.return_value.set_paused.assert_called_once_with(
        "user", "account", paused=True, reason="relationship_identity_mismatch",
    )
    event_args = connection.execute.call_args.args
    assert event_args[1][2] == "identity_mismatch"


async def test_authenticated_session_identity_repairs_stale_account_id(monkeypatch):
    from engagemate.worker import relationship_browser

    response = SimpleNamespace(
        status=200,
        json=AsyncMock(
            return_value={
                "user": {
                    "pk": "654321",
                    "pk_id": "654321",
                    "id": "654321",
                    "username": "sender_owner",
                }
            }
        ),
    )
    request = SimpleNamespace(get=AsyncMock(return_value=response))
    context = SimpleNamespace(
        cookies=AsyncMock(return_value=[{"name": "ds_user_id", "value": "654321"}]),
        request=request,
    )

    @asynccontextmanager
    async def opened(_bound):
        yield SimpleNamespace(context=context)

    monkeypatch.setattr(module.ProfileCaptureService, "_load", Mock(return_value="bound"))
    monkeypatch.setattr(
        module,
        "LeasedBrowserFactory",
        lambda *_args: SimpleNamespace(open=opened),
    )
    repair = Mock(return_value=True)
    repository = SimpleNamespace(repair_verified_identity=repair)
    monkeypatch.setattr(module, "AccountRepository", Mock(return_value=repository))
    settings = SimpleNamespace(
        session_encryption_key=SimpleNamespace(get_secret_value=lambda: "test-key")
    )
    worker = module.LiveFollowPerformer("pool", settings, None)
    worker._sender = Mock(return_value=("123456", "sender_owner"))
    worker._pause = Mock()
    perform = AsyncMock(return_value=("completed", {"receipt": True}))
    monkeypatch.setattr(
        relationship_browser,
        "RelationshipBrowser",
        lambda _context: SimpleNamespace(perform=perform),
    )
    claim = SimpleNamespace(
        owner_id="999999", handle="target", action="unfollow", user_id="user",
        ig_account_id="account",
    )
    before_mutation = AsyncMock(return_value=True)

    result = await worker(claim, before_mutation)

    assert result == ("completed", {"receipt": True})
    request.get.assert_awaited_once()
    repair.assert_called_once_with(
        "user", "account", expected_ig_user_id="123456",
        verified_ig_user_id="654321", verified_username="sender_owner",
    )
    perform.assert_awaited_once_with(
        "target", "999999", "unfollow", before_mutation
    )
    worker._pause.assert_not_called()


async def test_authenticated_identity_mismatch_still_pauses(monkeypatch):
    worker = module.LiveFollowPerformer(None, None, None)
    response = SimpleNamespace(
        status=200,
        json=AsyncMock(
            return_value={
                "user": {"pk": "654321", "username": "somebody_else"}
            }
        ),
    )
    context = SimpleNamespace(
        cookies=AsyncMock(return_value=[{"name": "ds_user_id", "value": "654321"}]),
        request=SimpleNamespace(get=AsyncMock(return_value=response)),
    )

    @asynccontextmanager
    async def opened(_bound):
        yield SimpleNamespace(context=context)

    monkeypatch.setattr(module.ProfileCaptureService, "_load", Mock(return_value="bound"))
    monkeypatch.setattr(
        module, "LeasedBrowserFactory", lambda *_args: SimpleNamespace(open=opened)
    )
    from engagemate.worker import relationship_browser

    perform = AsyncMock()
    monkeypatch.setattr(
        relationship_browser,
        "RelationshipBrowser",
        lambda _context: SimpleNamespace(perform=perform),
    )
    worker._sender = Mock(return_value=("123456", "sender_owner"))
    worker._pause = Mock()
    claim = SimpleNamespace(
        owner_id="999999", handle="target", action="unfollow", user_id="user",
        ig_account_id="account",
    )

    result = await worker(claim, AsyncMock())

    assert result[1]["reason"] == "sender_identity_mismatch"
    perform.assert_not_called()
    worker._pause.assert_called_once_with(claim, "identity_mismatch")


@pytest.mark.parametrize('stage', ['before_arm', 'after_arm', 'foreign_sender', 'foreign_adapter'])
async def test_cancel_boundary_rejects_foreign_carriers_and_context_masking(monkeypatch, stage):
    from engagemate.worker import relationship_browser
    from engagemate.worker.follow_maintenance import FollowMaintenance, InterruptedFollowResult

    worker = module.LiveFollowPerformer(None, None, None)
    original = asyncio.CancelledError()
    if stage.startswith('foreign'):
        original._engagemate_follow_result = InterruptedFollowResult('confirmed', {
            'receipt_matches': True, 'fresh_profile_matches': True})
    arm = stage in {'after_arm', 'foreign_adapter'}
    worker._sender = Mock(side_effect=original) if stage == 'foreign_sender' else Mock(return_value=('123', 'sender_owner'))
    monkeypatch.setattr(module.ProfileCaptureService, '_load', Mock(return_value='bound'))
    context = SimpleNamespace(cookies=AsyncMock(return_value=[{'name': 'ds_user_id', 'value': '123'}]))

    @asynccontextmanager
    async def opened(_bound):
        try:
            yield SimpleNamespace(context=context)
        finally:
            raise RuntimeError('private browser detail')

    monkeypatch.setattr(module, 'LeasedBrowserFactory', lambda *_args: SimpleNamespace(open=opened))
    async def perform(handle, owner_id, action, before):
        if arm:
            await before()
        raise original

    monkeypatch.setattr(relationship_browser, 'RelationshipBrowser', lambda _context: SimpleNamespace(perform=perform))
    claim = SimpleNamespace(id='follow', owner_id='456', handle='target', action='follow', user_id='user', ig_account_id='account')
    store = SimpleNamespace(enqueue_confirmed=AsyncMock(), claim_next=AsyncMock(return_value=claim),
        mark_attempting=AsyncMock(return_value=True), finish=AsyncMock(return_value=True))
    maintenance = FollowMaintenance(store, worker, execution_mode='autonomous')
    with pytest.raises(asyncio.CancelledError) as caught:
        await maintenance.run_once('worker')
    assert caught.value is original and not hasattr(original, '_engagemate_follow_result')
    store.finish.assert_awaited_once()
    assert store.finish.call_args.args[1:4:2] == ('uncertain' if arm else 'deferred',
        {'reason': 'worker_cancelled', 'attempted': arm})
