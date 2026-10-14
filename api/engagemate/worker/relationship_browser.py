"""One claim-bound native profile follow/unfollow through the assigned browser.

Follow request/control measured under an aborting guard on 2026-09-14. Unfollow
uses the same profile handler's compiled Relay mutation and confirmation dialog;
missing or changed controls defer without guessing. The caller owns consent,
sender verification and, for unfollow, its confirmed-follow ledger.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

from engagemate.worker.follow_maintenance import InterruptedFollowResult
from engagemate.worker.follower_browser import PlaywrightFollowerBrowser
from engagemate.worker.story_actions import _form, _nodes, _restriction, _unique_object

_HANDLE = re.compile(r"[a-z0-9_][a-z0-9._]{0,29}\Z")
_OWNER = re.compile(r"[1-9][0-9]{0,31}\Z")
_HOSTS = {"www.instagram.com", "instagram.com"}
_MUTATIONS = {
    "follow": "usePolarisFollowMutation",
    "unfollow": "usePolarisUnfollowMutation",
}
_FOLLOWING_CONTROL = re.compile(r"^Following(?: Down chevron icon)?$")
_ROOTS = {"follow": "xdt_create_friendship", "unfollow": "xdt_destroy_friendship"}
_READS = {
    "PolarisProfilePageContentQuery": "id",
    "PolarisProfileNoteBubbleQuery": "user_id",
    "PolarisProfileStoryHighlightsTrayContentQuery": "user_id",
    "PolarisProfileSuggestedUsersWithPreloadableQuery": "target_id",
    "PolarisSchoolPartnerProfileBadgeQuery": "igid",
    "PolarisProfilePostsQuery": "username",
    "PolarisProfileDirectOrPartnershipInboxMessageEligibilityQuery": None,
}
_HEALTH = {
    "rate_limit": "rate_limited",
    "login_required": "authentication_required",
    "checkpoint": "checkpoint_required",
    "challenge": "checkpoint_required",
}


def confirmed_relationship_evidence(action: str, evidence: object) -> bool:
    """Accept a native receipt or a scoped lost-response follow readback."""
    if (type(evidence) is not dict or evidence.get("health_signal")
            or evidence.get("fresh_profile_matches") is not True):
        return False
    if evidence.get("receipt_matches") is True:
        return True
    return (action == "follow"
            and evidence.get("receipt_source") == "profile_reconciliation"
            and evidence.get("receipt_matches") is False
            and all(evidence.get(key) is True for key in
                    ("receipt_timeout", "mutation_attempted", "request_forwarded", "identity_matches"))
            and all(evidence.get(key) is False for key in ("private", "following", "followed_by")))


def _mutation_matches(variables: object, owner_id: str) -> bool:
    if type(variables) is not dict or set(variables) != {
        "target_user_id",
        "container_module",
        "nav_chain",
    }:
        return False
    if variables["target_user_id"] != owner_id:
        return False
    return all(
        type(variables[key]) is str
        and 0 < len(variables[key]) <= 4096
        and not any(ord(c) < 32 or ord(c) == 127 for c in variables[key])
        for key in ("container_module", "nav_chain")
    )


def _receipt_matches(payload: object, action: str, handle: str, owner_id: str) -> bool:
    if type(payload) is not dict or type(payload.get("data")) is not dict:
        return False
    data = payload["data"]
    if set(data) != {_ROOTS[action]}:
        return False
    result = data[_ROOTS[action]]
    if type(result) is not dict or result.get("username") != handle:
        return False
    returned_id = result.get("id")
    if type(returned_id) is str and returned_id.isdecimal() and returned_id != owner_id:
        return False
    relationship = result.get("friendship_status")
    if (
        type(relationship) is not dict
        or relationship.get("following") is not (action == "follow")
        or relationship.get("outgoing_request") is not False
    ):
        return False
    try:
        for node in _nodes(payload):
            if node.get("errors") or node.get("error") or node.get("success") is False:
                return False
            if "status" in node and node["status"] != "ok":
                return False
            for key in ("pk", "owner_id", "target_user_id"):
                if key in node and node[key] != owner_id:
                    return False
    except ValueError:
        return False
    return True


def _read_request(request: Any, handle: str, owner_id: str) -> bool:
    parsed = urlsplit(request.url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not (host in _HOSTS or host.endswith((".cdninstagram.com", ".fbcdn.net")))
    ):
        return False
    if request.method == "POST":
        form = _form(request)
        if (
            host not in _HOSTS
            or parsed.path not in {"/api/graphql", "/graphql/query"}
            or parsed.query
            or form is None
        ):
            return False
        name, variables = form
        if name not in _READS:
            return False
        key = _READS[name]
        if key is None:
            return set(variables) == {"ig_creator_user_id"}
        return variables.get(key) == (handle if key == "username" else owner_id)
    if request.method not in {"GET", "HEAD"}:
        return False
    resource = request.resource_type
    if resource == "document":
        return host in _HOSTS and parsed.path == f"/{handle}/" and not parsed.query
    if host == "static.cdninstagram.com":
        return parsed.path.startswith(("/rsrc.php/", "/btmanifest/"))
    if host.endswith((".cdninstagram.com", ".fbcdn.net")):
        return resource in {"image", "font", "stylesheet", "script"}
    if parsed.path == "/api/v1/users/web_profile_info/":
        return parse_qs(parsed.query) == {"username": [handle]}
    return parsed.path == "/ajax/bootloader-endpoint/" and resource in {"xhr", "fetch"}


class RelationshipBrowser:
    def __init__(
        self,
        context: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_ms: int = 20_000,
    ) -> None:
        self._context, self._clock = context, clock
        self._timeout = max(100, min(timeout_ms, 30_000)) / 1000
        self._lock = asyncio.Lock()

    async def perform(
        self,
        handle: str,
        owner_id: str,
        action: str,
        before_mutation: Callable[[], Awaitable[bool]],
    ) -> tuple[str, dict]:
        return await self.perform_with_result(handle, owner_id, action, before_mutation, None)

    async def perform_with_result(
        self, handle, owner_id, action, before_mutation, on_result,
    ) -> tuple[str, dict]:
        if (
            type(handle) is not str
            or _HANDLE.fullmatch(handle) is None
            or type(owner_id) is not str
            or _OWNER.fullmatch(owner_id) is None
            or type(action) is not str
            or action not in _MUTATIONS
            or not callable(before_mutation)
            or (on_result is not None and not callable(on_result))
        ):
            return "deferred", {
                "reason": "invalid_request",
                "mutation_attempted": False,
            }
        now = self._clock()
        if type(now) is not datetime or now.utcoffset() != timedelta(0):
            return "deferred", {"reason": "invalid_clock", "mutation_attempted": False}
        async with self._lock:
            return await self._perform(handle, owner_id, action, before_mutation, on_result)

    async def _perform(
        self,
        handle: str,
        owner_id: str,
        action: str,
        before_mutation: Callable[[], Awaitable[bool]],
        on_result=None,
    ) -> tuple[str, dict]:
        page = cdp = None
        armed = closing = clicked = False
        forwarded = None
        receipt = False
        done = asyncio.Event()
        tasks: set[asyncio.Task] = set()
        evidence: dict[str, Any] = {
            "mutation_attempted": False,
            "request_forwarded": False,
        }
        outcome = "deferred"
        interruption = observer_error = None

        def latch(signal: Any) -> None:
            if signal is not None:
                health = _HEALTH.get(signal.value)
                if health is not None:
                    evidence["health_signal"] = health

        async def guard(route: Any) -> None:
            nonlocal forwarded
            try:
                request = route.request
                if closing or evidence.get("health_signal"):
                    await route.abort()
                    return
                if _read_request(request, handle, owner_id):
                    await route.continue_()
                    return
                parsed = urlsplit(request.url)
                form = _form(request) if request.method == "POST" else None
                if (
                    armed
                    and forwarded is None
                    and parsed.scheme == "https"
                    and parsed.netloc in _HOSTS
                    and parsed.path == "/api/graphql"
                    and not parsed.query
                    and not parsed.fragment
                    and form is not None
                    and form[0] == _MUTATIONS[action]
                    and _mutation_matches(form[1], owner_id)
                ):
                    forwarded = request
                    evidence["request_forwarded"] = True
                    await route.continue_()
                    return
                await route.abort()
            except Exception:  # noqa: BLE001 -- Protocol errors may only abort, never bypass guards.
                await route.abort()

        async def block_socket(socket: Any) -> None:
            await socket.close()

        async def consume(response: Any) -> None:
            nonlocal receipt
            if urlsplit(response.url).hostname not in _HOSTS:
                return
            latch(_restriction(response.status, None))
            if response.request is not forwarded:
                return
            evidence["http_status"] = response.status
            payload = None
            try:
                raw = await response.body()
                if type(raw) is bytes and len(raw) <= 65536:
                    payload = json.loads(raw, object_pairs_hook=_unique_object)
            except Exception:  # noqa: BLE001 -- Unknown receipts remain uncertain.
                payload = None
            latch(_restriction(response.status, payload))
            receipt = (
                200 <= response.status < 300
                and not evidence.get("health_signal")
                and _receipt_matches(payload, action, handle, owner_id)
            )
            evidence["receipt_matches"] = receipt
            done.set()

        def observe(response: Any) -> None:
            task = asyncio.create_task(consume(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def identity() -> bool:
            parsed = urlsplit(page.url)
            if (
                parsed.scheme != "https"
                or parsed.netloc not in _HOSTS
                or parsed.path != f"/{handle}/"
                or parsed.query
                or parsed.fragment
                or evidence.get("health_signal")
            ):
                return False
            header = page.locator("main header")
            return (
                await header.count() == 1
                and await header.get_by_text(handle, exact=True).count() >= 1
            )

        async def cleanup() -> None:
            if page is not None:
                try:
                    async with asyncio.timeout(3):
                        await page.close(run_before_unload=False)
                except Exception:  # noqa: BLE001 -- Keep the guard if closure fails.
                    evidence["cleanup_failed"] = True
                for task in tuple(tasks):
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tuple(tasks), return_exceptions=True)
                if cdp is not None and not evidence.get("cleanup_failed"):
                    try:
                        async with asyncio.timeout(1):
                            await cdp.detach()
                    except Exception:  # noqa: BLE001 -- Closed page cannot forward writes.
                        return

        try:
            async with asyncio.timeout(self._timeout * 3):
                page = await self._context.new_page()
                await page.route("**/*", guard)
                await page.route_web_socket("**/*", block_socket)
                page.on("response", observe)
                cdp = await self._context.new_cdp_session(page)
                await cdp.send("Network.setBypassServiceWorker", {"bypass": True})
                browser = PlaywrightFollowerBrowser(
                    page,
                    clock=lambda: self._clock().timestamp(),
                    timeout_ms=int(self._timeout * 1000),
                )
                profile = await browser.inspect(handle)
                exact = (
                    profile.get("owner_id") == owner_id
                    and profile.get("handle") == handle
                )
                evidence["identity_matches"] = exact
                evidence.update(
                    {
                        key: profile.get(key)
                        if type(profile.get(key)) is bool
                        else None
                        for key in ("private", "following", "followed_by")
                    }
                )
                if evidence.get("health_signal") or not exact:
                    evidence["reason"] = "profile_unavailable"
                elif action == "unfollow" and profile.get("following") is False:
                    outcome = "already_absent"
                elif action == "follow" and profile.get("following") is True:
                    outcome = "already_following"
                elif action == "follow" and any(
                    profile.get(k) is not False
                    for k in ("private", "following", "followed_by")
                ):
                    evidence["reason"] = "ineligible_relationship"
                elif action == "unfollow" and profile.get("following") is not True:
                    evidence["reason"] = "relationship_unknown"
                else:
                    controls = page.locator("main header").get_by_role(
                        "button",
                        name="Follow" if action == "follow" else _FOLLOWING_CONTROL,
                        exact=True,
                    )
                    async with asyncio.timeout(self._timeout):
                        while await controls.count() == 0 and not evidence.get(
                            "health_signal"
                        ):
                            await asyncio.sleep(0.05)
                    if (
                        await controls.count() != 1
                        or not await controls.is_visible()
                        or not await controls.is_enabled()
                        or not await identity()
                    ):
                        evidence["reason"] = "control_unavailable"
                    else:
                        if action == "unfollow":
                            # Opening the confirmation does not arm any mutation.
                            await controls.click(timeout=int(self._timeout * 1000))
                            dialog = page.get_by_role("dialog")
                            await dialog.wait_for(
                                state="visible", timeout=int(self._timeout * 1000)
                            )
                            recipient = dialog.get_by_text(
                                re.compile(
                                    rf"(?<![A-Za-z0-9._]){re.escape(handle)}(?![A-Za-z0-9._])"
                                )
                            )
                            # The visible dialog initially contains only a spinner.
                            # Wait for its recipient before verifying or arming it.
                            async with asyncio.timeout(self._timeout):
                                while (
                                    await dialog.count() == 1
                                    and not await recipient.count()
                                    and not evidence.get("health_signal")
                                ):
                                    await asyncio.sleep(0.05)
                            if (
                                await dialog.count() != 1
                                or not await recipient.count()
                                or evidence.get("health_signal")
                            ):
                                raise ValueError("unverified recipient dialog")
                            controls = dialog.get_by_role(
                                "button", name="Unfollow", exact=True
                            )
                        if (
                            await controls.count() != 1
                            or not await controls.is_enabled()
                            or not await controls.is_visible()
                            or not await identity()
                        ):
                            evidence["reason"] = "control_unavailable"
                        elif await before_mutation() is not True:
                            evidence["reason"] = "before_mutation_denied"
                        elif not await identity():
                            evidence["reason"] = "identity_changed"
                        else:
                            armed = clicked = True
                            evidence["mutation_attempted"] = True
                            await controls.click(timeout=int(self._timeout * 1000))
                            try:
                                await asyncio.wait_for(done.wait(), self._timeout)
                            except TimeoutError:
                                evidence["receipt_timeout"] = True
                            armed = False
                            outcome = "uncertain"
                            # A missing response can follow an accepted write.
                            # Read the exact public profile once; never click again.
                            # An explicit invalid receipt or an unfollow retains
                            # the stricter native-receipt requirement.
                            reconcile_follow = (action == "follow" and forwarded is not None
                                                and evidence.get("receipt_timeout") is True
                                                and not evidence.get("health_signal")
                                                and "http_status" not in evidence)
                            if receipt or reconcile_follow:
                                fresh = await browser.inspect(handle)
                                matches = (
                                    fresh.get("owner_id") == owner_id
                                    and fresh.get("handle") == handle
                                    and fresh.get("following") is (action == "follow")
                                )
                                if action == "follow":
                                    matches = matches and fresh.get("private") is False
                                evidence["fresh_profile_matches"] = matches
                                if reconcile_follow and not receipt:
                                    evidence["receipt_matches"] = False
                                    evidence["receipt_source"] = "profile_reconciliation"
                                if confirmed_relationship_evidence(action, evidence):
                                    outcome = "confirmed"
        except asyncio.CancelledError as error:
            interruption = error
            outcome = "uncertain" if clicked else "deferred"
        except Exception as error:  # noqa: BLE001 -- Never retry an attempted native mutation.
            outcome = "uncertain" if clicked else "deferred"
            reason = getattr(error, "reason", None)
            if type(reason) is str and reason in {
                "rate_limited",
                "authentication_required",
                "checkpoint_required",
            }:
                evidence["health_signal"] = reason
            evidence["reason"] = (
                "action_unverified" if clicked else "profile_or_control_unavailable"
            )
        finally:
            closing = True
            try:
                if on_result is not None and interruption is None:
                    # Native receipt or the scoped follow reconciliation has
                    # passed with fresh readback before confirmation persists.
                    await on_result(outcome, dict(evidence))
            except asyncio.CancelledError as error:
                interruption = error
            except Exception as error:  # noqa: BLE001 - retain persistence error after cleanup
                observer_error = error
            task = asyncio.create_task(
                cleanup(), name="engagemate-relationship-cleanup"
            )
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError as error:
                    # A cancelled cleanup task makes shield raise a new
                    # exception. Read its original exception via result below.
                    if interruption is None and asyncio.current_task().cancelling():
                        interruption = error
                except Exception:  # noqa: BLE001 - inspect the cleanup task result below
                    break
            try:
                task.result()
            except asyncio.CancelledError as error:
                if interruption is None:
                    interruption = error
            except Exception:  # noqa: BLE001 - retain known action result when cleanup fails
                evidence["cleanup_failed"] = True
            if interruption is not None:
                interruption._engagemate_follow_result = InterruptedFollowResult(outcome, dict(evidence))
                raise interruption
            if observer_error is not None:
                raise observer_error
        if outcome != "confirmed" and (evidence.get("cleanup_failed") or evidence.get("health_signal")):
            outcome = "uncertain" if clicked else "deferred"
        return outcome, evidence
