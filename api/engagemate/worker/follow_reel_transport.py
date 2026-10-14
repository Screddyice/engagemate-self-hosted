"""One exact-recipient message request with durable-gate and readback evidence."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from engagemate.worker.follow_reels import compose_message

_ORIGIN = "https://www.instagram.com"
_APP_ID = "936619743392459"


class ReelBrowserError(RuntimeError):
    def __init__(self, reason: str, health_signal: str | None = None, *, http_status: int | None = None):
        self.reason, self.health_signal = reason, health_signal
        self.http_status = http_status
        super().__init__(reason)


def verified_thread_item(payload, *, thread_id, item_id, sender_id, recipient_id, body):
    """Confirm this exact message in a one-to-one thread with the claimed user."""
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False
    thread = payload.get("thread")
    if not isinstance(thread, dict) or str(thread.get("thread_id")) != thread_id:
        return False
    users = thread.get("users")
    if not isinstance(users, list) or len(users) != 1:
        return False
    if not isinstance(users[0], dict) or str(users[0].get("pk")) != recipient_id:
        return False
    if thread.get("is_group") is not False:
        return False
    items = thread.get("items")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict)
        and item.get("item_id") == item_id
        and str(item.get("user_id")) == sender_id
        and (
            (item.get("item_type") == "text" and item.get("text") == body)
            or (item.get("item_type") == "link" and isinstance(item.get("link"), dict)
                and item["link"].get("text") == body)
        )
        for item in items
    )


class FollowReelTransport:
    def __init__(self, context):
        self.context = context
        self.headers = {"X-IG-App-ID": _APP_ID, "X-Requested-With": "XMLHttpRequest"}
        self.sender_id = None
        self.sender_handle = None
        self.recipient_id = None
        self._send_started = False

    async def _request(self, method, path, *, form=None):
        # All paths are constructed below from validated numeric IDs. Never
        # follow a Location header with the session or a message body.
        response = await self.context.request.fetch(
            _ORIGIN + path, method=method, form=form, headers=self.headers,
            timeout=12000, max_redirects=0, max_retries=0,
        )
        try:
            if response.status == 429:
                raise ReelBrowserError("instagram_rate_limited", "rate_limited", http_status=429)
            if 300 <= response.status < 400:
                location = response.headers.get("location", "")
                try:
                    destination = urlsplit(location)
                except ValueError:
                    raise ReelBrowserError("instagram_redirected", http_status=response.status) from None
                path = destination.path.lower().rstrip("/")
                instagram_destination = (
                    destination.scheme in ("", "https")
                    and destination.hostname in (None, "instagram.com", "www.instagram.com")
                )
                if instagram_destination and (
                    path in ("/challenge", "/checkpoint")
                    or path.startswith(("/challenge/", "/checkpoint/"))
                ):
                    raise ReelBrowserError("instagram_checkpoint", "checkpoint_required", http_status=response.status)
                if instagram_destination and (
                    path == "/accounts/login" or path.startswith("/accounts/login/")
                ):
                    raise ReelBrowserError("instagram_login_required", "authentication_required", http_status=response.status)
                # Instagram also redirects authenticated API requests to its
                # home page during temporary restrictions. This response alone
                # cannot establish that the saved session needs a new login.
                raise ReelBrowserError("instagram_redirected", http_status=response.status)
            try:
                payload = await response.json()
            except Exception:
                raise ReelBrowserError("instagram_response_unreadable", http_status=response.status) from None
            if not isinstance(payload, dict):
                raise ReelBrowserError("instagram_response_unreadable", http_status=response.status)
            if payload.get("challenge") or payload.get("checkpoint_url"):
                raise ReelBrowserError("instagram_checkpoint", "checkpoint_required", http_status=response.status)
            message = payload.get("message")
            if (payload.get("error_type") == "csrf_failed"
                    or (isinstance(message, str) and "csrf" in message.lower())):
                raise ReelBrowserError("instagram_csrf_rejected", http_status=response.status)
            if message == "login_required" or payload.get("require_login") is True:
                raise ReelBrowserError("instagram_login_required", "authentication_required", http_status=response.status)
            # Forbidden can also mean a rejected CSRF context or messaging
            # permission. Its status alone does not establish an expired login.
            if response.status != 200:
                raise ReelBrowserError("instagram_request_refused", http_status=response.status)
            if payload.get("status") != "ok":
                raise ReelBrowserError("instagram_request_refused", http_status=response.status)
            return payload
        finally:
            await response.dispose()

    async def send(self, *, sender_id, recipient_id, reel_url, body, before_mutation):
        if (self.sender_id != sender_id or self.recipient_id != recipient_id
                or not isinstance(self.sender_handle, str) or not self.sender_handle):
            return "deferred", {"reason": "message_identity_unverified"}
        if self._send_started:
            return "deferred", {"reason": "message_already_attempted"}
        suffix = "\n" + reel_url
        if not isinstance(body, str) or not body.endswith(suffix):
            return "deferred", {"reason": "message_payload_invalid"}
        try:
            if compose_message(reel_url, body[:-len(suffix)]) != body:
                raise ValueError("message mismatch")
        except (ValueError, TypeError):
            return "deferred", {"reason": "message_payload_invalid"}
        # Build everything before arming. No second POST, including on a timeout.
        context_id = str(uuid4())
        form = {"recipient_users": json.dumps([[recipient_id]]), "text": body,
                "action": "send_item", "client_context": context_id,
                "mutation_token": context_id}
        async def refresh_session():
            cookies = await self.context.cookies(_ORIGIN)
            identities = {c.get("value") for c in cookies if c.get("name") == "ds_user_id"}
            csrf = [c.get("value") for c in cookies if c.get("name") == "csrftoken"]
            if identities != {sender_id} or len(csrf) != 1 or not isinstance(csrf[0], str) or not csrf[0]:
                raise ReelBrowserError("message_session_changed")
            self.headers.update({"X-CSRFToken": csrf[0], "Origin": _ORIGIN,
                                 "Referer": _ORIGIN + "/direct/inbox/"})
        # Reel reads can outlive the authenticated session. Confirm the server's
        # viewer immediately before the durable attempt gate, then refresh any
        # cookies the inbox response changed before constructing the write.
        preflight = {"phase": "session_preflight", "mutation_attempted": False,
                     "server_confirmed": False}
        session_verified = False
        try:
            await refresh_session()
            inbox = await self._request("GET", "/api/v1/direct_v2/inbox/?limit=1")
            viewer = inbox.get("viewer")
            if (not isinstance(viewer, dict) or str(viewer.get("pk")) != sender_id
                    or viewer.get("username") != self.sender_handle):
                raise ReelBrowserError("sender_identity_mismatch", "identity_mismatch", http_status=200)
            await refresh_session()
            session_verified = True
        except ReelBrowserError as error:
            preflight["reason"] = error.reason
            if error.http_status is not None:
                preflight["http_status"] = error.http_status
            if error.health_signal:
                preflight["health_signal"] = error.health_signal
            return "deferred", preflight
        except Exception:
            return "deferred", {**preflight, "reason": "message_session_unavailable"}
        if await before_mutation() is not True:
            return "deferred", {"reason": "message_policy_changed"}
        self._send_started = True
        evidence = {"sender_id": sender_id, "recipient_id": recipient_id,
                    "mutation_attempted": True, "server_confirmed": False,
                    "phase": "send_request",
                    "transport": "instagram_authenticated_request"}
        try:
            payload = await self._request("POST", "/api/v1/direct_v2/threads/broadcast/text/", form=form)
            result = payload.get("payload")
            if not isinstance(result, dict):
                raise ReelBrowserError("message_receipt_missing", http_status=200)
            item_id, thread_id = result.get("item_id"), result.get("thread_id")
            if (not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,100}", item_id)
                    or not isinstance(thread_id, str) or not re.fullmatch(r"[0-9]{1,100}", thread_id)):
                raise ReelBrowserError("message_receipt_missing", http_status=200)
            evidence.update(item_id=item_id, thread_id=thread_id)
            evidence["phase"] = "receipt_readback"
            thread = await self._request("GET", f"/api/v1/direct_v2/threads/{thread_id}/?limit=20")
            if not verified_thread_item(thread, thread_id=thread_id, item_id=item_id,
                                        sender_id=sender_id, recipient_id=recipient_id, body=body):
                raise ReelBrowserError("message_readback_unconfirmed", http_status=200)
            return "sent", {**evidence, "server_confirmed": True}
        except ReelBrowserError as error:
            if error.reason == "instagram_login_required" and session_verified:
                evidence["reason"] = "message_transport_refused"
            else:
                evidence["reason"] = error.reason
            if error.http_status is not None:
                evidence["http_status"] = error.http_status
            if error.health_signal and not (error.reason == "instagram_login_required" and session_verified):
                evidence["health_signal"] = error.health_signal
        except Exception:
            evidence["reason"] = "message_transport_uncertain"
        return "uncertain", evidence
