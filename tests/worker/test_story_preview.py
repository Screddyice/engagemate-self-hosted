from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from engagemate.worker.story_preview import ReadOnlyStoryBrowser, StoryPreviewError
from engagemate.worker.story_types import (
    DiscoveryContext,
    DiscoveryTarget,
    StoryMediaType,
)

OWNER = "12345678901"
MEDIA = "3000000000000000870"
HANDLE = "sample.viewer25"
TAKEN = datetime.fromtimestamp(1789246777, UTC)
NOW = TAKEN + timedelta(minutes=2)
URL = f"https://www.instagram.com/stories/{HANDLE}/"
GRAPHQL = "https://www.instagram.com/api/graphql"
SEEN = "PolarisStoriesV3SeenMutation"
JPEG = b"\xff\xd8\xffactual-element-screenshot\xff\xd9"


def request(
    name=None, *, method="POST", url=GRAPHQL, variables=None, resource_type="fetch"
):
    body = (
        urlencode(
            {"fb_api_req_friendly_name": name, "variables": json.dumps(variables or {})}
        )
        if name
        else "unknown=write"
    )
    return SimpleNamespace(
        method=method, url=url, post_data=body, resource_type=resource_type
    )


def seen(**changes):
    variables = {
        "reelId": OWNER,
        "reelMediaId": MEDIA,
        "reelMediaOwnerId": OWNER,
        "reelMediaTakenAt": int(TAKEN.timestamp()),
    }
    variables.update(changes)
    return request(SEEN, variables=variables)


def snapshot():
    return {
        "url": URL,
        "ownerLinks": 2,
        "auth": False,
        "restricted": False,
        "times": [TAKEN.isoformat()],
        "media": [
            {
                "index": 4,
                "tag": "VIDEO",
                "visible": True,
                "width": 583,
                "height": 1037,
                "intrinsicWidth": 1080,
                "intrinsicHeight": 1920,
                "source": "https://scontent.cdninstagram.com/actual-story.jpg",
                "ready": 4,
            }
        ],
    }


class Route:
    def __init__(self, request, events):
        self.request = request
        self.events = events
        self.actions = []

    async def abort(self):
        self.actions.append("abort")
        self.events.append("abort")

    async def continue_(self):
        self.actions.append("continue")
        self.events.append("continue")


class StatusMatches:
    def __init__(self, matches):
        self.matches = matches

    async def count(self):
        return len(self.matches)

    def nth(self, index):
        return StatusMatches([self.matches[index]])

    async def is_visible(self):
        return self.matches[0][1]


class Page:
    def __init__(self):
        self.events = []
        self.url = "about:blank"
        self.guard = None
        self.requests = [seen()]
        self.routes = []
        self.state = snapshot()
        self.jpeg = JPEG
        self.after_screenshot = None
        self.on_close = None
        self.close_error = False
        self.closed = False
        self.socket_supported = True
        self.status = 200
        self.navigation_error = False
        self.gate_count = 0
        self.gate_clicks = 0
        self.on_gate = None

    def on(self, event, handler):
        assert event == "response"
        self.response_handler = handler

    async def route(self, pattern, handler):
        assert pattern == "**/*"
        self.guard = handler
        self.events.append("guard")

    async def route_web_socket(self, pattern, handler):
        if not self.socket_supported:
            raise AttributeError("unavailable")
        self.events.append("websocket_guard")
        self.socket_handler = handler

    async def emit(self, req):
        assert self.guard is not None
        route = Route(req, self.events)
        self.routes.append(route)
        await self.guard(route)
        return route

    async def goto(self, url, **kwargs):
        assert self.events[:4] == ["guard", "websocket_guard", "cdp", "bypass"]
        self.events.append("goto")
        self.url = url
        if self.navigation_error:
            raise RuntimeError("private browser diagnostic")
        for req in self.requests:
            await self.emit(req)
        return SimpleNamespace(status=self.status, url=self.url)

    async def evaluate(self, script, args):
        assert args == {"handle": HANDLE}
        assert "time[datetime]" in script
        self.events.append("evaluate")
        return copy.deepcopy(self.state)

    def get_by_role(self, role, *, name, exact):
        assert (role, name, exact) == ("button", "View story", False)
        return self

    def get_by_text(self, pattern, *, exact):
        assert exact is True
        return StatusMatches([
            (text, visible) for text, visible in self.state.get("statuses", [])
            if pattern.fullmatch(text)
        ])

    async def count(self):
        return self.gate_count

    async def is_visible(self):
        return self.gate_count == 1

    async def click(self, *, timeout):
        assert timeout == 3000 and self.guard is not None
        self.gate_clicks += 1
        if self.on_gate:
            await self.on_gate(self)

    def locator(self, selector):
        assert selector == "video, img"
        return self

    def nth(self, index):
        assert index == self.state["media"][0]["index"]
        self.events.append("actual_media_locator")
        return self

    async def screenshot(self, **kwargs):
        assert kwargs == {"type": "jpeg", "quality": 75, "timeout": 3000}
        self.events.append("screenshot")
        if self.after_screenshot:
            await self.after_screenshot(self)
        return self.jpeg

    async def close(self, **kwargs):
        assert kwargs == {"run_before_unload": False}
        assert self.guard is not None
        self.events.append("close")
        if self.on_close:
            await self.on_close(self)
        if self.close_error:
            raise RuntimeError("close failed")
        self.closed = True

    async def unroute(self, pattern, handler):
        assert self.closed and self.guard is handler
        self.events.append("unroute")
        self.guard = None


class BrowserContext:
    def __init__(self):
        self.page = Page()
        self.main_profile_page = object()
        self.new_pages = 0
        self.cdp_supported = True

    async def new_page(self):
        self.new_pages += 1
        return self.page

    async def new_cdp_session(self, page):
        assert page is self.page
        if not self.cdp_supported:
            raise RuntimeError("CDP unavailable")
        page.events.append("cdp")
        return self

    async def send(self, method, params):
        assert method == "Network.setBypassServiceWorker" and params == {"bypass": True}
        self.page.events.append("bypass")

    async def detach(self):
        assert self.page.closed
        self.page.events.append("detach")


@pytest.fixture
def fixture():
    ctx = DiscoveryContext(uuid4(), uuid4(), NOW)
    target = DiscoveryTarget(
        uuid4(), ctx.user_id, ctx.ig_account_id, OWNER, HANDLE, "discovery", True
    )
    browser = BrowserContext()
    return browser, ctx, target


async def preview(fixture):
    browser, ctx, target = fixture
    return await ReadOnlyStoryBrowser(browser, timeout_seconds=0.15).active_stories(
        ctx, target
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["VIDEO", "IMG"])
async def test_returns_actual_single_media_and_blocks_seen_through_page_close(
    fixture, tag
):
    browser, _, target = fixture
    page = browser.page
    page.state["media"][0]["tag"] = tag
    page.on_close = lambda page: page.emit(seen())
    stories = await preview(fixture)
    assert len(stories) == 1
    story = stories[0]
    assert (
        story.target_id,
        story.owner_id,
        story.media_id,
        story.taken_at,
        story.media_hash,
    ) == (target.id, OWNER, MEDIA, TAKEN, None)
    assert story.content.caption_text is None and story.content.sticker_text is None
    if tag == "VIDEO":
        assert story.content.media_type is StoryMediaType.VIDEO
        assert story.content.image is None
        assert len(story.content.video_frames) == 1
        assert story.content.video_frames[0].data == JPEG
        assert story.content.video_frames[0].mime_type == "image/jpeg"
    else:
        assert story.content.media_type is StoryMediaType.IMAGE
        assert story.content.image.data == JPEG
        assert story.content.image.mime_type == "image/jpeg"
        assert story.content.video_frames == ()
    assert [route.actions for route in page.routes] == [["abort"], ["abort"]]
    assert (
        page.events.index("close")
        < page.events.index("unroute")
        < page.events.index("detach")
    )
    assert browser.new_pages == 1
    assert page.events.count("screenshot") == 1


@pytest.mark.asyncio
async def test_only_measured_queries_and_read_resources_escape_guard(fixture):
    page = fixture[0].page
    page.requests += [
        request("PolarisStoriesV3TrayContainerQuery"),
        request("PolarisProfileStoryHighlightsTrayContentQuery"),
        request("InventedDangerousQuery"),
        request("usePolarisStoriesV4LikeMutationLikeMutation"),
        request(),
        request(method="PUT"),
        request(method="DELETE"),
        request(
            method="GET",
            url="https://scontent.cdninstagram.com/frame.jpg",
            resource_type="image",
        ),
        request(method="GET", url=URL, resource_type="document"),
        request(method="GET", url="https://www.instagram.com/api/v1/media/123/seen/"),
        request(
            "PolarisStoriesV3TrayContainerQuery",
            url="https://external.example/api/graphql",
        ),
        request(
            method="GET", url="https://external.example/pixel", resource_type="image"
        ),
    ]
    await preview(fixture)
    assert [route.actions for route in page.routes] == [
        [action]
        for action in [
            "abort",
            "continue",
            "continue",
            "abort",
            "abort",
            "abort",
            "abort",
            "abort",
            "continue",
            "continue",
            "abort",
            "abort",
            "abort",
        ]
    ]


@pytest.mark.asyncio
async def test_websocket_never_connected(fixture):
    page = fixture[0].page
    closed = []

    async def close():
        closed.append(True)

    async def during_close(page):
        await page.socket_handler(SimpleNamespace(close=close))

    page.on_close = during_close
    await preview(fixture)
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,reason",
    [
        ({"ownerLinks": 0}, "timeout"),
        ({"times": []}, "timeout"),
        ({"times": [NOW.isoformat()]}, "identity_mismatch"),
        ({"times": [TAKEN.isoformat(), TAKEN.isoformat()]}, "ambiguous"),
        ({"auth": True}, "authentication_required"),
        ({"restricted": True}, "rate_limited"),
        ({"url": "https://www.instagram.com/checkpoint/"}, "checkpoint_required"),
        ({"url": "https://www.instagram.com/stories/wrong/"}, "identity_mismatch"),
    ],
)
async def test_rejects_unverified_owner_time_and_restrictions(fixture, change, reason):
    page = fixture[0].page
    page.state.update(change)
    with pytest.raises(StoryPreviewError, match=reason):
        await preview(fixture)
    assert "screenshot" not in page.events
    assert page.closed and page.guard is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", [{"ready": 1}, {"width": 200}, {"height": 100}, {"width": float("nan")}]
)
async def test_media_must_be_loaded_and_large(fixture, change):
    page = fixture[0].page
    page.state["media"][0].update(change)
    with pytest.raises(StoryPreviewError, match="story_preview_timeout"):
        await preview(fixture)
    assert "screenshot" not in page.events
    assert page.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["IMG", "VIDEO"])
@pytest.mark.parametrize("dimension,value", [
    ("intrinsicWidth", 0), ("intrinsicHeight", 0),
    ("intrinsicWidth", 1), ("intrinsicHeight", float("nan")),
])
async def test_css_size_and_ready_state_do_not_replace_decoded_dimensions(
    fixture, tag, dimension, value
):
    from engagemate.worker.story_preview import _CENSUS

    assert "el.videoWidth" in _CENSUS and "el.videoHeight" in _CENSUS
    assert "el.naturalWidth" in _CENSUS and "el.naturalHeight" in _CENSUS
    page = fixture[0].page
    page.state["media"][0].update(tag=tag, ready=2, **{dimension: value})
    with pytest.raises(StoryPreviewError, match="^story_preview_timeout$"):
        await preview(fixture)
    assert "screenshot" not in page.events and page.closed


@pytest.mark.asyncio
async def test_multiple_media_is_ambiguous(fixture):
    page = fixture[0].page
    page.state["media"].append(dict(page.state["media"][0], index=5))
    with pytest.raises(StoryPreviewError, match="story_preview_ambiguous"):
        await preview(fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["navigation", "screenshot", "close"])
async def test_conflicting_seen_identity_is_blocked_and_discards_capture(fixture, when):
    page = fixture[0].page
    other = seen(reelMediaId="3000000000000000871")
    if when == "navigation":
        page.requests.append(other)
    elif when == "screenshot":
        page.after_screenshot = lambda page: page.emit(other)
    else:
        page.on_close = lambda page: page.emit(other)
    with pytest.raises(StoryPreviewError, match="story_preview_auto_advanced"):
        await preview(fixture)
    assert all(route.actions == ["abort"] for route in page.routes)
    assert page.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"source": "https://scontent.cdninstagram.com/other.jpg"}, {"index": 8}])
async def test_dom_media_change_during_screenshot_discards_capture(fixture, change):
    page = fixture[0].page

    async def mutate(page):
        page.state["media"][0].update(change)

    page.after_screenshot = mutate
    with pytest.raises(StoryPreviewError, match="story_preview_auto_advanced"):
        await preview(fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    "This story is unavailable", "This story is no longer available.",
    "This story has expired", "This story has been deleted",
])
async def test_loaded_large_placeholder_never_becomes_an_opportunity(fixture, status):
    page = fixture[0].page
    page.state["statuses"] = [(status, True)]
    with pytest.raises(StoryPreviewError, match="^story_unavailable$"):
        await preview(fixture)
    assert "screenshot" not in page.events
    assert page.gate_clicks == 0 and page.closed
    assert all(route.actions == ["abort"] for route in page.routes)


@pytest.mark.asyncio
async def test_unavailable_story_gate_is_not_clicked(fixture):
    page = fixture[0].page
    page.requests = []
    page.gate_count = 1
    page.state["statuses"] = [("This story is unavailable", True)]
    with pytest.raises(StoryPreviewError, match="^story_unavailable$"):
        await preview(fixture)
    assert page.gate_clicks == 0 and "screenshot" not in page.events


@pytest.mark.asyncio
async def test_story_becoming_unavailable_before_gate_click_is_rechecked(fixture):
    page = fixture[0].page
    page.requests, page.gate_count = [], 1
    original_evaluate = page.evaluate

    async def changed(script, args):
        if page.events.count("evaluate") == 1:
            page.state["statuses"] = [("This story is unavailable", True)]
        return await original_evaluate(script, args)

    page.evaluate = changed
    with pytest.raises(StoryPreviewError, match="^story_unavailable$"):
        await preview(fixture)
    assert page.events.count("evaluate") == 2 and page.gate_clicks == 0


@pytest.mark.asyncio
async def test_unavailable_after_capture_discards_media_without_writes(fixture):
    page = fixture[0].page

    async def unavailable(page):
        page.state["statuses"] = [("This story is unavailable", True)]

    page.after_screenshot = unavailable
    with pytest.raises(StoryPreviewError, match="^story_unavailable$"):
        await preview(fixture)
    assert page.events.count("screenshot") == 1
    assert all(route.actions == ["abort"] for route in page.routes)
    assert page.closed


@pytest.mark.asyncio
async def test_hidden_status_and_unavailable_caption_are_not_native_error(fixture):
    fixture[0].page.state["statuses"] = [
        ("This story is unavailable", False), ("Tickets unavailable until Friday", True)
    ]
    assert len(await preview(fixture)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("flag,reason", [
    ("auth", "authentication_required"), ("checkpoint", "checkpoint_required"),
    ("rateLimited", "rate_limited"),
])
async def test_account_restriction_precedes_unavailable_story(fixture, flag, reason):
    page = fixture[0].page
    page.state[flag] = True
    page.state["statuses"] = [("This story is unavailable", True)]
    with pytest.raises(StoryPreviewError, match=f"^{reason}$"):
        await preview(fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["story_unavailable.png", "deleted.jpg", "expired.png"])
@pytest.mark.parametrize("ready", [0, 2])
async def test_explicit_error_asset_is_an_unavailable_story(fixture, path, ready):
    page = fixture[0].page
    page.state["media"][0].update(
        tag="IMG", source=f"https://scontent.cdninstagram.com/{path}", ready=ready
    )
    with pytest.raises(StoryPreviewError, match="^story_unavailable$"):
        await preview(fixture)
    assert "screenshot" not in page.events
    assert all(route.actions == ["abort"] for route in page.routes)


@pytest.mark.asyncio
async def test_unknown_media_source_remains_unreadable(fixture):
    page = fixture[0].page
    page.state["media"][0]["source"] = "https://untrusted.example/image.jpg"
    with pytest.raises(StoryPreviewError, match="^story_preview_media_unreadable$"):
        await preview(fixture)
    assert "screenshot" not in page.events


@pytest.mark.asyncio
async def test_ready_state_progress_does_not_change_media_identity(fixture):
    page = fixture[0].page
    page.state["media"][0]["ready"] = 2

    async def loaded(page):
        page.state["media"][0]["ready"] = 4

    page.after_screenshot = loaded
    assert len(await preview(fixture)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_request",
    [
        seen(reelMediaOwnerId="999"),
        request(SEEN, url="https://external.example/api/graphql"),
        request(SEEN),
    ],
)
async def test_seen_identity_must_be_valid_and_from_instagram_graphql(
    fixture, bad_request
):
    page = fixture[0].page
    page.requests = [bad_request]
    with pytest.raises(StoryPreviewError, match="story_preview_identity_mismatch"):
        await preview(fixture)
    assert page.routes[0].actions == ["abort"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["websocket", "cdp", "navigation"])
async def test_guard_setup_and_navigation_errors_fail_closed(fixture, failure):
    browser = fixture[0]
    page = browser.page
    if failure == "websocket":
        page.socket_supported = False
    elif failure == "cdp":
        browser.cdp_supported = False
    else:
        page.navigation_error = True
    with pytest.raises(StoryPreviewError, match="^story_preview_unavailable$"):
        await preview(fixture)
    assert page.closed and page.guard is None
    if failure != "navigation":
        assert "goto" not in page.events


@pytest.mark.asyncio
async def test_close_failure_retains_guard(fixture):
    page = fixture[0].page
    page.close_error = True
    with pytest.raises(StoryPreviewError, match="story_preview_close_failed"):
        await preview(fixture)
    assert page.guard is not None and "unroute" not in page.events
    assert (await page.emit(seen())).actions == ["abort"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not a JPEG",
        b"\x89PNG\r\n\x1a\nwrong-format",
        JPEG[:-2],
        b"\xff\xd8\xff\xff\xd9",
        JPEG[:-2] + b"x" * (512 * 1024 + 1 - len(JPEG)) + JPEG[-2:],
    ],
)
async def test_invalid_or_oversized_capture_is_rejected(fixture, data):
    page = fixture[0].page
    page.jpeg = data
    with pytest.raises(StoryPreviewError, match="story_preview_media_unreadable"):
        await preview(fixture)
    assert page.closed


@pytest.mark.asyncio
async def test_disabled_and_foreign_targets_never_open_page(fixture):
    browser, ctx, target = fixture
    adapter = ReadOnlyStoryBrowser(browser)
    assert await adapter.active_stories(ctx, replace(target, enabled=False)) == ()
    with pytest.raises(StoryPreviewError, match="story_preview_invalid_target"):
        await adapter.active_stories(ctx, replace(target, user_id=uuid4()))
    assert browser.new_pages == 0


@pytest.mark.asyncio
async def test_binary_telemetry_is_aborted_without_invalidating_media(fixture):
    class BinaryRequest:
        method = "POST"
        url = "https://www.instagram.com/ajax/bz/"
        resource_type = "fetch"

        @property
        def post_data(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "binary telemetry")

    page = fixture[0].page
    page.requests.append(BinaryRequest())
    assert len(await preview(fixture)) == 1
    assert page.routes[-1].actions == ["abort"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, reason",
    [
        (401, "authentication_required"),
        (403, "story_preview_request_refused"),
        (429, "rate_limited"),
    ],
)
@pytest.mark.parametrize("when", ["navigation", "screenshot", "close"])
async def test_restriction_responses_latch_canonical_account_pause_reason(
    fixture, status, reason, when
):
    page = fixture[0].page

    async def restricted(page):
        page.response_handler(SimpleNamespace(url=GRAPHQL, status=status))

    if when == "navigation":
        page.status = status
    elif when == "screenshot":
        page.after_screenshot = restricted
    else:
        page.on_close = restricted
    with pytest.raises(StoryPreviewError, match=f"^{reason}$"):
        await preview(fixture)
    assert page.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('payload,reason', [
    ({'message': 'csrf_failed'}, 'story_preview_request_refused'),
    ({'require_login': True}, 'authentication_required'),
    ({'message': 'login_required'}, 'authentication_required'),
    ({'challenge': {'url': 'hidden'}}, 'checkpoint_required'),
    ({'message': 'Please wait a few minutes'}, 'rate_limited'),
])
@pytest.mark.parametrize('when', ['screenshot', 'close'])
async def test_forbidden_preview_reads_explicit_restriction_without_reconnect_guess(fixture, payload, reason, when):
    page = fixture[0].page
    async def refused(page):
        async def body():
            return json.dumps(payload).encode()
        page.response_handler(SimpleNamespace(url=GRAPHQL, status=403, body=body))
    if when == 'screenshot':
        page.after_screenshot = refused
    else:
        page.on_close = refused
    with pytest.raises(StoryPreviewError, match='^' + reason + '$'):
        await preview(fixture)
    assert page.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key,reason",
    [
        ("checkpoint", "checkpoint_required"),
        ("auth", "authentication_required"),
        ("rateLimited", "rate_limited"),
    ],
)
async def test_dom_restrictions_return_canonical_reason(fixture, key, reason):
    fixture[0].page.state[key] = True
    with pytest.raises(StoryPreviewError, match=f"^{reason}$"):
        await preview(fixture)


@pytest.mark.parametrize(
    "path,resource",
    [
        ("/rsrc.php/ye/r/module.wasm", "fetch"),
        ("/btmanifest/123/instagram/main", "xhr"),
        ("/rsrc.php/yk/r/icon.webp", "other"),
    ],
)
def test_measured_static_loader_requests_are_read_only(path, resource):
    from types import SimpleNamespace

    from engagemate.worker.story_preview import _read_request

    request = SimpleNamespace(
        url="https://static.cdninstagram.com" + path,
        method="GET",
        resource_type=resource,
    )
    assert _read_request(request, None)
    request.method = "POST"
    assert not _read_request(request, None)
    request.method = "GET"
    request.url = "https://untrusted.example" + path
    assert not _read_request(request, None)


@pytest.mark.asyncio
async def test_opens_one_measured_gate_under_guard_then_captures(fixture):
    page = fixture[0].page
    page.requests = []
    page.state["media"] = []
    page.gate_count = 1

    async def opened(page):
        page.state = snapshot()
        await page.emit(seen())

    page.on_gate = opened
    assert len(await preview(fixture)) == 1
    assert page.gate_clicks == 1
    assert page.routes[0].actions == ["abort"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ambiguous", "foreign", "owner_missing", "repeated"])
async def test_gate_cannot_open_ambiguous_foreign_or_repeat(fixture, kind):
    page = fixture[0].page
    page.requests = []
    page.state["media"] = []
    page.gate_count = 2 if kind == "ambiguous" else 1
    if kind == "foreign":
        page.state["url"] = "https://www.instagram.com/stories/other/"
    if kind == "owner_missing":
        page.state["ownerLinks"] = 0
    with pytest.raises(StoryPreviewError):
        await preview(fixture)
    assert page.gate_clicks == (1 if kind == "repeated" else 0)
    assert page.closed


@pytest.mark.parametrize(
    "host", ["scontent-atl3-1.cdninstagram.com", "video.fbcdn.net"]
)
def test_measured_cdn_media_fetch_is_allowed_without_allowing_writes(host):
    from engagemate.worker.story_preview import _read_request

    req = SimpleNamespace(
        url=f"https://{host}/o1/video.mp4?range=1", method="GET", resource_type="fetch"
    )
    assert _read_request(req, None)
    req.method = "POST"
    assert not _read_request(req, None)
    req.method = "GET"
    req.url = f"https://{host}/like/video.mp4"
    assert not _read_request(req, None)


@pytest.mark.asyncio
async def test_jpeg_at_transport_size_limit_is_preserved(fixture):
    page = fixture[0].page
    page.jpeg = JPEG[:-2] + b"x" * (512 * 1024 - len(JPEG)) + JPEG[-2:]
    (story,) = await preview(fixture)
    (frame,) = story.content.video_frames
    assert frame.mime_type == "image/jpeg"
    assert frame.data == page.jpeg
    assert len(frame.data) == 512 * 1024
    assert page.events.count("screenshot") == 1
