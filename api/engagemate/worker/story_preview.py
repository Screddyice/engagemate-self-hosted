"""Capture actual Story media on a disposable page while blocking view writes.

The context must disable service workers. Page-scoped CDP bypass and WebSocket
blocking provide additional isolation; the original profile page is untouched.
Metadata comes from an aborted seen mutation, never from page-load timing.
"""

# Browser failures cross third-party protocol boundaries. Keep them bounded,
# and never log raw exception messages that can contain request credentials.
# ruff: noqa: BLE001, S110
from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from engagemate.worker.follower_browser import FollowerBrowserError, _check_payload

from engagemate.worker.story_availability import (
    loaded_story_media, story_media_unavailable, visible_story_unavailable,
)
from engagemate.worker.story_identity import StoryIdentity, parse_seen_mutation
from engagemate.worker.story_types import (
    ActiveStory,
    DiscoveryContext,
    DiscoveryTarget,
    StoryContent,
    StoryMedia,
    StoryMediaType,
)

_MAX_BYTES = 512 * 1024
_JPEG_START = b"\xff\xd8\xff"
_JPEG_END = b"\xff\xd9"
_SEEN = "PolarisStoriesV3SeenMutation"
_RESTRICTIONS = {"authentication_required", "checkpoint_required", "rate_limited"}
_HANDLE = re.compile(r"[a-z0-9_][a-z0-9._]{0,29}\Z")
_OWNER = re.compile(r"[1-9][0-9]{0,31}\Z")
_QUERIES = {
    "PolarisProfileStoryHighlightsTrayContentQuery",
    "PolarisStoriesV3TrayContainerQuery",
}
_GRAPHQL = {"/api/graphql", "/api/graphql/", "/graphql/query", "/graphql/query/"}
_STATIC = {"document", "script", "stylesheet", "image", "media", "font", "manifest"}
_WRITE_PATH = re.compile(
    r"/(?:seen|like|unlike|delete|create|send|direct|approve|follow|unfollow)(?:/|$)"
)

# These selectors reflect the measured regular viewer: owner profile anchors,
# time[datetime], and a visible large img/video. No synthetic tray attributes.
_CENSUS = """({handle}) => {
  const visible = el => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0 &&
      r.top < innerHeight && r.left < innerWidth && s.display !== 'none' &&
      s.visibility === 'visible' && Number(s.opacity) > 0;
  };
  const anchors = [...document.querySelectorAll('a[href]')].filter(visible);
  const ownerLinks = anchors.filter(a => {
    const u = new URL(a.href, location.href);
    return ['instagram.com','www.instagram.com'].includes(u.hostname) &&
      u.pathname.replace(/\\/$/, '') === '/' + handle;
  }).length;
  const auth = anchors.some(a => /\\/accounts\\/(login|emailsignup)\\//.test(a.href));
  const text = (document.body?.innerText || '').slice(0, 50000).toLowerCase();
  const checkpoint = /confirm it.s you|challenge_required|checkpoint_required/.test(text);
  const rateLimited = /try again later|please wait a few minutes|we restrict certain activity|feedback_required/.test(text);
  const loginRequired = /login_required/.test(text);
  const times = [...document.querySelectorAll('time[datetime]')].filter(visible).map(t => t.getAttribute('datetime'));
  const media = [...document.querySelectorAll('video, img')].map((el,index) => {
    const r = el.getBoundingClientRect();
    return {index, tag: el.tagName, visible: visible(el), width:r.width, height:r.height,
      intrinsicWidth: el.tagName === 'VIDEO' ? el.videoWidth : el.naturalWidth,
      intrinsicHeight: el.tagName === 'VIDEO' ? el.videoHeight : el.naturalHeight,
      source: el.currentSrc || el.src || '', ready: el.tagName === 'VIDEO' ? el.readyState : (el.complete && el.naturalWidth > 0 ? 2 : 0)};
  }).filter(m => m.visible && m.width > 200 && m.height > 200);
  return {url: location.href, ownerLinks, auth: auth || loginRequired, checkpoint, rateLimited, times, media};
}"""


class StoryPreviewError(RuntimeError):
    """A bounded reason, safe to report without cookies or request forms."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _form(value: Any) -> dict[str, list[str]]:
    if type(value) is not str or not value or len(value) > 131_072:
        return {}
    try:
        return parse_qs(
            value, keep_blank_values=True, strict_parsing=True, max_num_fields=128
        )
    except ValueError:
        return {}


def _instagram(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.netloc.lower() in {
        "instagram.com",
        "www.instagram.com",
    }


def _read_request(request: Any, friendly: str | None) -> bool:
    parsed = urlsplit(request.url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    if request.method == "POST":
        return (
            _instagram(request.url) and parsed.path in _GRAPHQL and friendly in _QUERIES
        )
    if request.method not in {"GET", "HEAD"}:
        return False
    if _WRITE_PATH.search(parsed.path):
        return False
    if parsed.path in _GRAPHQL:
        names = _form(parsed.query).get("fb_api_req_friendly_name", [])
        return _instagram(request.url) and len(names) == 1 and names[0] in _QUERIES
    trusted = host in {"instagram.com", "www.instagram.com"} or host.endswith(
        (".cdninstagram.com", ".fbcdn.net")
    )
    return trusted and (
        request.resource_type in _STATIC
        or (
            host.endswith((".cdninstagram.com", ".fbcdn.net"))
            and parsed.path.lower().endswith((".mp4", ".jpg", ".jpeg", ".png", ".webp"))
            and request.resource_type in {"xhr", "fetch", "other"}
        )
        or (
            host == "static.cdninstagram.com"
            and parsed.path.startswith(("/rsrc.php/", "/btmanifest/"))
            and request.resource_type in {"xhr", "fetch", "other"}
        )
        or (
            _instagram(request.url)
            and request.resource_type in {"xhr", "fetch"}
            and (
                parsed.path.startswith("/api/")
                or parsed.path == "/ajax/bootloader-endpoint/"
            )
        )
    )


def _restriction(snapshot: dict[str, Any]) -> str | None:
    path = urlsplit(str(snapshot.get("url", ""))).path
    if snapshot.get("checkpoint") or path.startswith(("/checkpoint/", "/challenge/")):
        return "checkpoint_required"
    if snapshot.get("auth") or path.startswith("/accounts/"):
        return "authentication_required"
    if snapshot.get("rateLimited") or snapshot.get("restricted"):
        return "rate_limited"
    return None


async def _available_snapshot(page: Any, handle: str) -> dict[str, Any]:
    snapshot = await page.evaluate(_CENSUS, {"handle": handle})
    if not isinstance(snapshot, dict):
        raise StoryPreviewError("story_preview_unreadable")
    restriction = _restriction(snapshot)
    if restriction:
        raise StoryPreviewError(restriction)
    if await visible_story_unavailable(page):
        raise StoryPreviewError("story_unavailable")
    return snapshot


def _media_sample(
    snapshot: Any, target: DiscoveryTarget, identity: StoryIdentity
) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        raise StoryPreviewError("story_preview_unreadable")
    restriction = _restriction(snapshot)
    if restriction:
        raise StoryPreviewError(restriction)
    url = snapshot.get("url")
    if not isinstance(url, str) or not _instagram(url):
        raise StoryPreviewError("story_preview_identity_mismatch")
    parsed = urlsplit(url)
    if parsed.path.rstrip("/") not in {
        f"/stories/{target.handle}",
        f"/stories/{target.handle}/{identity.media_id}",
    }:
        raise StoryPreviewError("story_preview_identity_mismatch")
    if parsed.fragment or parsed.query not in {"", "r=1"}:
        raise StoryPreviewError("story_preview_identity_mismatch")
    times = snapshot.get("times")
    media = snapshot.get("media")
    if (
        type(snapshot.get("ownerLinks")) is not int
        or snapshot["ownerLinks"] < 1
        or times == []
        or media == []
    ):
        return None
    if (
        not isinstance(times, list)
        or len(times) != 1
        or not isinstance(media, list)
        or len(media) != 1
    ):
        raise StoryPreviewError("story_preview_ambiguous")
    try:
        taken = datetime.fromisoformat(times[0].replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        raise StoryPreviewError("story_preview_identity_mismatch") from None
    if taken.tzinfo is None or taken.astimezone(UTC) != identity.taken_at:
        raise StoryPreviewError("story_preview_identity_mismatch")
    sample = media[0]
    if not isinstance(sample, dict) or sample.get("tag") not in {"IMG", "VIDEO"}:
        raise StoryPreviewError("story_preview_unreadable")
    if (
        type(sample.get("index")) is not int
        or sample["index"] < 0
        or not isinstance(sample.get("source"), str)
        or not sample["source"]
        or len(sample["source"]) > 8192
        or sample.get("visible") is not True
    ):
        raise StoryPreviewError("story_preview_unreadable")
    if story_media_unavailable(sample):
        raise StoryPreviewError("story_unavailable")
    if any(
        type(sample.get(key)) not in (int, float)
        or not math.isfinite(sample[key])
        or sample[key] <= 200
        for key in ("width", "height")
    ):
        return None
    if type(sample.get("ready")) is not int or sample["ready"] < 2:
        return None
    if any(
        type(sample.get(key)) not in (int, float)
        or not math.isfinite(sample[key])
        or sample[key] < 200
        for key in ("intrinsicWidth", "intrinsicHeight")
    ):
        return None
    if not loaded_story_media({
        **sample, "loaded": True,
        "width": sample["intrinsicWidth"], "height": sample["intrinsicHeight"],
    }):
        raise StoryPreviewError("story_preview_media_unreadable")
    return {key: value for key, value in sample.items() if key != "ready"}


class ReadOnlyStoryBrowser:
    def __init__(self, context: Any, *, timeout_seconds: float = 20) -> None:
        if not 0 < timeout_seconds <= 60:
            raise ValueError("invalid preview timeout")
        self._context = context
        self._timeout = timeout_seconds

    async def active_stories(
        self, context: DiscoveryContext, target: DiscoveryTarget
    ) -> tuple[ActiveStory, ...]:
        if (
            type(context) is not DiscoveryContext
            or type(target) is not DiscoveryTarget
            or type(context.user_id) is not UUID
            or type(context.ig_account_id) is not UUID
            or target.user_id != context.user_id
            or target.ig_account_id != context.ig_account_id
            or type(target.id) is not UUID
            or type(target.enabled) is not bool
            or type(context.now) is not datetime
            or context.now.tzinfo is None
            or type(target.handle) is not str
            or _HANDLE.fullmatch(target.handle) is None
            or type(target.owner_id) is not str
            or _OWNER.fullmatch(target.owner_id) is None
        ):
            raise StoryPreviewError("story_preview_invalid_target")
        if not target.enabled:
            return ()
        try:
            page = await self._context.new_page()
        except Exception:
            raise StoryPreviewError("story_preview_unavailable") from None
        identity: StoryIdentity | None = None
        guard_error: str | None = None
        result: ActiveStory | None = None
        opened_gate = False
        cdp = None
        response_tasks: set[asyncio.Task] = set()

        def latch_error(reason: str) -> None:
            nonlocal guard_error
            if guard_error not in _RESTRICTIONS:
                guard_error = reason

        async def guard(route: Any) -> None:
            nonlocal identity
            try:
                request = route.request
                try:
                    post_data = request.post_data if request.method == "POST" else None
                except UnicodeDecodeError:
                    # Instagram sends binary telemetry. It is an unknown write,
                    # so block it without invalidating an otherwise valid sample.
                    await route.abort()
                    return
                form = _form(post_data)
                names = form.get("fb_api_req_friendly_name", [])
                friendly = names[0] if len(names) == 1 else None
                if _SEEN in names:
                    await route.abort()
                    if (
                        not _instagram(request.url)
                        or urlsplit(request.url).path not in _GRAPHQL
                    ):
                        latch_error("story_preview_identity_mismatch")
                        return
                    try:
                        parsed = parse_seen_mutation(
                            post_data,
                            expected_owner_id=target.owner_id,
                            expected_handle=target.handle,
                            page_url=page.url,
                            now=context.now,
                        )
                        if identity is not None and parsed != identity:
                            latch_error("story_preview_auto_advanced")
                        identity = parsed
                    except ValueError:
                        latch_error("story_preview_identity_mismatch")
                elif _read_request(request, friendly):
                    await route.continue_()
                else:
                    await route.abort()
            except Exception:
                latch_error("story_preview_guard_failed")
                try:
                    await route.abort()
                except Exception:
                    pass

        async def block_socket(socket: Any) -> None:
            try:
                await socket.close()
            except Exception:
                latch_error("story_preview_guard_failed")

        async def inspect_refusal(response: Any) -> None:
            try:
                async with asyncio.timeout(1):
                    raw = await response.body()
                if type(raw) is bytes and len(raw) <= 65_536:
                    _check_payload(json.loads(raw))
            except FollowerBrowserError as error:
                if error.reason in _RESTRICTIONS:
                    latch_error(error.reason)
            except Exception:
                pass  # An unreadable refusal remains a refusal, never a login.

        def observe_response(response: Any) -> None:
            if _instagram(response.url):
                if response.status == 429:
                    latch_error("rate_limited")
                elif response.status == 401:
                    latch_error("authentication_required")
                elif response.status == 403:
                    latch_error("story_preview_request_refused")
                    if len(response_tasks) < 16:
                        task = asyncio.create_task(inspect_refusal(response))
                        response_tasks.add(task)
                        task.add_done_callback(response_tasks.discard)

        try:
            async with asyncio.timeout(self._timeout):
                await page.route("**/*", guard)
                page.on("response", observe_response)
                await page.route_web_socket("**/*", block_socket)
                cdp = await self._context.new_cdp_session(page)
                await cdp.send("Network.setBypassServiceWorker", {"bypass": True})
                response = await page.goto(
                    f"https://www.instagram.com/stories/{target.handle}/",
                    wait_until="domcontentloaded",
                    timeout=int(self._timeout * 1000),
                )
                if response is not None:
                    observe_response(response)
                if guard_error:
                    raise StoryPreviewError(guard_error)
                if response is None or response.status >= 400:
                    raise StoryPreviewError("story_preview_navigation_failed")
                while True:
                    if guard_error:
                        raise StoryPreviewError(guard_error)
                    snapshot = await _available_snapshot(page, target.handle)
                    if identity is None and not opened_gate:
                        gate = page.get_by_role(
                            "button", name="View story", exact=False
                        )
                        count = await gate.count()
                        if count > 1:
                            raise StoryPreviewError("story_preview_ambiguous")
                        if count == 1 and await gate.is_visible():
                            snapshot = await _available_snapshot(page, target.handle)
                            if snapshot.get("ownerLinks") == 0:
                                await asyncio.sleep(0.05)
                                continue
                            parsed_url = urlsplit(str(snapshot.get("url", "")))
                            if (
                                not _instagram(str(snapshot.get("url", "")))
                                or parsed_url.path.rstrip("/")
                                != f"/stories/{target.handle}"
                                or parsed_url.query not in {"", "r=1"}
                                or parsed_url.fragment
                                or type(snapshot.get("ownerLinks")) is not int
                                or snapshot["ownerLinks"] < 1
                            ):
                                raise StoryPreviewError(
                                    "story_preview_identity_mismatch"
                                )
                            opened_gate = True
                            await gate.click(timeout=3_000)
                    if identity is not None:
                        media = _media_sample(snapshot, target, identity)
                        if media is not None:
                            break
                    await asyncio.sleep(0.05)
                before_identity = identity
                image = (
                    await page.locator("video, img")
                    .nth(media["index"])
                    .screenshot(type="jpeg", quality=75, timeout=3_000)
                )
                after = _media_sample(
                    await _available_snapshot(page, target.handle),
                    target,
                    before_identity,
                )
                if guard_error or identity != before_identity or after != media:
                    raise StoryPreviewError(
                        guard_error or "story_preview_auto_advanced"
                    )
                if (
                    type(image) is not bytes
                    or not image.startswith(_JPEG_START)
                    or not image.endswith(_JPEG_END)
                    or not len(_JPEG_START) + len(_JPEG_END) < len(image) <= _MAX_BYTES
                ):
                    raise StoryPreviewError("story_preview_media_unreadable")
                payload = StoryMedia("image/jpeg", image)
                video = media["tag"] == "VIDEO"
                result = ActiveStory(
                    target.id,
                    identity.owner_id,
                    identity.media_id,
                    identity.taken_at,
                    None,
                    StoryContent(
                        StoryMediaType.VIDEO if video else StoryMediaType.IMAGE,
                        None,
                        None,
                        None if video else payload,
                        (payload,) if video else (),
                    ),
                )
        except StoryPreviewError:
            raise
        except TimeoutError:
            raise StoryPreviewError("story_preview_timeout") from None
        except Exception:
            raise StoryPreviewError("story_preview_unavailable") from None
        finally:
            # Keep request interception active through unload and delayed beacons.
            # A close failure deliberately leaves the guard installed.
            try:
                await asyncio.wait_for(page.close(run_before_unload=False), timeout=5)
            except Exception:
                raise StoryPreviewError("story_preview_close_failed") from None
            finally:
                if response_tasks:
                    await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
            try:
                await page.unroute("**/*", guard)
            except Exception:
                pass  # A closed Playwright target may reject interceptor cleanup.
            if cdp is not None:
                try:
                    await cdp.detach()
                except Exception:
                    pass
            if guard_error in _RESTRICTIONS:
                raise StoryPreviewError(guard_error)
        if guard_error or result is None:
            raise StoryPreviewError(guard_error or "story_preview_unreadable")
        return (result,)
