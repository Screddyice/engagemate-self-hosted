"""Bound Instagram reel reads and a single, receipt-checked follow-up DM.

Reel metadata uses the authenticated browser request context. Message delivery
uses the web composer because Instagram redirects private API writes made with
web cookies to the login flow. A lost UI response is uncertain and never
causes another send.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import parse_qs, urlsplit

from engagemate.worker.follow_reel_reads import reel_candidates, verified_reel_detail
from engagemate.worker.follow_reel_transport import (
    FollowReelTransport,
    ReelBrowserError,
    verified_thread_item,
)
from engagemate.worker.follow_reels import compose_message
from engagemate.worker.follower_browser import (
    FollowerBrowserError,
    PlaywrightFollowerBrowser,
)
from engagemate.worker.relationship_browser import _read_request
from engagemate.worker.story_actions import _form

__all__ = ["FollowReelBrowser", "ReelBrowserError", "verified_thread_item"]

_ORIGIN = "https://www.instagram.com"
_MESSAGE_MUTATION = "IGDirectTextSendMutation"
# Opening the profile's Message control bootstraps the direct composer with
# several read-only GraphQL operations. These names are the measured web
# contract; they are allowed only before the exact send mutation is armed.
_MESSAGE_UI_READS = frozenset({
    "igdchattabscontentoffmsysquery",
    "igdchattabrootoffmsysquery",
    "igpresenceunifiedsetupquery",
    "polarisviewersettingsquery",
    "usefeaturelimitsoffmsysquery",
    "quickpromotionsupportigschemabatchfetchquery",
    "polarisapigetfrcookiequery",
    "igdchattabsjeweloffmsysquery",
    "igdomnipickernullstatelistquery",
})
_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_HANDLE = re.compile(r"[a-z0-9_][a-z0-9._]{0,29}\Z")
_THREAD_PATH = re.compile(r"/direct/(?:t|thread)/([0-9]{1,100})(?:/|\Z)")


async def _one_visible(locator, timeout=12):
    """Wait for one visible control, including mounted but hidden controls."""
    try:
        async with asyncio.timeout(timeout):
            while True:
                if await locator.count() == 1 and await locator.is_visible():
                    return True
                await asyncio.sleep(0.05)
    except Exception:  # noqa: BLE001 - bounded UI readiness fails before arming
        return False


def _bounded_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{1,100}", value) else None


def _nested_value(payload, keys, *, depth=0, visited=0):
    """Find one bounded response field without retaining raw Instagram payloads."""
    if depth > 10 or visited > 512:
        return None
    if isinstance(payload, dict):
        for key in keys:
            value = _bounded_id(payload.get(key))
            if value:
                return value
        for value in payload.values():
            found = _nested_value(value, keys, depth=depth + 1, visited=visited + 1)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _nested_value(value, keys, depth=depth + 1, visited=visited + 1)
            if found:
                return found
    return None


def _inbox_message_receipt(payload, *, sender_id, recipient_id, body):
    """Find one exact sender-authored item in the intended one-to-one inbox thread.

    The composer response and URL do not always contain a thread id. The inbox
    is a read-only receipt source in that case; ambiguous matches are rejected
    so a missing receipt can never turn into a guessed thread or a replay.
    """
    matches = set()

    def walk(value, *, thread_id=None, users=None, is_group=None, depth=0, visited=0):
        if depth > 12 or visited > 2048:
            return
        if isinstance(value, dict):
            raw_thread_id = value.get("thread_id")
            current_thread_id = (
                _bounded_id(str(raw_thread_id)) if raw_thread_id is not None else thread_id
            )
            current_users = value.get("users") if isinstance(value.get("users"), list) else users
            current_is_group = value.get("is_group") if isinstance(value.get("is_group"), bool) else is_group
            item_type = value.get("item_type")
            item_text = value.get("text")
            if item_type == "link" and isinstance(value.get("link"), dict):
                item_text = value["link"].get("text")
            item_id = value.get("item_id")
            recipient_present = (
                isinstance(current_users, list)
                and any(
                    isinstance(user, dict) and str(user.get("pk")) == recipient_id
                    for user in current_users
                )
            )
            if (
                current_thread_id
                and _bounded_id(str(item_id))
                and item_type in {"text", "link"}
                and str(value.get("user_id")) == sender_id
                and item_text == body
                and current_is_group is not True
                and recipient_present
            ):
                matches.add((current_thread_id, str(item_id)))
            for child in value.values():
                walk(
                    child,
                    thread_id=current_thread_id,
                    users=current_users,
                    is_group=current_is_group,
                    depth=depth + 1,
                    visited=visited + 1,
                )
        elif isinstance(value, list):
            for child in value:
                walk(
                    child,
                    thread_id=thread_id,
                    users=users,
                    is_group=is_group,
                    depth=depth + 1,
                    visited=visited + 1,
                )

    walk(payload)
    if len(matches) != 1:
        return None
    thread_id, item_id = next(iter(matches))
    return {"thread_id": thread_id, "item_id": item_id}


def _message_recipients(value, recipient_id):
    if value is None:
        return True
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return False
    if not isinstance(value, list):
        return False
    return value == [recipient_id] or value == [[recipient_id]]


def _message_form_matches(form, *, recipient_id, body):
    """Match only the measured Polaris text-send request for this recipient."""
    if not form or form[0] != _MESSAGE_MUTATION or not isinstance(form[1], dict):
        return False
    variables = form[1]
    text = variables.get("text")
    if text != {"sensitive_string_value": body}:
        return False
    thread_id = variables.get("ig_thread_igid")
    if not _bounded_id(thread_id):
        return False
    offline = variables.get("offline_threading_id")
    if not isinstance(offline, str) or not re.fullmatch(r"[0-9]{1,100}", offline):
        return False
    return _message_recipients(variables.get("recipient_igids"), recipient_id)


def _message_ui_read(form):
    """Recognize only measured read-only requests needed to open the composer."""
    return (
        isinstance(form, tuple)
        and len(form) == 2
        and isinstance(form[0], str)
        and form[0].lower() in _MESSAGE_UI_READS
    )


def _message_thread_bootstrap_matches(request, recipient_id):
    """Match Instagram's no-body thread bootstrap for this exact recipient."""
    try:
        raw = request.post_data
        fields = parse_qs(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
        return False
    if set(fields) != {"recipient_users", "fb_dtsg", "jazoest"}:
        return False
    recipients = fields.get("recipient_users")
    if not recipients or len(recipients) != 1:
        return False
    try:
        recipients = json.loads(recipients[0])
    except (TypeError, ValueError):
        return False
    if recipients != [recipient_id]:
        return False
    csrf = fields.get("fb_dtsg")
    jazoest = fields.get("jazoest")
    return (
        len(csrf) == 1 and isinstance(csrf[0], str) and 0 < len(csrf[0]) <= 16_384
        and len(jazoest) == 1 and re.fullmatch(r"[0-9]{1,32}", jazoest[0]) is not None
    )


def _message_response_failure(status, headers, payload, *, session_verified=False):
    location = headers.get("location", "") if isinstance(headers, dict) else ""
    try:
        destination = urlsplit(location)
    except ValueError:
        destination = None
    path = destination.path.lower().rstrip("/") if destination is not None else ""
    if status == 429:
        return ReelBrowserError("instagram_rate_limited", "rate_limited", http_status=status)
    if path == "/accounts/login" or path.startswith("/accounts/login/") or status == 401:
        if session_verified:
            return ReelBrowserError("message_transport_refused", http_status=status)
        return ReelBrowserError("instagram_login_required", "authentication_required", http_status=status)
    if (
        path in {"/challenge", "/checkpoint"}
        or path.startswith(("/challenge/", "/checkpoint/"))
    ):
        return ReelBrowserError("instagram_checkpoint", "checkpoint_required", http_status=status)
    if isinstance(payload, dict):
        if payload.get("challenge") or payload.get("checkpoint_url"):
            return ReelBrowserError("instagram_checkpoint", "checkpoint_required", http_status=status)
        message = payload.get("message")
        if payload.get("require_login") is True or (isinstance(message, str) and "login_required" in message):
            if session_verified:
                return ReelBrowserError("message_transport_refused", http_status=status)
            return ReelBrowserError("instagram_login_required", "authentication_required", http_status=status)
    if status < 200 or status >= 300:
        return ReelBrowserError("message_request_refused", http_status=status)
    if isinstance(payload, dict) and payload.get("errors"):
        return ReelBrowserError("message_request_refused", http_status=status)
    return None


class FollowReelBrowser(FollowReelTransport):
    def __init__(self, context):
        super().__init__(context)
        self.sender_handle = None
        self.page = None
        self._profile_target = None
        self._reel_cursor = None
        self._message_target = None
        self._message_body = None
        self._message_armed = False
        self._message_forwarded = None
        self._message_status = None
        self.recipient_handle = None

    async def close(self):
        if self.page is not None:
            try:
                await self.page.close(run_before_unload=False)
            finally:
                self.page = None

    async def _ensure_page(self):
        if self.page is None:
            self.page = await self.context.new_page()
            await self.page.route("**/*", self._guard)
            await self.page.route_web_socket("**/*", lambda socket: socket.close())
        return self.page

    async def _guard(self, route):
        try:
            request = route.request
            parsed = urlsplit(request.url)
            allowed = False
            if self._message_target:
                handle, owner_id = self._message_target
                allowed = _read_request(request, handle, owner_id)
                if not allowed and parsed.scheme == "https" and parsed.netloc == "www.instagram.com":
                    if request.method in {"GET", "HEAD"}:
                        allowed = parsed.path.startswith("/direct/") and not parsed.fragment
                    elif (request.method == "POST"
                          and parsed.path == "/api/v1/direct_v2/create_group_thread/"
                          and not parsed.query and not parsed.fragment
                          and not self._message_armed):
                        # Clicking Message creates or reopens the exact
                        # recipient thread before the text composer is shown.
                        # It carries no body; the later GraphQL send remains
                        # the only mutation allowed after arming.
                        allowed = _message_thread_bootstrap_matches(request, owner_id)
                    elif (request.method == "POST"
                          and parsed.path in {"/api/graphql", "/graphql/query"}
                          and not parsed.query and not parsed.fragment):
                        form = _form(request)
                        friendly_name = form[0].lower() if form else ""
                        is_direct_mutation = any(token in friendly_name for token in (
                            "mutation", "send", "broadcast", "create", "delete", "mark",
                        ))
                        if form and (_message_ui_read(form) or "direct" in friendly_name):
                            if _message_ui_read(form):
                                # Composer bootstrap queries are read-only and
                                # must never pass once the send is armed.
                                allowed = not self._message_armed
                            elif not self._message_armed:
                                allowed = not is_direct_mutation
                            elif self._message_armed:
                                allowed = bool(self._message_forwarded is None and self._message_body
                                               and _message_form_matches(
                                                   form, recipient_id=owner_id, body=self._message_body,
                                               ))
                                if allowed:
                                    self._message_forwarded = request
                if allowed:
                    await route.continue_()
                    return
            if self._profile_target:
                handle, owner_id = self._profile_target
                allowed = _read_request(request, handle, owner_id)
                # This obsolete endpoint can return 429 while the authenticated
                # profile GraphQL succeeds. It is not used as identity evidence.
                if parsed.path == "/api/v1/users/web_profile_info/":
                    allowed = False
                if parsed.scheme == "https" and parsed.netloc == "www.instagram.com":
                    if (request.method == "GET" and request.resource_type == "document"
                            and parsed.path == f"/{handle}/reels/" and not parsed.query):
                        allowed = True
                    form = _form(request) if request.method == "POST" else None
                    if (parsed.path in {"/graphql/query", "/api/graphql"}
                            and not parsed.query and not parsed.fragment and form
                            and form[0] in {"PolarisProfileReelsTabContentQuery", "PolarisProfileReelsTabContentQuery_connection"}):
                        variables = form[1]
                        data = variables.get("data")
                        allowed = (variables.get("user_id", variables.get("id")) == owner_id
                                   and isinstance(data, dict)
                                   and data.get("target_user_id") == owner_id
                                   and (form[0] == "PolarisProfileReelsTabContentQuery"
                                        or variables.get("after") == self._reel_cursor))
            if allowed:
                await route.continue_()
            else:
                await route.abort()
        except Exception:  # noqa: BLE001 - route failures must block the request
            await route.abort()

    async def _refresh_message_session(self):
        cookies = await self.context.cookies(_ORIGIN)
        identities = {c.get("value") for c in cookies if c.get("name") == "ds_user_id"}
        csrf = [c.get("value") for c in cookies if c.get("name") == "csrftoken"]
        if identities != {self.sender_id} or len(csrf) != 1 or not isinstance(csrf[0], str) or not csrf[0]:
            raise ReelBrowserError("message_session_changed")
        self.headers.update({
            "X-CSRFToken": csrf[0],
            "Origin": _ORIGIN,
            "Referer": _ORIGIN + "/direct/inbox/",
        })

    async def _verify_message_session(self):
        await self._refresh_message_session()
        inbox = await self._request("GET", "/api/v1/direct_v2/inbox/?limit=1")
        viewer = inbox.get("viewer")
        if (not isinstance(viewer, dict) or str(viewer.get("pk")) != self.sender_id
                or viewer.get("username") != self.sender_handle):
            raise ReelBrowserError("sender_identity_mismatch", "identity_mismatch", http_status=200)
        await self._refresh_message_session()

    async def _send_from_profile(self, *, recipient_id, body, before_mutation):
        """Send through Instagram's web UI; the private API rejects web cookies."""
        page = await self._ensure_page()
        evidence = {
            "sender_id": self.sender_id,
            "recipient_id": recipient_id,
            "mutation_attempted": False,
            "server_confirmed": False,
            "transport": "instagram_message_ui",
        }
        try:
            await self._verify_message_session()
        except ReelBrowserError as error:
            evidence.update(phase="session_preflight", reason=error.reason)
            if error.http_status is not None:
                evidence["http_status"] = error.http_status
            if error.health_signal:
                evidence["health_signal"] = error.health_signal
            return "deferred", evidence
        except Exception:  # noqa: BLE001 - preflight failure never arms a send
            return "deferred", {**evidence, "phase": "session_preflight",
                                 "reason": "message_session_unavailable"}
        evidence["session_verified"] = True
        self._message_target = (self.recipient_handle, recipient_id)
        self._message_body = body
        self._message_forwarded = None
        self._message_status = None
        self._message_armed = False
        response_done = asyncio.Event()
        response_payload = None
        response_headers = {}
        response_tasks = set()

        async def capture(response):
            nonlocal response_payload, response_headers
            if response.request is self._message_forwarded:
                self._message_status = response.status
                response_headers = dict(response.headers)
                try:
                    raw = await response.body()
                    if isinstance(raw, bytes) and len(raw) <= 65536:
                        response_payload = json.loads(raw)
                except Exception:  # noqa: BLE001 - unreadable bodies cannot confirm delivery
                    response_payload = None
                response_done.set()

        def observe(response):
            task = asyncio.create_task(capture(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", observe)
        try:
            # Instagram labels the profile action "Messages" in the current
            # web UI, while older layouts used the singular "Message".
            message_name = re.compile(r"^(?:Send )?Messages?$", re.IGNORECASE)
            button = page.get_by_role("button", name=message_name)
            if not await _one_visible(button):
                # Some Instagram profile layouts expose the same action as a
                # link instead of a button. Keep the text exact and bounded
                # so navigation links cannot arm the sender accidentally.
                button = page.get_by_role("link", name=message_name)
            if not await _one_visible(button):
                button = page.locator(
                    'button:has-text("Send message"), button:has-text("Messages"), '
                    'button:has-text("Message"), a:has-text("Send message"), '
                    'a:has-text("Messages"), a:has-text("Message"), '
                    '[role="button"]:has-text("Send message"), '
                    '[role="button"]:has-text("Messages"), '
                    '[role="button"]:has-text("Message"), '
                    '[aria-label="Send message"], [aria-label="Messages"], [aria-label="Message"]'
                )
            if not await _one_visible(button):
                raise ReelBrowserError("message_composer_unavailable")
            await button.click(timeout=12000)
            composer = page.locator(
                'textarea[placeholder*="Message"]:visible, '
                '[contenteditable="true"][role="textbox"]:visible, '
                '[contenteditable="true"]:visible'
            )
            if not await _one_visible(composer):
                raise ReelBrowserError("message_composer_unavailable")
            await composer.fill(body)
            send_button = page.get_by_role("button", name="Send", exact=True)
            if not await _one_visible(send_button) or not await send_button.is_enabled():
                raise ReelBrowserError("message_send_control_unavailable")
            if await before_mutation() is not True:
                return "deferred", {"reason": "message_policy_changed"}
            if self._send_started:
                return "deferred", {"reason": "message_already_attempted"}
            self._send_started = True
            self._message_armed = True
            evidence["mutation_attempted"] = True
            await send_button.click(timeout=12000)
            try:
                await asyncio.wait_for(response_done.wait(), timeout=20)
            except asyncio.TimeoutError:
                raise ReelBrowserError("message_response_unobserved") from None
            if type(self._message_status) is not int:
                raise ReelBrowserError("message_response_unobserved")
            evidence["http_status"] = self._message_status
            failure = _message_response_failure(
                self._message_status, response_headers, response_payload, session_verified=True,
            )
            if failure:
                raise failure
            evidence["phase"] = "receipt_readback"
            match = _THREAD_PATH.search(urlsplit(page.url).path)
            thread_id = match.group(1) if match else _nested_value(
                response_payload, ("thread_id", "threadId", "thread_v2_id")
            )
            receipt = None
            if not thread_id:
                # Some successful UI sends leave the page on the profile and
                # omit the thread id from the GraphQL response. Re-read the
                # inbox to identify the exact item without sending again.
                inbox = await self._request("GET", "/api/v1/direct_v2/inbox/?limit=100")
                receipt = _inbox_message_receipt(
                    inbox, sender_id=self.sender_id, recipient_id=recipient_id, body=body,
                )
                if receipt:
                    thread_id = receipt["thread_id"]
                    evidence["receipt_source"] = "inbox"
            if not thread_id:
                raise ReelBrowserError("message_receipt_missing")
            await self._refresh_message_session()
            thread = await self._request("GET", f"/api/v1/direct_v2/threads/{thread_id}/?limit=20")
            items = thread.get("thread", {}).get("items", []) if isinstance(thread, dict) else []
            expected_item_id = receipt["item_id"] if receipt else None
            matches = [
                item for item in items
                if isinstance(item, dict)
                and _bounded_id(str(item.get("item_id")))
                and (expected_item_id is None or str(item.get("item_id")) == expected_item_id)
                and str(item.get("user_id")) == self.sender_id
                and ((item.get("item_type") == "text" and item.get("text") == body)
                     or (item.get("item_type") == "link" and isinstance(item.get("link"), dict)
                         and item["link"].get("text") == body))
            ]
            item_id = matches[0].get("item_id") if len(matches) == 1 else None
            if not item_id or not verified_thread_item(
                thread, thread_id=thread_id, item_id=item_id,
                sender_id=self.sender_id, recipient_id=recipient_id, body=body,
            ):
                raise ReelBrowserError("message_readback_unconfirmed")
            return "sent", {**evidence, "reason": "message_sent", "item_id": item_id,
                             "thread_id": thread_id,
                             "server_confirmed": True}
        except ReelBrowserError as error:
            evidence["reason"] = error.reason
            if error.health_signal:
                evidence["health_signal"] = error.health_signal
        except Exception:  # noqa: BLE001 - preserve uncertain transport outcomes
            evidence["reason"] = "message_transport_uncertain"
        finally:
            page.remove_listener("response", observe)
            for task in tuple(response_tasks):
                task.cancel()
            await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
            self._message_armed = False
            self._message_target = None
            self._message_body = None
            self._message_forwarded = None
            self._message_status = None
        # A browser control or composer failure happens before the send button
        # is armed. It is safe to release the reservation for a later worker
        # attempt; only an armed transport belongs in the uncertain bucket.
        return ("uncertain" if evidence["mutation_attempted"] else "deferred"), evidence

    async def send(self, *, sender_id, recipient_id, reel_url, body, before_mutation):
        if self._send_started:
            return "deferred", {"reason": "message_already_attempted"}
        if (self.sender_id != sender_id or self.recipient_id != recipient_id
                or not self.sender_handle or not self.recipient_handle):
            return "deferred", {"reason": "message_identity_unverified"}
        suffix = "\n" + reel_url
        if not isinstance(body, str) or not body.endswith(suffix):
            return "deferred", {"reason": "message_payload_invalid"}
        try:
            if compose_message(reel_url, body[:-len(suffix)]) != body:
                raise ValueError("message mismatch")
        except (ValueError, TypeError):
            return "deferred", {"reason": "message_payload_invalid"}
        # Recheck the recipient in the browser immediately before arming the UI.
        self._profile_target = None
        profile = await self._inspect_profile(self.recipient_handle, recipient_id)
        if (profile.get("owner_id") != recipient_id
                or profile.get("handle") != self.recipient_handle):
            return "deferred", {"reason": "recipient_identity_mismatch"}
        if profile.get("following") is not True:
            return "deferred", {"reason": "recipient_no_longer_followed"}
        return await self._send_from_profile(
            recipient_id=recipient_id, body=body, before_mutation=before_mutation,
        )

    async def _inspect_profile(self, handle, owner_id):
        self._profile_target = (handle, owner_id)
        page = await self._ensure_page()
        try:
            return await PlaywrightFollowerBrowser(page).inspect(handle)
        except FollowerBrowserError as error:
            health = error.reason if error.reason in {
                "rate_limited", "authentication_required", "checkpoint_required"
            } else None
            raise ReelBrowserError(error.reason, health) from error

    async def verify_accounts(self, sender_id, sender_handle, recipient_id, recipient_handle):
        self.sender_id = self.recipient_id = None
        if (not all(isinstance(v, str) and _ID.fullmatch(v) for v in (sender_id, recipient_id))
                or sender_id == recipient_id
                or not all(isinstance(v, str) and _HANDLE.fullmatch(v)
                           for v in (sender_handle, recipient_handle))):
            raise ReelBrowserError("invalid_message_identity")
        cookies = await self.context.cookies(_ORIGIN)
        identities = {c.get("value") for c in cookies if c.get("name") == "ds_user_id"}
        csrf = [c.get("value") for c in cookies if c.get("name") == "csrftoken"]
        if identities != {sender_id} or len(csrf) != 1 or not isinstance(csrf[0], str) or not csrf[0]:
            raise ReelBrowserError("sender_identity_mismatch", "identity_mismatch")
        self.headers["X-CSRFToken"] = csrf[0]
        # The native inbox exposes the authenticated viewer. Own-profile reads
        # have no friendship_status and cannot use the prospect-profile parser.
        inbox = await self._request("GET", "/api/v1/direct_v2/inbox/?limit=1")
        sender = inbox.get("viewer")
        if (not isinstance(sender, dict) or str(sender.get("pk")) != sender_id
                or sender.get("username") != sender_handle):
            raise ReelBrowserError("sender_identity_mismatch", "identity_mismatch")
        recipient = await self._inspect_profile(recipient_handle, recipient_id)
        if recipient.get("owner_id") != recipient_id or recipient.get("handle") != recipient_handle:
            raise ReelBrowserError("recipient_identity_mismatch")
        if recipient.get("following") is not True:
            raise ReelBrowserError("recipient_no_longer_followed")
        self.sender_id, self.recipient_id = sender_id, recipient_id
        self.sender_handle = sender_handle
        self.recipient_handle = recipient_handle

    async def read_reels(self, sender_id):
        if self.sender_id != sender_id or not self.sender_handle:
            raise ReelBrowserError("sender_not_verified")
        self._profile_target = (self.sender_handle, sender_id)
        page = await self._ensure_page()
        pages = asyncio.Queue()
        tasks = set()
        candidates = []
        pagination_form = None

        async def consume(response):
            parsed = urlsplit(response.url)
            if (parsed.hostname != "www.instagram.com"
                    or parsed.path not in {"/api/graphql", "/graphql/query"}):
                return
            form = _form(response.request)
            if not form or form[0] not in {
                    "PolarisProfileReelsTabContentQuery", "PolarisProfileReelsTabContentQuery_connection"}:
                return
            is_next = form[0].endswith("_connection")
            if is_next and form[1].get("after") != self._reel_cursor:
                return
            try:
                payload = await PlaywrightFollowerBrowser._read_payload(response)
                batch, cursor = reel_candidates(payload, sender_id)
                fields = None
                if is_next:
                    raw = parse_qs(response.request.post_data, keep_blank_values=True)
                    if any(len(v) != 1 for v in raw.values()):
                        raise ValueError("ambiguous reel query")
                    fields = {k: v[0] for k, v in raw.items()}
                await pages.put((batch, cursor, fields))
            except Exception as error:  # noqa: BLE001 - forward read failure to the bounded caller
                await pages.put(error)

        def observe(response):
            task = asyncio.create_task(consume(response))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def receive():
            value = await asyncio.wait_for(pages.get(), timeout=20)
            if isinstance(value, Exception):
                raise value
            return value

        page.on("response", observe)
        try:
            navigation = await page.goto(f"{_ORIGIN}/{self.sender_handle}/reels/",
                                         wait_until="domcontentloaded", timeout=20000)
            await PlaywrightFollowerBrowser(page)._check_page()
            if navigation is not None:
                PlaywrightFollowerBrowser._check_status(navigation.status)
            batch, cursor, _ = await receive()
            candidates.extend(batch)
            if cursor:
                self._reel_cursor = cursor
                last_tile = page.locator('a[href*="/reel/"]').last
                await last_tile.wait_for(state="attached", timeout=10000)
                await last_tile.scroll_into_view_if_needed(timeout=10000)
                await page.mouse.wheel(0, 2000)
                batch, cursor, pagination_form = await receive()
                if pagination_form is None:
                    raise ValueError("pagination query not observed")
                candidates.extend(batch)
        except FollowerBrowserError as error:
            health = error.reason if error.reason in {
                "rate_limited", "authentication_required", "checkpoint_required"
            } else None
            raise ReelBrowserError(error.reason, health) from error
        except (ValueError, TimeoutError) as error:
            raise ReelBrowserError("reel_feed_unreadable") from error
        finally:
            page.remove_listener("response", observe)
            for task in tuple(tasks):
                task.cancel()
            await asyncio.gather(*tuple(tasks), return_exceptions=True)
            # Continue only the measured pagination request; no ambient page
            # queries remain running during media reads or the eventual send.
            await self.close()
            self._reel_cursor = None

        seen_cursors = set()
        for _ in range(18):
            if cursor is None:
                break
            if cursor in seen_cursors or pagination_form is None or len(candidates) > 250:
                raise ReelBrowserError("reel_pagination_changed")
            seen_cursors.add(cursor)
            variables = json.loads(pagination_form["variables"])
            variables["after"] = cursor
            # The UI requests four tiles at a time despite its page_size=12.
            # Use that bounded page capacity so overlaps do not exhaust our
            # page ceiling before the complete owned feed has been read.
            variables["first"] = 12
            pagination_form["variables"] = json.dumps(variables)
            response = await self.context.request.fetch(
                _ORIGIN + "/graphql/query", method="POST", form=pagination_form,
                headers=self.headers, timeout=12000, max_redirects=0, max_retries=0,
            )
            try:
                batch, cursor = reel_candidates(await PlaywrightFollowerBrowser._read_payload(response), sender_id)
                candidates.extend(batch)
            except (ValueError, FollowerBrowserError) as error:
                health = error.reason if isinstance(error, FollowerBrowserError) and error.reason in {
                    "rate_limited", "authentication_required", "checkpoint_required"
                } else None
                raise ReelBrowserError("reel_pagination_unreadable", health) from error
            finally:
                await response.dispose()
        if cursor is not None or len(candidates) > 250:
            raise ReelBrowserError("reel_feed_too_large")
        distinct = {}
        for candidate in candidates:
            previous = distinct.get(candidate["id"])
            if previous is not None and previous["code"] != candidate["code"]:
                raise ReelBrowserError("reel_identity_changed")
            distinct[candidate["id"]] = candidate
        if len(distinct) < 3:
            raise ReelBrowserError("three_sender_reels_required")
        semaphore = asyncio.Semaphore(3)

        async def detail(candidate):
            async with semaphore:
                payload = await self._request("GET", f"/api/v1/media/{candidate['id']}/info/")
                try:
                    return verified_reel_detail(payload, candidate, sender_id)
                except (ValueError, TypeError, OverflowError, OSError) as error:
                    raise ReelBrowserError("reel_detail_unverified") from error

        reads = [asyncio.create_task(detail(candidate)) for candidate in distinct.values()]
        try:
            return await asyncio.gather(*reads)
        except BaseException:
            for task in reads:
                task.cancel()
            await asyncio.gather(*reads, return_exceptions=True)
            raise
