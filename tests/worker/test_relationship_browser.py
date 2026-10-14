from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode

import pytest
from engagemate.worker import relationship_browser as module

HANDLE, OWNER = "target", "1931354828"
NOW = datetime(2026, 9, 14, tzinfo=UTC)


def profile(**values):
    return {
        "handle": HANDLE,
        "owner_id": OWNER,
        "private": False,
        "following": False,
        "followed_by": False,
        **values,
    }


def variables():
    return {
        "target_user_id": OWNER,
        "container_module": "profile",
        "nav_chain": "native-navigation",
    }


def request(
    action="follow",
    *,
    name=None,
    values=None,
    url="https://www.instagram.com/api/graphql",
    method="POST",
):
    return SimpleNamespace(
        method=method,
        url=url,
        resource_type="fetch",
        post_data=urlencode(
            {
                "fb_api_req_friendly_name": name or module._MUTATIONS[action],
                "variables": json.dumps(values if values is not None else variables()),
            }
        ),
    )


def receipt(action="follow", **changes):
    return {
        "data": {
            module._ROOTS[action]: {
                "username": HANDLE,
                "friendship_status": {
                    "following": action == "follow",
                    "outgoing_request": False,
                },
                **changes,
            }
        }
    }


class Control:
    def __init__(self, page, name):
        self.page, self.name = page, name

    async def count(self):
        if self.name == "recipient" and self.page.recipient_loading_reads:
            self.page.recipient_loading_reads -= 1
            return 0
        return self.page.counts.get(self.name, 1)

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def wait_for(self, **kwargs):
        pass

    def get_by_role(self, role, *, name, exact=True):
        if isinstance(name, re.Pattern):
            actual = self.page.following_name
            return Control(self.page, "Following" if name.fullmatch(actual) else "missing")
        if name == "Following" and self.page.following_name != name:
            return Control(self.page, "missing")
        return Control(self.page, name)

    def get_by_text(self, text, **kwargs):
        return Control(self.page, "recipient" if self.name == "dialog" else "identity")

    async def click(self, **kwargs):
        self.page.clicks.append(self.name)
        if self.name == "Following":
            await self.page.emit(
                request("unfollow")
            )  # Must remain unarmed during the dialog opener.
            return
        assert self.page.callback_calls == 1
        await self.page.emit(self.page.request)
        if self.page.duplicate:
            await self.page.emit(self.page.request)


class Page:
    def __init__(self):
        self.url = "about:blank"
        self.listeners = {}
        self.counts = {"missing": 0}
        self.following_name = "Following"
        self.recipient_loading_reads = 0
        self.clicks, self.forwarded, self.aborted = [], [], []
        self.order = []
        self.callback_calls = 0
        self.request = request()
        self.payload = receipt()
        self.status = 200
        self.duplicate = False
        self.response_missing = False
        self.body_stalled = False
        self.close_error = False
        self.closed = False
        self.during_inspect = None
        self.during_close = None

    async def route(self, pattern, handler):
        self.guard = handler
        self.order.append("guard")

    async def route_web_socket(self, pattern, handler):
        self.socket_guard = handler

    def on(self, event, handler):
        self.listeners[event] = handler

    def locator(self, selector):
        assert selector == "main header"
        return Control(self, "header")

    def get_by_role(self, role):
        assert role == "dialog"
        return Control(self, "dialog")

    async def emit(self, req):
        async def abort():
            self.aborted.append(req)

        async def forward():
            self.forwarded.append(req)

            async def body():
                if self.body_stalled:
                    await asyncio.Event().wait()
                return (
                    self.payload
                    if type(self.payload) is bytes
                    else json.dumps(self.payload).encode()
                )

            if not self.response_missing:
                self.listeners["response"](
                    SimpleNamespace(url=req.url, request=req, status=self.status, body=body)
                )

        await self.guard(SimpleNamespace(request=req, abort=abort, continue_=forward))

    async def close(self, *, run_before_unload):
        assert run_before_unload is False
        if self.during_close:
            await self.emit(self.during_close)
        self.order.append("close")
        if self.close_error:
            raise RuntimeError("private browser details")
        self.closed = True


@pytest.fixture
def world(monkeypatch):
    page = Page()
    profiles = [profile(), profile(following=True)]

    class Inspector:
        def __init__(self, supplied, **kwargs):
            assert supplied is page

        async def inspect(self, handle):
            assert handle == HANDLE and page.order[0] == "guard"
            page.url = "https://www.instagram.com/target/"
            page.order.append("inspect")
            if page.during_inspect:
                await page.during_inspect()
            result = profiles.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(module, "PlaywrightFollowerBrowser", Inspector)

    async def new_page():
        return page

    async def cdp_session(supplied):
        assert supplied is page

        async def send(name, params):
            assert name == "Network.setBypassServiceWorker" and params == {
                "bypass": True
            }

        async def detach():
            page.order.append("detach")

        return SimpleNamespace(send=send, detach=detach)

    async def before():
        page.callback_calls += 1
        return True

    browser = module.RelationshipBrowser(
        SimpleNamespace(new_page=new_page, new_cdp_session=cdp_session),
        clock=lambda: NOW,
        timeout_ms=100,
    )
    return SimpleNamespace(page=page, profiles=profiles, browser=browser, before=before)


@pytest.mark.asyncio
async def test_lost_follow_response_reconciles_exact_fresh_state_without_replay(world):
    world.page.response_missing = True
    outcome, evidence = await world.browser.perform(HANDLE, OWNER, "follow", world.before)
    assert outcome == "confirmed"
    assert evidence["receipt_source"] == "profile_reconciliation"
    assert evidence["receipt_matches"] is False and evidence["receipt_timeout"] is True
    assert evidence["fresh_profile_matches"] is True
    assert evidence["following"] is False  # Preserve the pre-write baseline.
    assert world.page.clicks == ["Follow"] and len(world.page.forwarded) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh", [profile(), profile(owner_id="foreign", following=True),
                                   profile(private=True, following=True), RuntimeError("unreadable")])
async def test_missing_response_with_unverified_fresh_state_stays_uncertain(world, fresh):
    world.page.response_missing = True
    world.profiles[-1] = fresh
    outcome, _ = await world.browser.perform(HANDLE, OWNER, "follow", world.before)
    assert outcome == "uncertain"
    assert world.page.clicks == ["Follow"] and len(world.page.forwarded) == 1


@pytest.mark.asyncio
async def test_lost_unfollow_response_remains_uncertain(world):
    world.page.response_missing = True
    world.page.request = request("unfollow")
    world.profiles[:] = [profile(following=True), profile()]
    outcome, _ = await world.browser.perform(HANDLE, OWNER, "unfollow", world.before)
    assert outcome == "uncertain" and world.page.order.count("inspect") == 1


@pytest.mark.asyncio
async def test_observed_rejected_response_with_stalled_body_never_reconciles(world):
    world.page.status = 500
    world.page.body_stalled = True
    outcome, evidence = await world.browser.perform(HANDLE, OWNER, "follow", world.before)
    assert outcome == "uncertain" and evidence["http_status"] == 500
    assert world.page.order.count("inspect") == 1


def readback_evidence():
    return dict(receipt_source="profile_reconciliation", receipt_matches=False,
                receipt_timeout=True, mutation_attempted=True, request_forwarded=True,
                identity_matches=True, private=False, following=False, followed_by=False,
                fresh_profile_matches=True)


@pytest.mark.parametrize("key", list(readback_evidence()))
def test_reconciliation_boundary_requires_every_proof(key):
    evidence = readback_evidence()
    evidence.pop(key)
    assert not module.confirmed_relationship_evidence("follow", evidence)


def test_reconciliation_boundary_rejects_health_signal_and_unfollow():
    assert module.confirmed_relationship_evidence("follow", readback_evidence())
    assert not module.confirmed_relationship_evidence("unfollow", readback_evidence())
    assert not module.confirmed_relationship_evidence("follow", {**readback_evidence(), "health_signal":"rate_limited"})


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["follow", "unfollow"])
async def test_confirmed_requires_one_claimed_request_receipt_and_fresh_profile(
    world, action
):
    world.page.request = request(action)
    world.page.payload = receipt(action)
    world.page.duplicate = True
    world.profiles[:] = [
        profile(following=action == "unfollow"),
        profile(following=action == "follow"),
    ]
    outcome, evidence = await world.browser.perform(HANDLE, OWNER, action, world.before)
    assert outcome == "confirmed" and evidence["fresh_profile_matches"]
    assert len(world.page.forwarded) == 1 and len(world.page.aborted) >= 1
    assert world.page.callback_calls == 1 and world.page.order.count("inspect") == 2
    assert world.page.closed and world.page.order[-2:] == ["close", "detach"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,state,outcome",
    [("follow", True, "already_following"), ("unfollow", False, "already_absent")],
)
async def test_noop_never_calls_claim_or_click(world, action, state, outcome):
    world.profiles[:] = [profile(following=state)]
    result, evidence = await world.browser.perform(HANDLE, OWNER, action, world.before)
    assert result == outcome and evidence["mutation_attempted"] is False
    assert (
        world.page.callback_calls == 0 and world.page.clicks == [] and world.page.closed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"private": True},
        {"private": None},
        {"following": None},
        {"followed_by": True},
        {"followed_by": None},
        {"owner_id": "other"},
        {"handle": "other"},
    ],
)
async def test_follow_requires_explicit_eligible_identity(world, changes):
    world.profiles[:] = [profile(**changes)]
    result, _ = await world.browser.perform(HANDLE, OWNER, "follow", world.before)
    assert (
        result == "deferred"
        and world.page.callback_calls == 0
        and world.page.clicks == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [False, None, 1, "true"])
async def test_callback_must_return_true_once_before_any_mutation(world, answer):
    async def before():
        world.page.callback_calls += 1
        return answer

    result, _ = await world.browser.perform(HANDLE, OWNER, "follow", before)
    assert (
        result == "deferred"
        and world.page.callback_calls == 1
        and world.page.forwarded == []
    )


@pytest.mark.asyncio
async def test_identity_rechecked_after_callback(world):
    async def before():
        world.page.callback_calls += 1
        world.page.url = "https://www.instagram.com/other/"
        return True

    result, _ = await world.browser.perform(HANDLE, OWNER, "follow", before)
    assert result == "deferred" and world.page.clicks == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"status": "ok"},
        receipt(username="foreign"),
        receipt("unfollow"),
        {"data": {"xdt_create_friendship": None}},
        {**receipt(), "errors": [{"message": "failed"}]},
    ],
)
async def test_unknown_receipt_is_uncertain_and_never_retried(world, payload):
    world.page.payload = payload
    result, evidence = await world.browser.perform(
        HANDLE, OWNER, "follow", world.before
    )
    assert result == "uncertain" and evidence["request_forwarded"]
    assert world.page.clicks == ["Follow"] and world.page.order.count("inspect") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fresh",
    [
        profile(),
        profile(following=True, owner_id="other"),
        profile(following=True, private=True),
    ],
)
async def test_receipt_without_fresh_relationship_never_confirms(world, fresh):
    world.profiles[1] = fresh
    assert (await world.browser.perform(HANDLE, OWNER, "follow", world.before))[
        0
    ] == "uncertain"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,signal",
    [
        (429, "rate_limited"),
        (401, "authentication_required"),
    ],
)
async def test_restriction_propagates_bounded_health(world, status, signal):
    world.page.status = status
    result, evidence = await world.browser.perform(
        HANDLE, OWNER, "follow", world.before
    )
    assert result == "uncertain" and evidence["health_signal"] == signal


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [receipt(), {"message": "csrf_failed"}, {}])
async def test_plain_forbidden_mutation_stays_uncertain_without_false_login(world, payload):
    world.page.status, world.page.payload = 403, payload
    result, evidence = await world.browser.perform(HANDLE, OWNER, "follow", world.before)
    assert result == "uncertain"
    assert evidence["request_forwarded"] and evidence["mutation_attempted"]
    assert evidence["http_status"] == 403 and evidence["receipt_matches"] is False
    assert "health_signal" not in evidence
    assert world.page.clicks == ["Follow"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,signal", [
    ({"message": "login_required"}, "authentication_required"),
    ({"require_login": True}, "authentication_required"),
    ({"challenge": {"url": "hidden"}}, "checkpoint_required"),
    ({"message": "Please wait a few minutes"}, "rate_limited"),
])
async def test_explicit_forbidden_mutation_restrictions_still_pause(world, payload, signal):
    world.page.status, world.page.payload = 403, payload
    result, evidence = await world.browser.perform(HANDLE, OWNER, "follow", world.before)
    assert result == "uncertain" and evidence["health_signal"] == signal
    assert world.page.clicks == ["Follow"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "PolarisStoriesV3SeenMutation",
        "usePolarisFollowMutationBypassFrictionMutation",
        "usePolarisFollowUserFollowMutation",
        "usePolarisUnfollowMutation",
    ],
)
async def test_only_exact_armed_native_mutation_can_forward(world, name):
    world.page.request = request(name=name)
    result, _ = await world.browser.perform(HANDLE, OWNER, "follow", world.before)
    assert result == "uncertain" and world.page.forwarded == []


@pytest.mark.asyncio
async def test_early_and_cleanup_writes_stay_blocked(world):
    async def early():
        await world.page.emit(request())

    world.page.during_inspect = early
    world.page.during_close = request()
    result, _ = await world.browser.perform(HANDLE, OWNER, "follow", world.before)
    assert (
        result == "confirmed"
        and len(world.page.forwarded) == 1
        and len(world.page.aborted) == 3
    )


@pytest.mark.asyncio
async def test_cancelled_inspection_closes_under_guard(world):
    started = asyncio.Event()

    async def pending():
        started.set()
        await asyncio.Event().wait()

    world.page.during_inspect = pending
    task = asyncio.create_task(
        world.browser.perform(HANDLE, OWNER, "follow", world.before)
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.page.closed and world.page.callback_calls == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"target_user_id": True},
        {"target_user_id": 1931354828},
        {"target_user_id": "other"},
        {"extra": True},
        {"nav_chain": ""},
        {"nav_chain": "x\nsecret"},
        {"container_module": None},
    ],
)
def test_native_variables_reject_unmeasured_shapes(changes):
    assert not module._mutation_matches({**variables(), **changes}, OWNER)


@pytest.mark.parametrize("action", ["follow", "unfollow"])
@pytest.mark.parametrize(
    "conflict",
    [
        {"status": None},
        {"status": "fail"},
        {"error": True},
        {"success": False},
        {"pk": "other"},
        {"id": "999999"},
    ],
)
def test_receipts_reject_conflicting_fields(action, conflict):
    payload = receipt(action)
    payload["data"][module._ROOTS[action]].update(conflict)
    assert not module._receipt_matches(payload, action, HANDLE, OWNER)


@pytest.mark.parametrize(
    "url,resource",
    [
        ("https://www.instagram.com/stories/target/", "document"),
        ("https://www.instagram.com/direct/inbox/", "document"),
        ("https://www.instagram.com/api/v1/friendships/1/destroy/", "fetch"),
        ("https://evil.example/rsrc.php/file.js", "script"),
    ],
)
def test_profile_guard_rejects_unrelated_reads(url, resource):
    req = request(url=url, method="GET")
    req.resource_type = resource
    assert not module._read_request(req, HANDLE, OWNER)


@pytest.mark.asyncio
@pytest.mark.parametrize("accessible_name", ["Following", "Following Down chevron icon"])
async def test_unfollow_accepts_measured_following_control_names(world, accessible_name):
    world.page.following_name = accessible_name
    world.page.request = request("unfollow")
    world.page.payload = receipt("unfollow")
    world.profiles[:] = [profile(following=True), profile(following=False)]
    outcome, evidence = await world.browser.perform(HANDLE, OWNER, "unfollow", world.before)
    assert outcome == "confirmed"
    assert evidence["receipt_matches"] and evidence["fresh_profile_matches"]
    assert world.page.clicks == ["Following", "Unfollow"]
    assert len(world.page.forwarded) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("accessible_name", ["Not Following", "Following someone else", "Following Down chevron icon extra"])
async def test_unfollow_rejects_unmeasured_control_names(world, accessible_name):
    world.page.following_name = accessible_name
    world.profiles[:] = [profile(following=True)]
    outcome, evidence = await world.browser.perform(HANDLE, OWNER, "unfollow", world.before)
    assert outcome == "deferred"
    assert not evidence["mutation_attempted"]
    assert world.page.callback_calls == 0 and not world.page.clicks


@pytest.mark.asyncio
async def test_unfollow_waits_for_dialog_recipient_after_loading_spinner(world):
    world.page.recipient_loading_reads = 1
    world.page.request = request("unfollow")
    world.page.payload = receipt("unfollow")
    world.profiles[:] = [profile(following=True), profile(following=False)]
    outcome, evidence = await world.browser.perform(HANDLE, OWNER, "unfollow", world.before)
    assert outcome == "confirmed" and evidence["fresh_profile_matches"]
    assert world.page.callback_calls == 1


@pytest.mark.asyncio
async def test_unfollow_never_arms_dialog_without_recipient(world):
    world.page.counts["recipient"] = 0
    world.profiles[:] = [profile(following=True)]
    outcome, evidence = await world.browser.perform(HANDLE, OWNER, "unfollow", world.before)
    assert outcome == "deferred" and not evidence["mutation_attempted"]
    assert world.page.callback_calls == 0 and not world.page.forwarded
    assert world.page.clicks == ["Following"]


@pytest.fixture
def follow_flow(world, monkeypatch):
    from engagemate.worker import follow_runtime
    from engagemate.worker.follow_maintenance import FollowMaintenance

    performer = follow_runtime.LiveFollowPerformer(None, None, None)
    performer._sender = Mock(return_value=('123', 'sender_owner'))
    performer._pause = Mock()
    monkeypatch.setattr(follow_runtime.ProfileCaptureService, '_load', Mock(return_value='bound'))
    context = world.browser._context
    context.cookies = AsyncMock(return_value=[{'name': 'ds_user_id', 'value': '123'}])
    visits = []
    context_cleanup = AsyncMock()

    @asynccontextmanager
    async def opened(_bound):
        visits.append('open')
        try:
            yield SimpleNamespace(context=context)
        finally:
            visits.append('close')
            await context_cleanup()

    monkeypatch.setattr(follow_runtime, 'LeasedBrowserFactory', lambda *_args: SimpleNamespace(open=opened))
    monkeypatch.setattr(module, 'RelationshipBrowser', lambda _context: world.browser)
    claim = SimpleNamespace(id='follow', owner_id=OWNER, handle=HANDLE, action='follow',
        user_id='user', ig_account_id='account')

    async def attempting(*_args):
        return await world.before()

    store = SimpleNamespace(enqueue_confirmed=AsyncMock(), claim_next=AsyncMock(return_value=claim),
        mark_attempting=AsyncMock(side_effect=attempting), finish=AsyncMock(return_value=True))
    worker = FollowMaintenance(store, performer, execution_mode='autonomous', clock=lambda: NOW)
    return SimpleNamespace(worker=worker, store=store, visits=visits, cleanup=context_cleanup, performer=performer)


@pytest.mark.parametrize('cleanup', ['page_error', 'context_error', 'page_cancel', 'context_cancel', 'masked_cancel'])
async def test_native_receipt_is_durable_before_cleanup_and_cancel(world, follow_flow, cleanup):
    flow = follow_flow
    original = asyncio.CancelledError()
    confirmed = {}

    async def finish(claim, outcome, now, evidence):
        assert 'close' not in world.page.order and flow.visits == ['open']
        assert outcome == 'confirmed' and now == NOW
        assert evidence['receipt_matches'] and evidence['fresh_profile_matches']
        confirmed.update(evidence)
        return True

    flow.store.finish.side_effect = finish
    if cleanup == 'page_error':
        world.page.close_error = True
        world.page.during_close = request()  # Guard stays closed after the receipt.
    elif cleanup in {'page_cancel', 'masked_cancel'}:
        async def close(**_kwargs):
            flow.store.finish.assert_awaited_once()
            raise original
        world.page.close = close
    if cleanup in {'context_error', 'masked_cancel'}:
        flow.cleanup.side_effect = RuntimeError('private context detail')
    elif cleanup == 'context_cancel':
        flow.cleanup.side_effect = original

    if 'cancel' in cleanup:
        with pytest.raises(asyncio.CancelledError) as caught:
            await flow.worker.run_once('worker')
        assert type(caught.value) is asyncio.CancelledError and caught.value is original
    else:
        summary = await flow.worker.run_once('worker')
        assert summary.engaged == 1
    flow.store.finish.assert_awaited_once()
    assert confirmed and 'cleanup_failed' not in confirmed and 'private context detail' not in str(confirmed)
    assert len(world.page.forwarded) == 1
    if cleanup == 'page_error':
        assert len(world.page.aborted) == 1


@pytest.mark.parametrize('accepted', [False, True])
async def test_follow_cleanup_timeout_observes_one_existing_finish(world, follow_flow, monkeypatch, accepted):
    flow = follow_flow
    flow.store.finish.return_value = accepted
    real_timeout = asyncio.timeout
    deadline = real_timeout(None)
    monkeypatch.setattr(asyncio, 'timeout',
        lambda seconds: deadline if seconds == 105 else real_timeout(seconds))

    async def close(**_kwargs):
        flow.store.finish.assert_awaited_once()
        deadline.reschedule(asyncio.get_running_loop().time())
        await asyncio.sleep(0.01)

    world.page.close = close
    summary = await flow.worker.run_once('worker')
    assert deadline.expired()
    flow.store.finish.assert_awaited_once()
    assert flow.store.finish.call_args.args[1] == 'confirmed'
    assert summary.engaged == int(accepted)
    assert summary.stop_reason == ('follow_confirmed' if accepted else 'follow_lease_lost')


async def test_confirmed_follow_finishes_before_cleanup_outlives_lease(world, follow_flow):
    flow = follow_flow
    current = NOW
    flow.worker.clock = lambda: current

    async def finish(claim, outcome, now, evidence):
        assert now == NOW and 'close' not in world.page.order
        return now < NOW + timedelta(seconds=120)

    async def close(**_kwargs):
        nonlocal current
        flow.store.finish.assert_awaited_once()
        current = NOW + timedelta(seconds=121)

    flow.store.finish.side_effect = finish
    world.page.close = close
    summary = await flow.worker.run_once('worker')
    flow.store.finish.assert_awaited_once()
    assert summary.engaged == 1 and current > NOW + timedelta(seconds=120)


async def test_cancel_during_follow_finish_retains_one_commit(world, follow_flow):
    flow = follow_flow
    committing, committed = asyncio.Event(), asyncio.Event()

    async def finish(*_args):
        committing.set()
        await committed.wait()
        return True

    flow.store.finish.side_effect = finish
    task = asyncio.create_task(flow.worker.run_once('worker'))
    try:
        await asyncio.wait_for(committing.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        committed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        flow.store.finish.assert_awaited_once()
        assert flow.store.finish.call_args.args[1] == 'confirmed'
    finally:
        committed.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize('failure', ['receipt', 'readback'])
async def test_unknown_native_follow_remains_uncertain_before_cleanup(world, follow_flow, failure):
    if failure == 'receipt':
        world.page.payload = {}
    else:
        world.profiles[1] = profile(following=True, owner_id='other')
    world.page.close_error = True
    summary = await follow_flow.worker.run_once('worker')
    follow_flow.store.finish.assert_awaited_once()
    assert follow_flow.store.finish.call_args.args[1] == 'uncertain'
    assert summary.engaged == 0


async def test_immediate_finish_error_cannot_report_confirmed(world, follow_flow):
    original = RuntimeError('finish unavailable')
    follow_flow.store.finish.side_effect = original
    with pytest.raises(RuntimeError) as caught:
        await follow_flow.worker.run_once('worker')
    assert caught.value is original
    follow_flow.store.finish.assert_awaited_once()
    assert world.page.closed
