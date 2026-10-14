from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from engagemate.worker.circuit_breaker import HealthSignal
from engagemate.worker.instagram_adapter import ActionOutcome
from engagemate.worker.story_actions import (
    LiveStoryAdapter,
    _acknowledged,
    _reply_acknowledged,
    _reply_variables_match,
    _restriction,
    _shape,
)
from engagemate.worker.story_fingerprint import fingerprint_story
from engagemate.worker.story_identity import StoryIdentityError
from engagemate.worker.story_types import StoryContent, StoryMediaType, StoryOpportunity

OWNER, MEDIA, HANDLE = "12345678901", "3000000000000000870", "sample.viewer25"
TAKEN = datetime.fromtimestamp(1789246777, UTC)
NOW = TAKEN + timedelta(minutes=2)
FP = fingerprint_story(OWNER, MEDIA, TAKEN, None)
SEEN = "PolarisStoriesV3SeenMutation"
LIKE = "usePolarisStoriesV4LikeMutationLikeMutation"
REPLY = "IGDirectStoryShareReplyMutation"
REPLY_TEXT = "A thoughtful moment."
GRAPHQL = "https://www.instagram.com/api/graphql"
OK = {"data": {"xdt_mark_story_reel_seen": {"__typename": "XDTMarkSeenResponse"}}}
LIKE_OK = {
    "data": {"xig_send_story_like": {"__typename": "XIGSendStoryLikeResponsePayload"}}
}


def opportunity():
    return StoryOpportunity(
        uuid4(),
        OWNER,
        HANDLE,
        "discovery",
        MEDIA,
        TAKEN,
        FP,
        StoryContent(StoryMediaType.IMAGE, None, None, None, ()),
    )


def request(name, variables=None, *, url=GRAPHQL, method="POST"):
    if variables is None:
        variables = {
            "reelId": OWNER,
            "reelMediaId": MEDIA,
            "reelMediaOwnerId": OWNER,
            "reelMediaTakenAt": int(TAKEN.timestamp()),
            "viewSeenAt": int(NOW.timestamp()),
        }
    return SimpleNamespace(
        url=url,
        method=method,
        resource_type="fetch",
        post_data=urlencode(
            {
                "fb_api_req_friendly_name": name,
                "variables": json.dumps(variables),
            }
        ),
    )


def like_request(**changes):
    values = {
        "media_id": MEDIA,
        "actor_id": "987654321",
        "client_mutation_id": "native-value",
    }
    values.update(changes)
    return request(LIKE, {"input": values})


def reply_variables():
    return {
        "data": {
            "is_shh_mode": False,
            "media_id": MEDIA,
            "reaction_emoji": None,
            "reel_id": OWNER,
            "sampled": False,
            "share_client_context": "native-context",
            "text": {"sensitive_string_value": REPLY_TEXT},
        },
        "send_data": {
            "forwarded_from_thread_id": None,
            "is_forwarded_from_own_message": None,
            "offline_threading_id": "native-context",
            "recipient_users": json.dumps([OWNER]),
            "thread_id": None,
        },
    }


class Route:
    def __init__(self, page, req):
        self.page, self.request = page, req
        self.forwarded = self.aborted = False

    async def abort(self):
        self.aborted = True
        self.page.aborted.append(self.request)

    async def continue_(self):
        self.forwarded = True
        self.page.forwarded.append(self.request)
        fields = self.request.post_data
        if SEEN in fields:
            self.page.respond(
                self.request, self.page.seen_status, self.page.seen_payload
            )
        elif LIKE in fields:
            self.page.respond(
                self.request, self.page.like_status, self.page.like_payload
            )
        elif REPLY in fields:
            self.page.respond(
                self.request, self.page.reply_status, self.page.reply_payload
            )


class Locator:
    def __init__(self, page, kind, index=None):
        self.page, self.kind, self.index = page, kind, index

    async def count(self):
        if self.kind == "owner" and self.page.owner_pending_reads:
            self.page.owner_pending_reads -= 1
            return 0
        return {
            "auth": self.page.auth_count,
            "owner": self.page.owner_count,
            "time": self.page.time_count,
            "media": 2,
            "like": self.page.like_count,
            "unlike": int(self.page.unlike),
            "button": 1,
            "gate": self.page.gate_count,
            "reply": self.page.reply_count,
            "send": self.page.send_count if self.page.fills else 0,
            "unavailable": int(self.page.unavailable),
        }[self.kind]

    async def get_attribute(self, name):
        if self.kind == "reply" and name == "placeholder":
            return self.page.reply_placeholder
        assert self.kind == "time" and name == "datetime"
        return self.page.taken_at.isoformat()

    def nth(self, index):
        return Locator(self.page, self.kind, index)

    async def bounding_box(self):
        return {
            "width": 32 if self.index == 0 else 583,
            "height": 32 if self.index == 0 else 1037,
        }

    async def evaluate(self, script):
        assert "readyState" in script and "naturalWidth" in script
        return {
            "tag": "IMG", "source": self.page.media_source,
            "loaded": self.page.media_loaded, "width": 583, "height": 1037,
        }

    def locator(self, selector):
        assert self.kind == "like"
        assert selector == "xpath=ancestor::*[self::button or @role='button'][1]"
        return Locator(self.page, "button")

    async def is_visible(self):
        if self.kind == "unavailable":
            return self.page.unavailable_visible
        return (
            self.page.gate_visible if self.kind == "gate" else self.page.button_visible
        )

    async def is_enabled(self):
        return (
            self.page.reply_enabled if self.kind == "reply" else self.page.send_enabled
        )

    async def fill(self, text, **kwargs):
        assert self.kind == "reply" and self.page.guard is not None
        self.page.fills += 1
        self.page.reply_text = text
        if self.page.on_fill is not None:
            await self.page.on_fill()

    async def input_value(self):
        assert self.kind == "reply"
        return self.page.reply_text

    async def click(self, **kwargs):
        if self.kind == "send":
            assert self.page.guard is not None
            self.page.reply_clicks += 1
            await self.page.send(self.page.reply_request)
            if self.page.duplicate_reply:
                await self.page.send(self.page.reply_request)
            self.page.reply_text = ""
            await asyncio.sleep(0)
            return
        if self.kind == "gate":
            assert self.page.guard is not None
            assert self.page.owner_count >= 1 and self.page.taken_at == TAKEN
            self.page.gate_clicks += 1
            if self.page.gate_error:
                raise RuntimeError("synthetic gate transport failure")
            self.page.media_loaded = self.page.media_after_gate
            await self.page.send(self.page.seen_request)
            await asyncio.sleep(0)
            return
        assert self.kind == "button"
        self.page.clicks += 1
        self.page.unlike = self.page.optimistic_unlike
        await self.page.send(self.page.like_request)
        if self.page.duplicate_like:
            await self.page.send(like_request())
        await asyncio.sleep(0)


class Page:
    def __init__(self):
        self.url = "about:blank"
        self.guard = None
        self.listeners = {}
        self.forwarded, self.aborted, self.order = [], [], []
        self.seen_payload = OK
        self.like_payload = LIKE_OK
        self.seen_status = self.like_status = 200
        self.owner_count, self.time_count, self.auth_count, self.like_count = 2, 1, 0, 1
        self.taken_at = TAKEN
        self.media_loaded = self.button_visible = self.optimistic_unlike = True
        self.unlike = self.closed = self.close_fails = False
        self.duplicate_seen = self.duplicate_like = False
        self.seen_request = request(SEEN)
        self.like_request = like_request()
        self.initial_requests = []
        self.final_url = f"https://www.instagram.com/stories/{HANDLE}/"
        self.clicks = 0
        self.response_gate = None
        self.navigated = asyncio.Event()
        self.during_close_request = None
        self.gate_count = self.gate_clicks = 0
        self.gate_visible = self.media_after_gate = True
        self.owner_pending_reads = 0
        self.gate_error = False
        self.reply_count = self.send_count = 1
        self.reply_enabled = self.send_enabled = True
        self.reply_placeholder = f"Reply to {HANDLE}..."
        self.fills = self.reply_clicks = 0
        self.reply_text = ""
        self.on_fill = None
        self.reply_request = request(REPLY, reply_variables())
        self.reply_status = 200
        self.reply_payload = {
            "data": {
                "direct_story_share_reply_with_slide_message_response": {
                    "message_id": "fixture-message-id",
                    "timestamp_ms": str(int(NOW.timestamp() * 1000)),
                    "id": "fixture-relay-id",
                }
            }
        }
        self.duplicate_reply = False
        self.unavailable = False
        self.unavailable_visible = True
        self.media_source = "https://scontent.cdninstagram.com/story.jpg"

    async def route_web_socket(self, selector, handler):
        assert selector == "**/*"
        self.order.append("socket_guard")
        self.socket_guard = handler

    async def route(self, selector, handler):
        assert selector == "**/*"
        self.order.append("guard")
        self.guard = handler

    def on(self, event, callback):
        self.listeners[event] = callback

    def remove_listener(self, event, callback):
        assert self.listeners[event] == callback
        del self.listeners[event]

    async def send(self, req):
        route = Route(self, req)
        await self.guard(route, req)
        return route

    def respond(self, req, status, payload):
        async def body():
            if self.response_gate is not None:
                await self.response_gate.wait()
            return (
                payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            )

        self.listeners["response"](
            SimpleNamespace(url=req.url, request=req, status=status, body=body)
        )

    async def goto(self, url, **kwargs):
        assert self.guard is not None
        assert url == f"https://www.instagram.com/stories/{HANDLE}/{MEDIA}/"
        self.order.append("goto")
        self.url = self.final_url
        for req in self.initial_requests:
            await self.send(req)
        if not self.gate_count:
            await self.send(self.seen_request)
        if self.duplicate_seen:
            await self.send(request(SEEN))
        await asyncio.sleep(0)
        self.navigated.set()
        return SimpleNamespace(status=200)

    def get_by_text(self, pattern, *, exact):
        assert exact and pattern.fullmatch("This story is unavailable")
        return Locator(self, "unavailable")

    def get_by_role(self, role, *, name, exact):
        if role == "button" and name == "Send" and exact is True:
            return Locator(self, "send")
        assert role == "button" and name == "View story" and exact is False
        return Locator(self, "gate")

    def locator(self, selector):
        if selector.startswith('a[href^="/accounts/login/"]'):
            kind = "auth"
        elif selector == f'a[href="/{HANDLE}/"]:visible':
            kind = "owner"
        else:
            kind = {
                "time[datetime]:visible": "time",
                "video:visible, img:visible": "media",
                'svg[aria-label="Like"]:visible': "like",
                'svg[aria-label="Unlike"]:visible': "unlike",
                f'textarea[placeholder^="Reply to {HANDLE}"]:visible': "reply",
            }[selector]
        return Locator(self, kind)

    async def close(self, *, run_before_unload):
        assert run_before_unload is False
        self.order.append("close")
        if self.during_close_request:
            await self.send(self.during_close_request)
        if self.close_fails:
            raise RuntimeError("synthetic private close diagnostic")
        self.closed = True


def _world(*, allow_reply_measurement=False, reply_authorized=None):
    action_page = Page()
    calls = []

    async def new_page():
        calls.append("new_page")
        return action_page

    async def health():
        calls.append("preflight")
        return HealthSignal.NONE

    async def cdp_session(page):
        assert page is action_page

        async def send(method, params):
            assert method == "Network.setBypassServiceWorker" and params == {
                "bypass": True
            }
            action_page.order.append("bypass_service_workers")

        async def detach():
            action_page.order.append("detach")

        return SimpleNamespace(send=send, detach=detach)

    main = SimpleNamespace(
        url=f"https://www.instagram.com/{HANDLE}/",
        context=SimpleNamespace(new_page=new_page, new_cdp_session=cdp_session),
    )
    adapter = LiveStoryAdapter(
        main,
        object(),
        opportunity(),
        health,
        clock=lambda: NOW,
        timeout_ms=200,
        allow_reply_measurement=allow_reply_measurement,
        reply_authorized=reply_authorized,
    )
    return SimpleNamespace(adapter=adapter, main=main, page=action_page, calls=calls)


@pytest.fixture
def world():
    return _world()


@pytest.fixture
def reply_world():
    return _world(allow_reply_measurement=True)


@pytest.mark.asyncio
async def test_constructor_has_no_io_and_action_requires_preflight(world):
    assert world.calls == [] and world.page.order == []
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.calls == []
    with pytest.raises(StoryIdentityError):
        LiveStoryAdapter(
            world.main,
            object(),
            replace(opportunity(), fingerprint="a" * 64),
            lambda: None,
            clock=lambda: NOW,
        )


@pytest.mark.asyncio
async def test_view_requires_ack_and_exact_viewer_then_closes_with_guard_in_place(
    world,
):
    assert await world.adapter.detect_health_signal() is HealthSignal.NONE
    world.page.during_close_request = request(SEEN)
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.CONFIRMED
    assert world.page.order == [
        "guard",
        "socket_guard",
        "bypass_service_workers",
        "goto",
        "close",
        "detach",
    ]
    assert world.page.closed and world.page.guard is not None
    assert len(world.page.forwarded) == 1 and len(world.page.aborted) == 1
    assert world.main.url == f"https://www.instagram.com/{HANDLE}/"
    assert await world.adapter.detect_health_signal() is HealthSignal.NONE
    assert world.calls == ["preflight", "new_page"]
    assert world.adapter.ack_diagnostics[0]["confirmed"] is True


@pytest.mark.asyncio
async def test_like_arms_one_matching_native_request_only_after_seen_ack(world):
    world.page.initial_requests = [
        like_request(),
        request("UnknownMutation"),
        request("AnyInventedQuery"),
    ]
    world.page.duplicate_seen = world.page.duplicate_like = True
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.CONFIRMED
    assert world.page.clicks == 1
    assert len(world.page.forwarded) == 2
    assert world.page.forwarded[1] is world.page.like_request
    assert len(world.page.aborted) == 5
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 1


@pytest.mark.asyncio
async def test_guard_allows_only_measured_read_queries_and_denies_unknown_writes(world):
    world.page.initial_requests = [
        request("PolarisStoriesV3TrayContainerQuery", {}),
        request("PolarisProfileStoryHighlightsTrayContentQuery", {}),
        request("UnknownQuery", {}),
        request("UnknownMutation", {}),
        request(
            "PolarisStoriesV3TrayContainerQuery",
            {},
            url="https://evil.example/api/graphql",
        ),
        request(
            "PolarisStoriesV3TrayContainerQuery",
            {},
            url="https://www.instagram.com/other",
        ),
        request(SEEN, method="PUT"),
    ]
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.CONFIRMED
    assert len(world.page.forwarded) == 3 and len(world.page.aborted) == 5


@pytest.mark.asyncio
async def test_binary_post_data_is_aborted_without_breaking_story_action(world):
    class BinaryTelemetry:
        url = GRAPHQL
        method = "POST"
        resource_type = "fetch"

        @property
        def post_data(self):
            raise UnicodeDecodeError("utf-8", b"\x9c", 0, 1, "invalid start byte")

    telemetry = BinaryTelemetry()
    world.page.initial_requests = [telemetry]
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.CONFIRMED
    assert telemetry in world.page.aborted
    assert telemetry not in world.page.forwarded


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", ["owner", "reel", "media", "highlight"])
async def test_wrong_seen_identity_never_passes_guard(world, mutate):
    variables = {
        "reelId": OWNER,
        "reelMediaId": MEDIA,
        "reelMediaOwnerId": OWNER,
        "reelMediaTakenAt": int(TAKEN.timestamp()),
    }
    variables[
        {
            "owner": "reelMediaOwnerId",
            "reel": "reelId",
            "media": "reelMediaId",
            "highlight": "reelId",
        }[mutate]
    ] = "highlight:123" if mutate == "highlight" else "123"
    world.page.seen_request = request(SEEN, variables)
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.forwarded == [] and world.page.clicks == 0 and world.page.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {"result": True}},
        {
            "data": {"result": {"status": "ok"}},
            "errors": [{"message": "unknown failure"}],
        },
        {"data": {"result": {"status": "fail"}}},
        b"not json",
    ],
)
async def test_http200_or_ui_success_without_seen_ack_does_not_permit_like(
    world, payload
):
    world.page.seen_payload = payload
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 0 and world.page.closed
    assert world.adapter.ack_diagnostics[0]["confirmed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {"success": True}},
        {"data": {"result": {"status": "ok", "media_id": "other"}}},
    ],
)
async def test_optimistic_unlike_without_explicit_matching_like_ack_is_not_confirmation(
    world, payload
):
    world.page.like_payload = payload
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.unlike and world.page.clicks == 1 and world.page.closed
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes", [{"media_id": "999"}, {"actor_id": True}, {"extra": "unmeasured"}]
)
async def test_wrong_like_input_is_blocked_even_after_one_click(world, changes):
    world.page.like_request = like_request(**changes)
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert (
        world.page.clicks == 1 and len(world.page.forwarded) == 1 and world.page.closed
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attribute,value",
    [
        ("owner_count", 0),
        ("owner_count", 5),
        ("time_count", 0),
        ("time_count", 2),
        ("taken_at", TAKEN + timedelta(seconds=1)),
        ("media_loaded", False),
        ("final_url", "https://www.instagram.com/stories/other/"),
        ("like_count", 2),
        ("button_visible", False),
    ],
)
async def test_missing_ambiguous_or_wrong_viewer_never_clicks_like(
    world, attribute, value
):
    setattr(world.page, attribute, value)
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 0 and world.page.closed


@pytest.mark.asyncio
async def test_like_ack_without_stable_unlike_evidence_is_not_confirmed(world):
    world.page.optimistic_unlike = False
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,payload,signal",
    [
        (429, {}, HealthSignal.RATE_LIMIT),
        (401, {}, HealthSignal.LOGIN_REQUIRED),
        (403, {"message": "login_required"}, HealthSignal.LOGIN_REQUIRED),
        (403, {"require_login": True}, HealthSignal.LOGIN_REQUIRED),
        (403, {"message": "challenge_required"}, HealthSignal.CHALLENGE),
        (200, {"message": "challenge_required"}, HealthSignal.CHALLENGE),
        (
            200,
            {"data": {"message": "Please wait a few minutes"}},
            HealthSignal.RATE_LIMIT,
        ),
    ],
)
async def test_restriction_receipts_latch_through_close_and_do_not_recheck_main_profile(
    world, status, payload, signal
):
    world.page.seen_status, world.page.seen_payload = status, payload
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 0
    assert await world.adapter.detect_health_signal() is signal
    assert world.calls.count("preflight") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"message": "csrf_failed"}])
async def test_plain_forbidden_seen_receipt_fails_before_like_without_false_login(world, payload):
    world.page.seen_status, world.page.seen_payload = 403, payload
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 0
    assert await world.adapter.detect_health_signal() is HealthSignal.NONE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,signal",
    [
        ("/challenge/123/", HealthSignal.CHECKPOINT),
        ("/accounts/login/", HealthSignal.LOGIN_REQUIRED),
    ],
)
async def test_auth_redirect_is_detected_without_another_profile_navigation(
    world, path, signal
):
    world.page.final_url = "https://www.instagram.com" + path
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.FAILED
    assert await world.adapter.detect_health_signal() is signal


@pytest.mark.asyncio
async def test_dm_and_mismatched_action_arguments_do_not_open_or_click(world):
    await world.adapter.detect_health_signal()
    assert (
        await world.adapter.dm_about_story(HANDLE, FP, "do not send")
        is ActionOutcome.FAILED
    )
    assert await world.adapter.view_story("other", FP) is ActionOutcome.FAILED
    assert await world.adapter.like_story(HANDLE, "0" * 64) is ActionOutcome.FAILED
    assert world.page.order == [] and world.page.clicks == 0


@pytest.mark.asyncio
async def test_cancellation_closes_page_denies_queued_write_and_cleans_tasks(world):
    world.page.response_gate = asyncio.Event()
    world.page.during_close_request = like_request()
    await world.adapter.detect_health_signal()
    task = asyncio.create_task(world.adapter.view_story(HANDLE, FP))
    await world.page.navigated.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.page.closed and world.page.guard is not None
    assert len(world.page.aborted) == 1
    assert world.page.listeners == {}
    assert not world.adapter._tasks


@pytest.mark.asyncio
async def test_close_failure_keeps_guard_blocking_and_health_unavailable(world):
    world.page.close_fails = True
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.FAILED
    route = await world.page.send(like_request())
    assert route.aborted and not route.forwarded
    assert await world.adapter.detect_health_signal() is HealthSignal.HEALTH_UNAVAILABLE


def test_ack_diagnostics_contain_keys_types_and_flags_not_secret_values():
    result = _shape(
        {
            "data": {
                "status": "ok",
                "access_token": "never-print-this-secret",
                "nested": {"body": "private message"},
            }
        }
    )
    encoded = json.dumps(result)
    assert "never-print-this-secret" not in encoded and "private message" not in encoded
    assert '"status_ok": true' in encoded and '"access_token": "str"' in encoded


def test_ack_requires_exact_measured_schema_and_rejects_untyped_status():
    assert _acknowledged(OK, MEDIA, OWNER)
    assert not _acknowledged({"status": "ok"}, MEDIA, OWNER)
    assert not _acknowledged({"data": {"status": "ok", "success": False}}, MEDIA, OWNER)
    assert not _acknowledged(
        {"data": {"status": "ok", "owner_id": "other"}}, MEDIA, OWNER
    )
    assert (
        _restriction(200, {"errors": [{"message": "login_required"}]})
        is HealthSignal.LOGIN_REQUIRED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,resource,method,allowed",
    [
        ("https://www.instagram.com/static/viewer.js", "script", "GET", True),
        ("https://scontent.cdninstagram.com/image.jpg", "image", "GET", True),
        ("https://video.fbcdn.net/story.mp4", "media", "HEAD", True),
        ("https://www.instagram.com/api/v1/media/123/like/", "fetch", "GET", False),
        (
            "https://www.instagram.com/graphql/query/?doc_id=unknown",
            "fetch",
            "GET",
            False,
        ),
        ("https://evil.example/collect", "image", "GET", False),
        ("https://www.instagram.com/api/graphql", "fetch", "OPTIONS", False),
        ("https://www.instagram.com/stories/other/", "document", "GET", False),
        (
            f"https://www.instagram.com/stories/{HANDLE}/{MEDIA}/?r=1",
            "document",
            "GET",
            True,
        ),
    ],
)
async def test_read_guard_blocks_get_mutation_bypasses_and_foreign_documents(
    world, url, resource, method, allowed
):
    req = request("ignored", url=url, method=method)
    req.resource_type = resource
    world.page.initial_requests = [req]
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.CONFIRMED
    assert (req in world.page.forwarded) is allowed
    assert (req in world.page.aborted) is not allowed


@pytest.mark.asyncio
async def test_auto_advance_seen_request_latches_failure_even_if_old_dom_has_same_timestamp(
    world,
):
    await world.adapter.detect_health_signal()
    original_goto = world.page.goto

    async def goto(*args, **kwargs):
        result = await original_goto(*args, **kwargs)
        wrong = {
            "reelId": OWNER,
            "reelMediaId": "999",
            "reelMediaOwnerId": OWNER,
            "reelMediaTakenAt": int(TAKEN.timestamp()),
        }
        await world.page.send(request(SEEN, wrong))
        return result

    world.page.goto = goto
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert len(world.page.forwarded) == 1 and len(world.page.aborted) == 1
    assert world.page.clicks == 0
    assert await world.adapter.detect_health_signal() is HealthSignal.HEALTH_UNAVAILABLE


@pytest.mark.asyncio
async def test_socket_handler_closes_without_connecting_to_server(world):
    closed = []

    async def close():
        closed.append(True)

    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.CONFIRMED
    await world.page.socket_guard(SimpleNamespace(close=close))
    assert closed == [True]


@pytest.mark.asyncio
async def test_service_worker_bypass_failure_prevents_navigation(world):
    async def failed_cdp(page):
        raise RuntimeError("synthetic private transport diagnostic")

    world.main.context.new_cdp_session = failed_cdp
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.FAILED
    assert "goto" not in world.page.order
    assert world.page.closed and world.page.forwarded == []


@pytest.mark.asyncio
async def test_close_timeout_keeps_guard_and_does_not_leave_cleanup_tasks(
    world, monkeypatch
):
    async def stuck_close(*, run_before_unload):
        assert run_before_unload is False
        await asyncio.Event().wait()

    original_timeout = asyncio.timeout
    monkeypatch.setattr(
        "engagemate.worker.story_actions.asyncio.timeout",
        lambda seconds: original_timeout(0.02 if seconds == 3 else seconds),
    )
    world.page.close = stuck_close
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.FAILED
    assert await world.adapter.detect_health_signal() is HealthSignal.HEALTH_UNAVAILABLE
    queued = await world.page.send(like_request())
    assert queued.aborted
    assert not any(
        task.get_name() == "engagemate-story-action-cleanup"
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_guarded_cleanup(world):
    gate = asyncio.Event()
    closing = asyncio.Event()
    original_close = world.page.close

    async def delayed_close(*, run_before_unload):
        closing.set()
        await gate.wait()
        await original_close(run_before_unload=run_before_unload)

    world.page.close = delayed_close
    world.page.response_gate = asyncio.Event()
    await world.adapter.detect_health_signal()
    task = asyncio.create_task(world.adapter.view_story(HANDLE, FP))
    await world.page.navigated.wait()
    task.cancel()
    await closing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.page.closed and not world.adapter._tasks
    assert not any(
        task.get_name() == "engagemate-story-action-cleanup"
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_expiration_between_construction_and_action_prevents_navigation(world):
    await world.adapter.detect_health_signal()
    world.adapter._clock = lambda: TAKEN + timedelta(days=1)
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.SKIPPED
    assert world.adapter.skip_reason == "story_expired"
    assert world.page.order == []


@pytest.mark.asyncio
async def test_diagnostics_are_copied_and_cannot_mutate_saved_receipt(world):
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.CONFIRMED
    diagnostic = world.adapter.ack_diagnostics[0]
    diagnostic["confirmed"] = False
    diagnostic["shape"]["objects"].clear()
    assert world.adapter.ack_diagnostics[0]["confirmed"] is True
    assert world.adapter.ack_diagnostics[0]["shape"]["objects"]


@pytest.mark.asyncio
async def test_latched_restriction_revokes_armed_like_before_any_post_is_forwarded(
    world,
):
    await world.adapter.detect_health_signal()
    original_send = world.page.send

    async def send(req):
        if req is world.page.like_request:
            world.adapter._latch(HealthSignal.RATE_LIMIT)
        return await original_send(req)

    world.page.send = send
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert len(world.page.forwarded) == 1
    assert world.page.like_request in world.page.aborted
    assert await world.adapter.detect_health_signal() is HealthSignal.RATE_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["view_story", "like_story"])
async def test_measured_view_story_gate_is_clicked_once_with_exact_identity_under_guard(
    world, action
):
    world.page.gate_count = 1
    world.page.media_loaded = False
    await world.adapter.detect_health_signal()
    assert await getattr(world.adapter, action)(HANDLE, FP) is ActionOutcome.CONFIRMED
    assert world.page.gate_clicks == 1
    assert world.page.clicks == int(action == "like_story")
    assert world.page.closed
    assert len(world.page.forwarded) == (2 if action == "like_story" else 1)


@pytest.mark.asyncio
async def test_gate_waits_for_late_owner_links_before_click(world):
    world.page.gate_count = 1
    world.page.media_loaded = False
    world.page.owner_pending_reads = 2
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.CONFIRMED
    assert world.page.owner_pending_reads == 0
    assert world.page.gate_clicks == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("gate_count", 2),
        ("gate_visible", False),
        ("owner_count", 0),
        ("owner_count", 5),
        ("time_count", 0),
        ("time_count", 2),
        ("taken_at", TAKEN + timedelta(seconds=1)),
        ("final_url", "https://www.instagram.com/stories/other/"),
        ("final_url", f"https://www.instagram.com/stories/{HANDLE}/999/"),
        ("final_url", f"https://www.instagram.com/stories/{HANDLE}/?r=2"),
    ],
)
async def test_ambiguous_foreign_or_unverified_gate_cannot_be_clicked(
    world, field, value
):
    world.page.gate_count = 1
    world.page.media_loaded = False
    setattr(world.page, field, value)
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.gate_clicks == 0 and world.page.clicks == 0
    assert world.page.forwarded == []
    assert world.page.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["media_missing", "click_failed"])
async def test_gate_is_never_clicked_twice_when_it_remains_or_click_outcome_is_uncertain(
    world, failure
):
    world.page.gate_count = 1
    world.page.media_loaded = False
    world.page.media_after_gate = False
    world.page.gate_error = failure == "click_failed"
    await world.adapter.detect_health_signal()
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.FAILED
    assert await world.adapter.view_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.gate_clicks == 1 and world.page.closed


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "ok", "data": {}},
        {"data": {}, "extensions": {"status": "ok"}},
        {"data": {"result": None}, "extensions": {"status": "ok"}},
        {"data": {"result": {}}, "status": "ok"},
        {"data": {"result": {"extensions": {"status": "ok"}}}},
        {"data": {"status": "ok"}},
        {"data": {"result": True}, "status": "ok"},
        {"data": {"result": {"status": "fail"}}, "status": "ok"},
        {"data": {"result": {"status": "ok"}, "other": None}},
    ],
)
def test_ack_rejects_untyped_or_ambiguous_operation_result(payload):
    assert not _acknowledged(payload, MEDIA, OWNER)


@pytest.mark.asyncio
async def test_envelope_only_ack_cannot_confirm_seen_or_enable_like(world):
    world.page.gate_count = 1
    world.page.media_loaded = False
    world.page.seen_payload = {
        "status": "ok",
        "data": {},
        "extensions": {"status": "ok"},
    }
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.gate_clicks == 1 and world.page.clicks == 0
    assert world.adapter.ack_diagnostics[0]["confirmed"] is False


@pytest.mark.parametrize("typename", ["ExampleSuccess", "ExampleError"])
def test_typename_receipt_is_diagnostic_only_and_never_confirmation(typename):
    payload = {
        "data": {
            "xdt_mark_story_reel_seen": {
                "__typename": typename,
                "access_token": "private-secret",
            }
        }
    }
    assert _shape(payload)["operation_schema"] == {
        "field": "xdt_mark_story_reel_seen",
        "typename": typename,
    }
    assert "private-secret" not in json.dumps(_shape(payload))
    assert not _acknowledged(payload, MEDIA, OWNER)


@pytest.mark.parametrize(
    "typename",
    [
        None,
        True,
        42,
        {},
        [],
        "",
        "A" * 129,
        "Type\nprivate-secret",
        "https://private.example/token",
        "Týpe",
    ],
)
def test_receipt_diagnostic_rejects_non_schema_type_values(typename):
    payload = {"data": {"xdt_mark_story_reel_seen": {"__typename": typename}}}
    assert "operation_schema" not in _shape(payload)
    assert not _acknowledged(payload, MEDIA, OWNER)


def test_receipt_diagnostic_ignores_nested_and_ambiguous_typenames():
    for payload in (
        {"data": {"one": {"nested": {"__typename": "PrivateNested"}}}},
        {"data": {"one": {"__typename": "One"}, "two": {"__typename": "Two"}}},
        {"extensions": {"__typename": "Extension"}},
    ):
        assert "operation_schema" not in _shape(payload)


@pytest.mark.asyncio
async def test_measured_typename_only_seen_shape_stays_failed_without_like(world):
    world.page.seen_payload = {
        "data": {
            "xdt_mark_story_reel_seen": {
                "__typename": "UnverifiedResultType",
            }
        }
    }
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 0
    assert world.adapter.ack_diagnostics[0]["confirmed"] is False
    assert world.adapter.ack_diagnostics[0]["shape"]["operation_schema"] == {
        "field": "xdt_mark_story_reel_seen",
        "typename": "UnverifiedResultType",
    }


@pytest.mark.parametrize("operation,payload", [("seen", OK), ("like", LIKE_OK)])
def test_exact_measured_relay_result_schema_is_an_ack(operation, payload):
    assert _acknowledged(payload, MEDIA, OWNER, operation=operation)


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("seen", LIKE_OK),
        ("like", OK),
        ("unknown", OK),
        ("seen", {"data": {"result": {"status": "ok"}}}),
        (
            "like",
            {"data": {"xig_send_story_like": {"__typename": "XDTMarkSeenResponse"}}},
        ),
        ("seen", {"data": {"wrong_field": {"__typename": "XDTMarkSeenResponse"}}}),
        ("seen", {"data": {"xdt_mark_story_reel_seen": None}}),
        (
            "seen",
            {
                "data": {
                    "xdt_mark_story_reel_seen": {"__typename": "XDTMarkSeenResponse"},
                    "other": {},
                }
            },
        ),
    ],
)
def test_receipt_schema_is_operation_specific(operation, payload):
    assert not _acknowledged(payload, MEDIA, OWNER, operation=operation)


@pytest.mark.parametrize("operation,receipt", [("seen", OK), ("like", LIKE_OK)])
@pytest.mark.parametrize(
    "conflict",
    [
        {"status": "fail"},
        {"success": False},
        {"error": {"message": "failed"}},
        {"errors": [{"message": "failed"}]},
        {"media_id": "wrong"},
        {"owner_id": "wrong"},
        {"reelMediaId": "wrong"},
        {"reelId": "wrong"},
    ],
)
def test_typed_receipt_does_not_override_conflicts(operation, receipt, conflict):
    payload = json.loads(json.dumps(receipt))
    next(iter(payload["data"].values())).update(conflict)
    assert not _acknowledged(payload, MEDIA, OWNER, operation=operation)
    payload = {**receipt, **conflict}
    assert not _acknowledged(payload, MEDIA, OWNER, operation=operation)


@pytest.mark.asyncio
async def test_seen_receipt_cannot_confirm_like_even_with_unlike_dom(world):
    world.page.like_payload = OK
    await world.adapter.detect_health_signal()
    assert await world.adapter.like_story(HANDLE, FP) is ActionOutcome.FAILED
    assert world.page.clicks == 1 and world.page.unlike
    assert [item["confirmed"] for item in world.adapter.ack_diagnostics] == [
        True,
        False,
    ]


@pytest.mark.parametrize("operation", [None, True, 1, [], {}])
def test_receipt_rejects_non_string_operation(operation):
    assert not _acknowledged(OK, MEDIA, OWNER, operation=operation)


@pytest.mark.parametrize("operation,receipt", [("seen", OK), ("like", LIKE_OK)])
@pytest.mark.parametrize("location", ["root", "result", "extensions"])
def test_explicit_null_status_rejects_typed_receipt(operation, receipt, location):
    payload = json.loads(json.dumps(receipt))
    if location == "root":
        payload["status"] = None
    elif location == "result":
        next(iter(payload["data"].values()))["status"] = None
    else:
        payload["extensions"] = {"status": None}
    assert not _acknowledged(payload, MEDIA, OWNER, operation=operation)


@pytest.mark.asyncio
async def test_reply_confirms_server_acceptance_and_blocks_duplicate_request(
    reply_world,
):
    reply_world.page.duplicate_reply = True
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.CONFIRMED
    )
    assert reply_world.page.fills == reply_world.page.reply_clicks == 1
    assert sum(REPLY in r.post_data for r in reply_world.page.forwarded) == 1
    assert sum(REPLY in r.post_data for r in reply_world.page.aborted) == 1
    diagnostic = reply_world.adapter.ack_diagnostics[-1]
    assert diagnostic["operation"] == "reply" and diagnostic["confirmed"] is True
    assert diagnostic["reply_receipt"]["message_id_type"] == "str"
    assert diagnostic["reply_receipt"]["message_id_length"] == len("fixture-message-id")
    assert diagnostic["reply_controls"]["reply_empty"] is True
    assert REPLY_TEXT not in json.dumps(diagnostic)
    assert "fixture-message-id" not in json.dumps(diagnostic)
    assert reply_world.adapter._reply_text is None and reply_world.page.closed


@pytest.mark.asyncio
async def test_reply_route_requires_the_durable_follow_receipt_before_forwarding():
    checks = []

    async def authorized():
        checks.append("checked")
        return False

    world = _world(allow_reply_measurement=True, reply_authorized=authorized)
    await world.adapter.detect_health_signal()
    assert await world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT) is ActionOutcome.FAILED
    assert checks == ["checked"]
    assert not any(REPLY in request.post_data for request in world.page.forwarded)


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [None, True, 1, "", " ", "x" * 201])
async def test_invalid_reply_text_never_opens_page(reply_world, text):
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, text)
        is ActionOutcome.FAILED
    )
    assert reply_world.page.order == []


@pytest.mark.asyncio
async def test_reply_without_preflight_never_opens_page(reply_world):
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert reply_world.page.order == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attribute,value",
    [
        ("reply_count", 0),
        ("reply_count", 2),
        ("reply_enabled", False),
        ("reply_placeholder", f"Reply to {HANDLE}wrong..."),
        ("send_count", 2),
        ("send_enabled", False),
    ],
)
async def test_reply_rejects_ambiguous_disabled_or_foreign_controls(
    reply_world, attribute, value
):
    setattr(reply_world.page, attribute, value)
    await reply_world.adapter.detect_health_signal()
    expected = (
        ActionOutcome.SKIPPED
        if attribute == "reply_count" and value == 0
        else ActionOutcome.FAILED
    )
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is expected
    )
    assert reply_world.page.reply_clicks == 0
    assert not any(REPLY in r.post_data for r in reply_world.page.forwarded)


def test_reply_request_requires_measured_fields_and_preserves_native_context():
    assert _reply_variables_match(reply_variables(), OWNER, MEDIA, REPLY_TEXT)


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("data", "media_id", "wrong"),
        ("data", "media_id", int(MEDIA)),
        ("data", "reel_id", "wrong"),
        ("data", "text", {"sensitive_string_value": "wrong"}),
        ("data", "text", {"sensitive_string_value": REPLY_TEXT, "extra": True}),
        ("data", "is_shh_mode", True),
        ("data", "sampled", True),
        ("data", "reaction_emoji", "heart"),
        ("data", "share_client_context", "different"),
        ("send_data", "recipient_users", json.dumps([OWNER, "wrong"])),
        ("send_data", "recipient_users", json.dumps([int(OWNER)])),
        ("send_data", "recipient_users", "not-json"),
        ("send_data", "recipient_users", [OWNER]),
        ("send_data", "thread_id", "other-thread"),
        ("send_data", "forwarded_from_thread_id", "other-thread"),
        ("send_data", "is_forwarded_from_own_message", False),
        ("send_data", "offline_threading_id", "private\ncontext"),
        ("data", "extra", True),
        ("send_data", "extra", True),
    ],
)
@pytest.mark.asyncio
async def test_reply_guard_aborts_foreign_or_changed_native_request(
    reply_world, section, key, value
):
    variables = reply_variables()
    variables[section][key] = value
    assert not _reply_variables_match(variables, OWNER, MEDIA, REPLY_TEXT)
    reply_world.page.reply_request = request(REPLY, variables)
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert not any(REPLY in r.post_data for r in reply_world.page.forwarded)
    assert reply_world.page.reply_clicks == 1


@pytest.mark.asyncio
async def test_reply_does_not_arm_during_composition_or_after_identity_changes(
    reply_world,
):
    async def during_fill():
        await reply_world.page.send(request(REPLY, reply_variables()))
        reply_world.page.owner_count = 0

    reply_world.page.on_fill = during_fill
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.SKIPPED
    )
    assert reply_world.page.reply_clicks == 0
    assert not any(REPLY in r.post_data for r in reply_world.page.forwarded)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://www.instagram.com/graphql/query",
        GRAPHQL + "?unmeasured=1",
        GRAPHQL + "#fragment",
        GRAPHQL + "/",
        "https://other.example/api/graphql",
    ],
)
async def test_reply_requires_the_exact_measured_endpoint(reply_world, url):
    reply_world.page.reply_request = request(REPLY, reply_variables(), url=url)
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert not any(REPLY in r.post_data for r in reply_world.page.forwarded)


@pytest.mark.asyncio
async def test_reply_restriction_latches_health_and_never_retries(reply_world):
    reply_world.page.reply_status = 429
    reply_world.page.reply_payload = {"message": "please wait"}
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert await reply_world.adapter.detect_health_signal() is HealthSignal.RATE_LIMIT
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert reply_world.page.reply_clicks == 1


@pytest.mark.asyncio
async def test_reply_measurement_is_disabled_by_default_before_any_io(world):
    assert (
        await world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert world.calls == [] and world.page.order == []
    assert world.page.fills == world.page.reply_clicks == 0
    assert world.page.forwarded == []


@pytest.mark.asyncio
async def test_successful_preflight_does_not_enable_default_reply_measurement(world):
    await world.adapter.detect_health_signal()
    assert (
        await world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert world.calls == ["preflight"] and world.page.order == []
    assert world.page.fills == world.page.reply_clicks == 0


@pytest.mark.parametrize("flag", [None, 0, 1, "true", "false", [], {}])
def test_reply_measurement_requires_an_explicit_boolean(flag):
    with pytest.raises(ValueError, match="allow_reply_measurement must be a boolean"):
        _world(allow_reply_measurement=flag)


@pytest.mark.parametrize(
    "recipients",
    [
        "null",
        "true",
        "{}",
        "[]",
        json.dumps(OWNER),
        json.dumps([[OWNER]]),
        json.dumps([OWNER, OWNER]),
        json.dumps([True]),
        json.dumps([float(OWNER)]),
        json.dumps(["0" + OWNER]),
        json.dumps([OWNER + " "]),
    ],
)
def test_reply_recipient_requires_one_exact_string_id(recipients):
    variables = reply_variables()
    variables["send_data"]["recipient_users"] = recipients
    assert not _reply_variables_match(variables, OWNER, MEDIA, REPLY_TEXT)


@pytest.mark.parametrize(
    "text",
    [
        REPLY_TEXT + " ",
        " " + REPLY_TEXT,
        REPLY_TEXT.lower(),
        REPLY_TEXT + "\u200b",
        None,
        True,
        1,
        [REPLY_TEXT],
    ],
)
def test_reply_text_requires_exact_string_value(text):
    variables = reply_variables()
    variables["data"]["text"]["sensitive_string_value"] = text
    assert not _reply_variables_match(variables, OWNER, MEDIA, REPLY_TEXT)


@pytest.mark.parametrize(
    "payload", [None, True, [], {}, {"data": {}}, {"data": {}, "send_data": None}]
)
def test_reply_payload_rejects_missing_or_malformed_containers(payload):
    assert not _reply_variables_match(payload, OWNER, MEDIA, REPLY_TEXT)


def reply_receipt():
    return {
        "data": {
            "direct_story_share_reply_with_slide_message_response": {
                "message_id": "opaque-message-identifier",
                "id": "different-relay-identifier",
                "timestamp_ms": str(int(NOW.timestamp() * 1000)),
            }
        }
    }


def accepts_reply(payload, *, started=NOW, received=NOW):
    return _reply_acknowledged(
        payload, MEDIA, OWNER, attempt_started_at=started, received_at=received
    )


def test_reply_receipt_accepts_measured_fields_without_inventing_type_or_id_equality():
    assert accepts_reply(reply_receipt())
    assert accepts_reply(
        reply_receipt(),
        started=NOW - timedelta(seconds=1),
        received=NOW + timedelta(seconds=1),
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ("message_id", None),
        ("message_id", True),
        ("message_id", 123),
        ("message_id", ""),
        ("message_id", "x" * 129),
        ("message_id", "contains space"),
        ("message_id", "x\ny"),
        ("id", None),
        ("id", []),
        ("id", ""),
        ("id", "x\x7fy"),
        ("timestamp_ms", None),
        ("timestamp_ms", 1789246777000),
        ("timestamp_ms", True),
        ("timestamp_ms", "1789246777000.0"),
        ("timestamp_ms", "１７８９２４６７７７０００"),
        ("timestamp_ms", str(int(NOW.timestamp() * 1000) - 1)),
        ("timestamp_ms", str(int(NOW.timestamp() * 1000) + 1)),
    ],
)
def test_reply_receipt_rejects_invalid_or_outside_attempt_values(key, value):
    payload = reply_receipt()
    payload["data"]["direct_story_share_reply_with_slide_message_response"][key] = value
    assert not accepts_reply(payload)


@pytest.mark.parametrize(
    "started,received",
    [
        (None, NOW),
        (NOW.replace(tzinfo=None), NOW),
        (NOW, NOW.replace(tzinfo=None)),
        (NOW + timedelta(seconds=1), NOW),
        (NOW - timedelta(seconds=31), NOW),
    ],
)
def test_reply_receipt_requires_bounded_recorded_attempt(started, received):
    assert not accepts_reply(reply_receipt(), started=started, received=received)


@pytest.mark.parametrize("location", ["root", "result", "extensions"])
@pytest.mark.parametrize(
    "key,value",
    [
        ("errors", [{"message": "failed"}]),
        ("error", "failed"),
        ("success", False),
        ("status", None),
        ("status", "failed"),
        ("media_id", "wrong"),
        ("owner_id", "wrong"),
        ("reel_id", "wrong"),
        ("message", "feedback_required"),
        ("checkpoint_url", "/checkpoint/"),
    ],
)
def test_reply_receipt_rejects_errors_restrictions_and_conflicting_identity(
    location, key, value
):
    payload = reply_receipt()
    node = (
        payload
        if location == "root"
        else payload["data"]["direct_story_share_reply_with_slide_message_response"]
        if location == "result"
        else payload.setdefault("extensions", {})
    )
    node[key] = value
    assert not accepts_reply(payload)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"data": None},
        OK,
        LIKE_OK,
        {
            "data": {
                "direct_story_share_reply_with_slide_message_response": {},
                "other": {},
            }
        },
        {"data": {"direct_story_share_reply_with_slide_message_response": None}},
    ],
)
def test_reply_receipt_requires_exact_single_measured_root(payload):
    assert not accepts_reply(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{"data": {}}, OK, {"errors": [{"message": "failed"}]}]
)
async def test_cleared_composer_cannot_confirm_missing_or_wrong_reply_receipt(
    reply_world, payload
):
    reply_world.page.reply_payload = payload
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert reply_world.page.reply_text == "" and reply_world.page.reply_clicks == 1


@pytest.mark.asyncio
async def test_unrelated_successful_reply_response_cannot_confirm_allowed_request(
    reply_world,
):
    original_respond = reply_world.page.respond

    def unrelated(req, status, payload):
        if REPLY in req.post_data:
            req = request(REPLY, reply_variables())
        return original_respond(req, status, payload)

    reply_world.page.respond = unrelated
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert reply_world.page.reply_clicks == 1
    assert "reply" not in reply_world.adapter._receipts


@pytest.mark.asyncio
async def test_reply_acceptance_does_not_require_composer_readback(
    reply_world, monkeypatch
):
    original = Locator.input_value

    async def value(locator):
        if locator.page.reply_clicks:
            raise RuntimeError("composer detached after accepted send")
        return await original(locator)

    monkeypatch.setattr(Locator, "input_value", value)
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.CONFIRMED
    )
    assert reply_world.adapter.ack_diagnostics[-1]["reply_controls"] == {
        "unavailable": True
    }


@pytest.mark.parametrize("kind", ["deep", "wide"])
def test_reply_receipt_rejects_excessive_nested_metadata(kind):
    payload = reply_receipt()
    if kind == "wide":
        payload["extensions"] = [{} for _ in range(257)]
    else:
        node = payload
        for _ in range(10):
            node["nested"] = {}
            node = node["nested"]
    assert not accepts_reply(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
async def test_valid_reply_receipt_cannot_override_http_failure(reply_world, status):
    reply_world.page.reply_status = status
    await reply_world.adapter.detect_health_signal()
    assert (
        await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
        is ActionOutcome.FAILED
    )
    assert reply_world.page.reply_clicks == 1


@pytest.mark.parametrize("location", ["root", "result", "extensions"])
@pytest.mark.parametrize("value", [0, None, "false", [], {}, 1, "true"])
def test_reply_receipt_rejects_nonboolean_explicit_success(location, value):
    payload = reply_receipt()
    node = (
        payload
        if location == "root"
        else payload["data"]["direct_story_share_reply_with_slide_message_response"]
        if location == "result"
        else payload.setdefault("extensions", {})
    )
    node["success"] = value
    assert not accepts_reply(payload)


@pytest.mark.parametrize("location", ["root", "result", "extensions"])
def test_reply_receipt_accepts_literal_true_success(location):
    payload = reply_receipt()
    node = (
        payload
        if location == "root"
        else payload["data"]["direct_story_share_reply_with_slide_message_response"]
        if location == "result"
        else payload.setdefault("extensions", {})
    )
    node["success"] = True
    assert accepts_reply(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_count,payload,expected", [
    (0, None, "request=not_forwarded,receipt=missing,http=0"),
    (1, {}, "request=forwarded,receipt=rejected,http=200"),
    (1, None, "request=forwarded,receipt=accepted,http=200"),
])
async def test_action_evidence_survives_cleanup_without_private_values(
    reply_world, reply_count, payload, expected
):
    reply_world.page.reply_count = reply_count
    if payload is not None:
        reply_world.page.reply_payload = payload
    await reply_world.adapter.detect_health_signal()
    await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
    evidence = reply_world.adapter.action_evidence
    assert expected in evidence
    assert evidence.startswith("action=reply,stage=")
    assert len(evidence) <= 160
    assert REPLY_TEXT not in evidence and HANDLE not in evidence
    assert "fixture-message-id" not in evidence
    assert reply_world.page.closed


@pytest.mark.asyncio
async def test_evidence_reports_lost_viewer_health_instead_of_stale_preflight(reply_world):
    await reply_world.adapter.detect_health_signal()
    await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT)
    reply_world.adapter._viewer_healthy = False
    assert reply_world.adapter.action_evidence.endswith("health=health_unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize("action,phase,change,expected", [
    (action, phase, change, expected)
    for action in ("like_story", "reply_to_story")
    for phase, change, expected in [
        ("initial", "unavailable", ActionOutcome.SKIPPED),
        ("initial", "placeholder", ActionOutcome.SKIPPED),
        ("initial", "hidden", ActionOutcome.CONFIRMED),
        ("initial", "auth", ActionOutcome.FAILED),
        ("send", "unavailable", ActionOutcome.SKIPPED),
        ("send", "unloaded", ActionOutcome.SKIPPED),
        ("send", "identity", ActionOutcome.SKIPPED),
        ("send", "expiry", ActionOutcome.SKIPPED),
        ("guard", "expiry", ActionOutcome.SKIPPED),
        ("fill", "unloaded", ActionOutcome.SKIPPED),
        ("fill", "expiry", ActionOutcome.SKIPPED),
        ("after", "accepted", ActionOutcome.CONFIRMED),
        ("after", "rejected", ActionOutcome.FAILED),
        ("receipt_wait", "expiry", ActionOutcome.SKIPPED),
    ] if action == "reply_to_story" or phase not in {"fill", "after", "receipt_wait"}
])
async def test_availability_at_each_send_boundary(reply_world, action, phase, change, expected):
    world = reply_world
    selected = world.page.reply_request if action == "reply_to_story" else world.page.like_request
    original_send, original_matches = world.page.send, world.adapter._viewer_matches

    async def mutate():
        if change in {"unavailable", "hidden", "auth", "accepted", "rejected"}:
            world.page.unavailable = True
            world.page.unavailable_visible = change != "hidden"
            world.page.auth_count = int(change == "auth")
        elif change == "placeholder":
            world.page.media_source = "https://static.cdninstagram.com/story_unavailable.png"
        elif change == "unloaded":
            world.page.media_loaded = False
        elif change == "identity":
            world.page.taken_at = TAKEN + timedelta(seconds=1)
        else:
            world.adapter._clock = lambda: TAKEN + timedelta(days=1)

    async def send(req):
        if req is selected and phase == "send":
            await mutate()
        result = await original_send(req)
        if req is selected and phase == "after":
            await mutate()
        return result

    async def matches():
        result = await original_matches()
        if phase == "guard" and (world.adapter._reply_armed or world.adapter._like_armed):
            await mutate()
        return result

    world.page.send, world.adapter._viewer_matches = send, matches
    if phase == "initial":
        await mutate()
    if phase == "fill":
        world.page.on_fill = mutate
    if change == "rejected":
        world.page.reply_payload = {}
    if phase == "receipt_wait":
        world.page.response_gate = asyncio.Event()
    await world.adapter.detect_health_signal()
    args = (HANDLE, FP, REPLY_TEXT) if action == "reply_to_story" else (HANDLE, FP)
    task = asyncio.create_task(getattr(world.adapter, action)(*args))
    if phase == "receipt_wait":
        await world.page.navigated.wait()
        await mutate()
        assert await task is expected
    else:
        async with asyncio.timeout(0.1):  # Refused writes wake the receipt wait.
            assert await task is expected
    skipped = expected is ActionOutcome.SKIPPED
    assert world.adapter.skip_reason == (
        ("story_expired" if change == "expiry" else "story_unavailable") if skipped else None
    )
    assert (selected in world.page.forwarded) is (phase == "after" or expected is ActionOutcome.CONFIRMED)
    if skipped:
        assert world.adapter._signal is None
    if change == "auth":
        assert await world.adapter.detect_health_signal() is HealthSignal.LOGIN_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["like_story", "reply_to_story"])
async def test_concurrent_duplicate_native_requests_only_forward_once(reply_world, action):
    world = reply_world
    original_send = world.page.send
    original_matches = world.adapter._viewer_matches
    selected = world.page.reply_request if action == "reply_to_story" else world.page.like_request
    arrivals = 0
    barrier = asyncio.Event()

    async def matches():
        nonlocal arrivals
        if world.adapter._reply_armed or world.adapter._like_armed:
            arrivals += 1
            if arrivals == 2:
                barrier.set()
            await barrier.wait()
        return await original_matches()

    async def send(req):
        if req is selected:
            return (await asyncio.gather(original_send(req), original_send(req)))[0]
        return await original_send(req)

    world.adapter._viewer_matches = matches
    world.page.send = send
    await world.adapter.detect_health_signal()
    args = (HANDLE, FP, REPLY_TEXT) if action == "reply_to_story" else (HANDLE, FP)
    assert await getattr(world.adapter, action)(*args) is ActionOutcome.CONFIRMED
    assert sum(req is selected for req in world.page.forwarded) == 1
    assert sum(req is selected for req in world.page.aborted) == 1


@pytest.mark.asyncio
async def test_expired_at_construction_is_validated_and_skipped_without_navigation(world):
    async def health():
        return HealthSignal.NONE

    adapter = LiveStoryAdapter(
        world.main, object(), opportunity(), health,
        clock=lambda: TAKEN + timedelta(days=1), allow_reply_measurement=True,
    )
    await adapter.detect_health_signal()
    assert await adapter.reply_to_story(HANDLE, FP, REPLY_TEXT) is ActionOutcome.SKIPPED
    assert adapter.skip_reason == "story_expired" and world.page.order == []
    with pytest.raises(StoryIdentityError):
        LiveStoryAdapter(
            world.main, object(), replace(opportunity(), fingerprint="a" * 64), health,
            clock=lambda: TAKEN + timedelta(days=1),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {}, {"reelMediaOwnerId": "123"}, {"reelMediaId": "123"},
    {"reelMediaTakenAt": int(TAKEN.timestamp()) + 86_401}, {"reelMediaId": None},
])
async def test_seen_expiry_requires_valid_identity(reply_world, changes):
    world = reply_world
    original_send = world.page.send

    async def send(req):
        if req is world.page.seen_request:
            world.adapter._clock = lambda: TAKEN + timedelta(days=1)
            req = request(SEEN, {
                "reelId": OWNER, "reelMediaOwnerId": OWNER,
                "reelMediaId": MEDIA, "reelMediaTakenAt": int(TAKEN.timestamp()), **changes,
            })
        return await original_send(req)

    world.page.send = send
    await world.adapter.detect_health_signal()
    expected = ActionOutcome.FAILED if changes else ActionOutcome.SKIPPED
    assert await world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT) is expected
    assert world.adapter.skip_reason == (None if changes else "story_expired")
    assert world.adapter._signal is (HealthSignal.HEALTH_UNAVAILABLE if changes else None)
    assert world.page.forwarded == [] and world.page.reply_clicks == 0


@pytest.mark.parametrize('failure', ['raise', 'timeout'])
async def test_reply_receipt_survives_page_cleanup_and_persists_sent(reply_world, monkeypatch, failure):
    from engagemate.worker.executor import (
        ExecutionContext,
        ExecutionResultStatus,
        Executor,
    )

    from tests.worker.test_executor import ACCOUNT, USER, Close, Repo, decision
    if failure == 'raise':
        reply_world.page.close_fails = True
    else:
        async def stuck_close(**kwargs):
            await asyncio.Event().wait()
        reply_world.page.close = stuck_close
        original_timeout = asyncio.timeout
        monkeypatch.setattr(asyncio, 'timeout', lambda seconds: original_timeout(0.01 if seconds == 3 else seconds))
    repo, close = Repo(), Close()
    result = await Executor(repo, reply_world.adapter).execute(
        opportunity(), decision('reply').model_copy(update={'generated_text': REPLY_TEXT}),
        ExecutionContext(USER, ACCOUNT, NOW, close))
    assert result.status is ExecutionResultStatus.SENT
    assert repo.calls == ['claim', 'sent', 'breaker']
    assert reply_world.adapter.confirmed_before_cleanup is True
    assert reply_world.adapter.action_evidence.endswith('cleanup=failed')
    assert await reply_world.adapter.detect_health_signal() is HealthSignal.HEALTH_UNAVAILABLE
    blocked = await reply_world.page.send(like_request())
    assert blocked.aborted and not blocked.forwarded
    assert close.calls == 1


@pytest.mark.parametrize("through_mcp", [False, True])
async def test_cancel_during_reply_cleanup_persists_receipt_and_propagates(reply_world, monkeypatch, through_mcp):
    from engagemate.worker.executor import ExecutionContext, Executor

    from tests.worker.test_executor import ACCOUNT, USER, Close, Repo, decision
    closing = asyncio.Event()
    release = asyncio.Event()
    original = reply_world.page.close
    async def held_close(**kwargs):
        closing.set()
        await release.wait()
        await original(**kwargs)
    reply_world.page.close = held_close
    repo = Repo()
    executor = Executor(repo, reply_world.adapter)
    context = ExecutionContext(USER, ACCOUNT, NOW, Close())
    async def perform():
        if not through_mcp:
            return await executor.execute(
                opportunity(), decision('reply').model_copy(update={'generated_text': REPLY_TEXT}), context)
        from engagemate.worker import execution_mcp_tools
        from engagemate.worker.active_run import ActiveRunRegistry

        from tests.worker.test_execution_mcp_tools import _active_run
        nonlocal run
        run = _active_run()
        run.executor = executor
        run.execution_context = context
        opaque = uuid4()
        run.opportunities[opaque] = opportunity()
        monkeypatch.setattr(execution_mcp_tools, '_story_reply_grounded', lambda *args: True)
        registry = ActiveRunRegistry()
        with registry.activate(run):
            await execution_mcp_tools._mutate(
                registry, str(opaque), 'receipt_cancel_fixture', 'reply', REPLY_TEXT, 'detail')
    run = None
    task = asyncio.create_task(perform())
    await closing.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError) as interrupted:
        await task
    assert interrupted.value._engagemate_story_result.status.value == 'sent'
    if through_mcp:
        assert run.tool_repository.finished[0]['safe_result'] == {'status': 'confirmed'}
    assert repo.calls == ['claim', 'sent']
    assert reply_world.adapter.confirmed_before_cleanup is True


async def test_cleanup_error_cannot_upgrade_unmatched_reply_receipt(reply_world):
    reply_world.page.reply_payload = {'data': {}}
    reply_world.page.close_fails = True
    await reply_world.adapter.detect_health_signal()
    assert await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT) is ActionOutcome.FAILED
    assert reply_world.adapter.confirmed_before_cleanup is False
    assert reply_world.adapter.action_evidence.endswith('cleanup=failed')


async def test_late_cleanup_exception_preserves_confirmed_reply(reply_world):
    async def broken_cleanup():
        raise RuntimeError('private cleanup detail')
    reply_world.adapter._cleanup = broken_cleanup
    await reply_world.adapter.detect_health_signal()
    assert await reply_world.adapter.reply_to_story(HANDLE, FP, REPLY_TEXT) is ActionOutcome.CONFIRMED
    assert reply_world.adapter.confirmed_before_cleanup is True
    assert 'private' not in reply_world.adapter.action_evidence
    blocked = await reply_world.page.send(like_request())
    assert blocked.aborted and not blocked.forwarded
