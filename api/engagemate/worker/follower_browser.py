"""Read profiles and follower lists without opening stories or taking actions.

Profile GraphQL and the follower dialog were measured against authenticated
Instagram on 2026-09-13. Missing evidence is unknown or an error, never an
empty audience. Only pagination of the observed follower endpoint uses the
browser context's authenticated request client.
"""
from __future__ import annotations

import asyncio
import math
import re
import time
import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit

_HANDLE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._]{0,29}\Z")
_ID = re.compile(r"[0-9]{1,32}\Z")
_HOSTS = {"www.instagram.com", "instagram.com"}
_FOLLOWERS = re.compile(r"^[0-9.,KMkm]+ followers$")
_RESERVED = {"accounts", "challenge", "checkpoint", "direct", "explore", "reel", "reels", "stories"}


class FollowerBrowserError(RuntimeError):
    """Bounded reason codes are safe to persist; raw response bodies are not."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _handle(value: Any) -> str:
    if not isinstance(value, str) or not _HANDLE.fullmatch(value) or value.lower() in _RESERVED:
        raise FollowerBrowserError("invalid_handle")
    return value.lower()


def _owner(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value)
    return value if _ID.fullmatch(value) else None


def _identity(node: dict) -> str | None:
    values = [_owner(node[key]) for key in ("pk", "id") if key in node]
    return values[0] if values and all(v and v == values[0] for v in values) else None


def _boolean(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _check_payload(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise FollowerBrowserError("response_unreadable")
    message = str(payload.get("message", ""))[:256].lower()
    if payload.get("checkpoint_url") or payload.get("challenge") or "challenge_required" in message:
        raise FollowerBrowserError("checkpoint_required")
    if "login_required" in message or payload.get("require_login") is True:
        raise FollowerBrowserError("authentication_required")
    if payload.get("spam") is True or any(s in message for s in ("please wait", "rate limit", "feedback_required")):
        raise FollowerBrowserError("rate_limited")
    if payload.get("status") not in (None, "ok") or payload.get("errors"):
        raise FollowerBrowserError("response_failed")


def _nodes(payload: Any):
    stack = [(payload, 0)]
    visited = 0
    while stack:
        node, depth = stack.pop()
        visited += 1
        if visited > 20_000 or depth > 30:
            raise FollowerBrowserError("response_too_large")
        if isinstance(node, dict):
            yield node
            stack.extend((v, depth + 1) for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend((v, depth + 1) for v in node if isinstance(v, (dict, list)))


def parse_profile(payload: Any, handle: str, *, now: float | None = None) -> dict | None:
    """Select exact profile identity; do not merge partial feed-user evidence."""
    handle = _handle(handle)
    _check_payload(payload)
    matches = []
    for node in _nodes(payload):
        username = node.get("username")
        if not isinstance(username, str) or username.lower() != handle:
            continue
        owner = _identity(node)
        # Full measured profile objects expose both profile counters.
        # A feed author may have privacy and partial friendship fields too.
        if (owner is None or "is_private" not in node
                or "media_count" not in node or "follower_count" not in node
                or not isinstance(node.get("friendship_status"), dict)):
            continue
        relationship = node["friendship_status"]
        reel = node.get("latest_reel_media")
        has_story = None
        if type(reel) in (int, float) and math.isfinite(reel) and reel >= 0:
            age = (time.time() if now is None else now) - reel
            if reel == 0:
                has_story = False
            elif age >= 0:
                has_story = age < 86_400
        matches.append({
            "owner_id": owner, "handle": handle,
            "private": _boolean(node.get("is_private")),
            "following": _boolean(relationship.get("following")),
            "followed_by": _boolean(relationship.get("followed_by")),
            "has_story": has_story,
        })
    if not matches:
        return None
    # Contradictory full objects are ambiguous, regardless of traversal order.
    if any(profile != matches[0] for profile in matches[1:]):
        raise FollowerBrowserError("profile_ambiguous")
    return matches[0]



def _public_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        raise FollowerBrowserError("profile_unreadable")
    cleaned = "".join(
        char for char in value
        if char in "\t\n\r" or unicodedata.category(char) not in {"Cc", "Cf", "Cs"}
    )
    # Match the UI's UTF-16 string bounds without cutting surrogate pairs.
    return cleaned.encode("utf-16-le")[:limit * 2].decode("utf-16-le", errors="ignore")


def _public_count(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 2_147_483_647:
        raise FollowerBrowserError("profile_unreadable")
    return value


def parse_public_profile(payload: Any, handle: str, *, owner_id: str | None = None) -> dict:
    """Map one verified full profile to the onboarding public-field contract."""
    handle = _handle(handle)
    identity = parse_profile(payload, handle)
    if identity is None or (owner_id is not None and identity["owner_id"] != owner_id):
        raise FollowerBrowserError("profile_unreadable")
    matches = []
    for node in _nodes(payload):
        username = node.get("username")
        if (not isinstance(username, str) or username.lower() != handle
                or _identity(node) != identity["owner_id"]
                or "media_count" not in node or "follower_count" not in node
                or not isinstance(node.get("friendship_status"), dict)):
            continue
        if type(node.get("is_private")) is not bool or type(node.get("is_verified")) is not bool:
            raise FollowerBrowserError("profile_unreadable")
        matches.append({
            "username": handle,
            "fullName": _public_text(node.get("full_name"), 256),
            "biography": _public_text(node.get("biography"), 2_200),
            "followersCount": _public_count(node.get("follower_count")),
            "followingCount": _public_count(node.get("following_count")),
            "postsCount": _public_count(node.get("media_count")),
            "isPrivate": node["is_private"],
            "isVerified": node["is_verified"],
            "latestPosts": [],
        })
    if not matches or any(profile != matches[0] for profile in matches[1:]):
        raise FollowerBrowserError("profile_unreadable")
    return matches[0]


def parse_followers(payload: Any) -> dict:
    _check_payload(payload)
    if payload.get("status") != "ok" or not isinstance(payload.get("users"), list):
        raise FollowerBrowserError("followers_unreadable")
    if len(payload["users"]) > 1_000:
        raise FollowerBrowserError("response_too_large")
    candidates = []
    seen: dict[str, str] = {}
    for user in payload["users"]:
        if not isinstance(user, dict):
            raise FollowerBrowserError("followers_unreadable")
        owner = _identity(user)
        try:
            handle = _handle(user.get("username"))
        except FollowerBrowserError:
            raise FollowerBrowserError("followers_unreadable") from None
        if owner is None or (owner in seen and seen[owner] != handle):
            raise FollowerBrowserError("followers_unreadable")
        if owner not in seen:
            candidates.append({"owner_id": owner, "handle": handle})
            seen[owner] = handle
    cursor = payload.get("next_max_id")
    if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or any(ord(c) < 32 for c in cursor)):
        raise FollowerBrowserError("pagination_unreadable")
    more = payload.get("has_more")
    if type(more) is not bool:
        raise FollowerBrowserError("pagination_unreadable")
    if more and cursor is None:
        raise FollowerBrowserError("pagination_unreadable")
    if not more and cursor is not None:
        raise FollowerBrowserError("pagination_unreadable")
    limited = payload.get("should_limit_list_of_followers")
    if limited is not None and type(limited) is not bool:
        raise FollowerBrowserError("followers_unreadable")
    return {"candidates": candidates, "next_cursor": cursor, "exhausted": not more,
            "limited": limited is True}


class PlaywrightFollowerBrowser:
    def __init__(self, page: Any, *, clock: Callable[[], float] = time.time,
                 timeout_ms: int = 20_000) -> None:
        self.page = page
        self.clock = clock
        self.timeout_ms = max(100, min(timeout_ms, 30_000))
        self._sources: dict[str, str] = {}
        self._latest_public_profile: tuple[str, str, dict] | None = None

    async def _check_page(self) -> None:
        path = urlsplit(self.page.url).path
        if path.startswith(("/challenge/", "/checkpoint/")):
            raise FollowerBrowserError("checkpoint_required")
        if path.startswith("/accounts/"):
            raise FollowerBrowserError("authentication_required")
        if await self.page.locator('a[href^="/accounts/login/"], a[href^="/accounts/emailsignup/"]').count():
            raise FollowerBrowserError("authentication_required")

    @staticmethod
    def _check_status(status: int, payload: Any = None) -> None:
        if status == 429:
            raise FollowerBrowserError("rate_limited")
        if status == 401:
            raise FollowerBrowserError("authentication_required")
        if status == 403:
            # Permission and CSRF refusals do not establish an expired login.
            # Explicit account restrictions still take precedence over refusal.
            try:
                _check_payload(payload)
            except FollowerBrowserError as error:
                if error.reason in {"authentication_required", "checkpoint_required", "rate_limited"}:
                    raise
        if status < 200 or status >= 300:
            raise FollowerBrowserError("response_failed")

    @classmethod
    async def _read_payload(cls, response: Any) -> Any:
        if response.status != 403:
            cls._check_status(response.status)
        try:
            payload = await response.json()
        except Exception:
            # A non-JSON refusal must not turn into an authentication signal.
            cls._check_status(response.status)
            raise
        cls._check_status(response.status, payload)
        return payload

    async def public_profile(self, handle: str) -> dict:
        profile = await self.inspect(handle)
        cached = self._latest_public_profile
        if cached is None or cached[:2] != (profile["handle"], profile["owner_id"]):
            raise FollowerBrowserError("profile_unreadable")
        return {**cached[2], "latestPosts": []}

    async def inspect(self, handle: str) -> dict:
        self._latest_public_profile = None
        handle = _handle(handle)
        loop = asyncio.get_running_loop()
        found = loop.create_future()
        tasks: set[asyncio.Task] = set()

        async def consume(response: Any) -> None:
            url = urlsplit(response.url)
            if url.hostname not in _HOSTS or url.path.rstrip("/") not in ("/api/graphql", "/graphql/query", "/api/v1/users/web_profile_info"):
                return
            try:
                payload = await self._read_payload(response)
                profile = parse_profile(payload, handle, now=self.clock())
                if profile is not None and not found.done():
                    try:
                        public = parse_public_profile(payload, handle, owner_id=profile["owner_id"])
                    except FollowerBrowserError:
                        # Discovery can use relationship evidence even when an
                        # onboarding metric is missing. Never fill metrics in.
                        public = None
                    if public is not None:
                        self._latest_public_profile = (handle, profile["owner_id"], public)
                    found.set_result(profile)
            except FollowerBrowserError as error:
                # Instagram multiplexes profile, feed and notification queries
                # on these URLs. An unrelated query failure is not evidence that
                # this profile failed. Global account failures still stop the
                # read, and contradictory exact profile objects are unsafe.
                if error.reason in {
                    "authentication_required", "checkpoint_required",
                    "rate_limited", "profile_ambiguous",
                } and not found.done():
                    found.set_exception(error)
            except Exception:  # noqa: BLE001 - fail closed and preserve session cleanup
                # Other GraphQL fetches may return non-JSON; no guessed profile.
                return

        def on_response(response: Any) -> None:
            task = asyncio.create_task(consume(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        self.page.on("response", on_response)
        try:
            navigation = await self.page.goto(f"https://www.instagram.com/{handle}/", wait_until="domcontentloaded", timeout=self.timeout_ms)
            await self._check_page()
            if navigation is not None:
                self._check_status(navigation.status)
            return await asyncio.wait_for(found, timeout=self.timeout_ms / 1000)
        except FollowerBrowserError:
            raise
        except Exception:  # noqa: BLE001 - fail closed and preserve session cleanup
            await self._check_page()
            raise FollowerBrowserError("profile_unreadable") from None
        finally:
            self.page.remove_listener("response", on_response)
            if not found.done():
                found.cancel()
            elif not found.cancelled():
                found.exception()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def followers(self, handle: str, cursor: str | None = None) -> dict:
        handle = _handle(handle)
        if cursor is not None and (not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or any(ord(c) < 32 for c in cursor)):
            raise FollowerBrowserError("pagination_unreadable")
        try:
            profile = await self.inspect(handle)
            if profile["private"] is True and profile["following"] is not True:
                return {"candidates": [], "next_cursor": None, "exhausted": True, "reason": "source_private"}
            if profile["private"] is None:
                raise FollowerBrowserError("source_privacy_unknown")
            source = profile["owner_id"]
            if handle in self._sources and self._sources[handle] != source:
                raise FollowerBrowserError("source_identity_changed")
            endpoint = f"https://www.instagram.com/api/v1/friendships/{source}/followers/"
            if cursor is None or handle not in self._sources:
                def matches(response: Any) -> bool:
                    parsed = urlsplit(response.url)
                    return parsed.hostname in _HOSTS and parsed.path == urlsplit(endpoint).path and response.request.method == "GET"

                async with self.page.expect_response(matches, timeout=self.timeout_ms) as pending:
                    await self.page.get_by_text(_FOLLOWERS).click(timeout=self.timeout_ms)
                response = await pending.value
                await self.page.get_by_role("dialog").wait_for(state="visible", timeout=self.timeout_ms)
                first_page = parse_followers(await self._read_payload(response))
                self._sources[handle] = source
                if cursor is None:
                    return first_page
            # The endpoint for this exact source has now been observed from the UI.
            response = await self.page.context.request.get(endpoint + "?" + urlencode({"count": 50, "max_id": cursor}), timeout=self.timeout_ms)
            result = parse_followers(await self._read_payload(response))
            if result["next_cursor"] == cursor:
                raise FollowerBrowserError("pagination_stalled")
            return result
        except FollowerBrowserError:
            raise
        except Exception:  # noqa: BLE001 - fail closed and preserve session cleanup
            await self._check_page()
            raise FollowerBrowserError("followers_unreadable") from None
