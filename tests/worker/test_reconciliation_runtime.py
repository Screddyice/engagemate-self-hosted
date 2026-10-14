from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from engagemate.worker import reconciliation_runtime as module
from engagemate.worker.follow_maintenance import SequencedWorker
from engagemate.worker.follower_browser import FollowerBrowserError
from engagemate.worker.relationship_reconciliation import RelationshipReconciliation

NOW = datetime(2026, 9, 23, tzinfo=UTC)


@pytest.fixture
def setup(monkeypatch):
    claim = SimpleNamespace(
        id="claim",
        user_id="user",
        ig_account_id="account",
        owner_id="456",
        handle="target",
        cached_observation=None,
    )
    reader = module.LiveReconciliationReader(None, None, None, clock=lambda: NOW)
    reader.accounts = SimpleNamespace(_sender=Mock(return_value=("123", "sender")), _pause=Mock())
    monkeypatch.setattr(
        module.ProfileCaptureService, "_load", Mock(return_value="bound")
    )
    page = SimpleNamespace(close=AsyncMock())
    context = SimpleNamespace(
        on=Mock(),
        cookies=AsyncMock(return_value=[{"name": "ds_user_id", "value": "123"}]),
        route=AsyncMock(),
        route_web_socket=AsyncMock(),
        new_page=AsyncMock(return_value=page),
    )

    @asynccontextmanager
    async def opened(bound):
        yield SimpleNamespace(context=context)

    monkeypatch.setattr(
        module, "LeasedBrowserFactory", lambda *args: SimpleNamespace(open=opened)
    )
    inspect = AsyncMock(
        return_value={"owner_id": "456", "handle": "target", "followed_by": True}
    )
    monkeypatch.setattr(
        module,
        "PlaywrightFollowerBrowser",
        lambda page: SimpleNamespace(inspect=inspect),
    )
    return claim, reader, context, inspect


async def test_read_guard_and_typed_observation_only(setup):
    claim, reader, context, _inspect = setup
    value, reason = await reader(claim, AsyncMock(return_value=True))
    assert value == {
        "owner_id": "456",
        "identity_matches": True,
        "followed_by": True,
        "observed_at": NOW.isoformat(),
    }
    assert reason is None
    guard = context.route.call_args.args[1]
    for method, url, allowed in [
        ("GET", "https://www.instagram.com/target/", True),
        ("POST", "https://www.instagram.com/api/v1/friendships/456/destroy/", False),
        ("GET", "https://www.instagram.com/other/", False),
    ]:
        route = SimpleNamespace(
            request=SimpleNamespace(
                method=method, url=url, resource_type="document", post_data=None
            ),
            continue_=AsyncMock(),
            abort=AsyncMock(),
        )
        await guard(route)
        assert route.continue_.await_count == int(allowed)
        assert route.abort.await_count == int(not allowed)
    socket = SimpleNamespace(close=AsyncMock())
    await context.route_web_socket.call_args.args[1](socket)
    socket.close.assert_awaited_once()


@pytest.mark.parametrize(
    "signal", ["rate_limited", "authentication_required", "checkpoint_required"]
)
async def test_restrictions_pause_and_stop(setup, signal):
    claim, reader, context, inspect = setup
    inspect.side_effect = FollowerBrowserError(signal)
    assert await reader(claim, AsyncMock(return_value=True)) == (None, signal)
    reader.accounts._pause.assert_called_once_with(claim, signal)
    context.new_page.return_value.close.assert_awaited_once()


@pytest.mark.parametrize('signal', [None, 'authentication_required', 'checkpoint_required', 'rate_limited'])
async def test_forbidden_read_cannot_confirm_or_mask_explicit_restriction(setup, signal):
    claim, reader, context, inspect = setup
    async def read_profile(*args):
        context.on.call_args.args[1](SimpleNamespace(url='https://www.instagram.com/api/graphql', status=403))
        if signal:
            raise FollowerBrowserError(signal)
        return {'owner_id':'456','handle':'target','followed_by':True}
    inspect.side_effect = read_profile
    assert await reader(claim, AsyncMock(return_value=True)) == (None, signal or 'profile_unreadable')
    if signal:
        reader.accounts._pause.assert_called_once_with(claim, signal)
    else:
        reader.accounts._pause.assert_not_called()
    context.new_page.return_value.close.assert_awaited_once()


@pytest.mark.parametrize("identity", ["other", "", None])
async def test_sender_mismatch_never_navigates(setup, identity):
    claim, reader, context, inspect = setup
    context.cookies.return_value = [{"name": "ds_user_id", "value": identity}]
    assert await reader(claim, AsyncMock(return_value=True)) == (
        None,
        "identity_mismatch",
    )
    inspect.assert_not_called()


@pytest.mark.parametrize(
    "patch",
    [
        {"owner_id": "999"},
        {"handle": "other"},
        {"followed_by": None},
        {"followed_by": "true"},
    ],
)
async def test_target_unknown_never_becomes_false_outcome(setup, patch):
    claim, reader, _context, inspect = setup
    inspect.return_value.update(patch)
    value, _ = await reader(claim, AsyncMock(return_value=True))
    assert value is None


async def test_cached_observation_skips_browser_and_policy_checked_twice_for_reads(
    setup,
):
    claim, reader, context, inspect = setup
    claim.cached_observation = {"followed_by": True}
    assert await reader(claim, AsyncMock(return_value=True)) == (
        claim.cached_observation,
        None,
    )
    context.cookies.assert_not_called()
    claim.cached_observation = None
    assert await reader(claim, AsyncMock(side_effect=[True, False])) == (
        None,
        "policy_changed",
    )
    inspect.assert_not_called()


def maintenance(claim):
    store = SimpleNamespace(
        claim_next=AsyncMock(return_value=claim),
        permitted=AsyncMock(return_value=True),
        finish=AsyncMock(return_value=True),
    )
    reader = AsyncMock(return_value=(None, "unknown"))
    return (
        store,
        reader,
        RelationshipReconciliation(
            store, reader, execution_mode="autonomous", clock=lambda: NOW
        ),
    )


async def test_no_billable_or_social_success_and_no_canary_reads(setup):
    claim, *_ = setup
    store, _reader, worker = maintenance(claim)
    result = await worker.run_once("w")
    assert result.engaged == 0 and result.considered == 1
    worker.execution_mode = "canary"
    store.claim_next.reset_mock()
    assert await worker.run_once("w") is None
    store.claim_next.assert_not_called()


async def test_cancel_finishes_reserved_attempt_without_refund(setup):
    import asyncio

    claim, *_ = setup
    store, reader, worker = maintenance(claim)
    reader.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await worker.run_once("w")
    store.finish.assert_awaited_once_with(claim, NOW, reason="worker_cancelled")


async def test_timeout_records_unknown_and_sensitive_errors_redacted(setup):
    claim, *_ = setup
    for error, reason in [
        (TimeoutError(), "timeout"),
        (RuntimeError("secret password"), "unknown"),
    ]:
        store, reader, worker = maintenance(claim)
        reader.side_effect = error
        await worker.run_once("w")
        store.finish.assert_awaited_once_with(claim, NOW, None, reason)
        assert "secret" not in str(store.finish.call_args)


async def test_three_busy_lanes_are_fair():
    lanes = [
        SimpleNamespace(run_once=AsyncMock(return_value=name))
        for name in ("story", "follow", "read")
    ]
    worker = SequencedWorker(*lanes)
    assert [await worker.run_once("w") for _ in range(6)] == [
        "follow",
        "story",
        "read",
    ] * 2


async def test_blocked_checkpoint_redirect_pauses_without_forwarding(setup):
    claim, reader, context, inspect = setup

    async def redirected(handle):
        guard = context.route.call_args.args[1]
        route = SimpleNamespace(
            request=SimpleNamespace(
                url="https://www.instagram.com/challenge/123/",
                method="GET",
                resource_type="document",
            ),
            continue_=AsyncMock(),
            abort=AsyncMock(),
        )
        await guard(route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_called()
        raise FollowerBrowserError("profile_unreadable")

    inspect.side_effect = redirected
    assert await reader(claim, AsyncMock(return_value=True)) == (
        None,
        "checkpoint_required",
    )
    reader.accounts._pause.assert_called_once_with(claim, "checkpoint_required")


async def test_fresh_manual_negative_skips_browser(setup):
    claim, reader, context, _inspect = setup
    claim.cached_observation = {"followed_by": False}
    assert await reader(claim, AsyncMock(return_value=True)) == (
        claim.cached_observation,
        None,
    )
    context.cookies.assert_not_called()


async def test_http_rate_limit_blocks_remaining_requests(setup):
    claim, reader, context, inspect = setup

    async def restricted(handle):
        context.on.call_args.args[1](
            SimpleNamespace(url="https://www.instagram.com/api/graphql", status=429)
        )
        route = SimpleNamespace(
            request=SimpleNamespace(
                url="https://www.instagram.com/target/",
                method="GET",
                resource_type="document",
            ),
            continue_=AsyncMock(),
            abort=AsyncMock(),
        )
        await context.route.call_args.args[1](route)
        route.abort.assert_awaited_once()
        route.continue_.assert_not_called()
        return {"owner_id": "456", "handle": "target", "followed_by": True}

    inspect.side_effect = restricted
    assert await reader(claim, AsyncMock(return_value=True)) == (None, "rate_limited")
    reader.accounts._pause.assert_called_once_with(claim, "rate_limited")


@pytest.mark.parametrize("account_identity", [None, "123", (), ("123",),
    ("123", "sender", "extra"), (None, "sender"), ("invalid", "sender"),
    ("123", None), ("123", ""), ("456", "sender")])
async def test_invalid_sender_contract_fails_closed(setup, account_identity):
    claim, reader, context, inspect = setup
    reader.accounts._sender.return_value = account_identity
    assert await reader(claim, AsyncMock(return_value=True)) == (None, "identity_mismatch")
    context.new_page.assert_not_called()
    inspect.assert_not_called()
