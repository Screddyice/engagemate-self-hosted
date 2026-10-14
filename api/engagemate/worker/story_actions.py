"""Claim-bound Story actions with guarded writes and explicit server receipts.

The executor owns the durable claim. This adapter opens its disposable page
only after a successful preflight health check; construction performs no I/O.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from engagemate.worker.circuit_breaker import HealthSignal
from engagemate.worker.instagram_adapter import ActionOutcome
from engagemate.worker.story_availability import (
    loaded_story_media,
    story_media_unavailable,
    visible_story_unavailable,
)
from engagemate.worker.story_identity import (
    StoryIdentityError,
    parse_seen_mutation,
    verify_opportunity_identity,
)
from engagemate.worker.story_preview import _read_request
from engagemate.worker.story_types import StoryOpportunity

_SEEN = "PolarisStoriesV3SeenMutation"
_LIKE = "usePolarisStoriesV4LikeMutationLikeMutation"
_REPLY = "IGDirectStoryShareReplyMutation"
_HOSTS = frozenset({"www.instagram.com", "instagram.com"})
_GRAPHQL_PATHS = frozenset({"/api/graphql", "/graphql/query"})
_MAX_ACK = 65_536
_ID = re.compile(r"[1-9][0-9]{0,31}\Z")
_SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_SCHEMA_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
# Measured from the exact Relay .graphql modules on 2026-09-13 under the
# mutation-blocked preview guard. Both operations select only __typename.
# These declarations establish the expected schema, not a live action receipt.
_RECEIPT_SCHEMAS = {
    "seen": ("xdt_mark_story_reel_seen", "XDTMarkSeenResponse"),
    "like": ("xig_send_story_like", "XIGSendStoryLikeResponsePayload"),
}


def _form(request: Any) -> tuple[str, dict] | None:
    try:
        raw = request.post_data
    except UnicodeDecodeError:
        # Playwright decodes this property as UTF-8; binary Instagram telemetry
        # must stay blocked without breaking the route listener.
        return None
    if type(raw) is not str or not raw or len(raw) > 131_072:
        return None
    try:
        fields = parse_qs(
            raw, keep_blank_values=True, strict_parsing=True, max_num_fields=128
        )
        names, values = fields.get("fb_api_req_friendly_name"), fields.get("variables")
        if (
            names is None
            or len(names) != 1
            or values is None
            or len(values) != 1
            or len(values[0]) > 16_384
        ):
            return None
        variables = json.loads(values[0], object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        return None
    if type(variables) is not dict:
        return None
    return names[0], variables


def _reply_variables_match(
    variables: object, owner_id: str, media_id: str, text: str
) -> bool:
    """Accept only the measured native Story reply, with one exact recipient."""
    if type(variables) is not dict or set(variables) != {"data", "send_data"}:
        return False
    data, send = variables["data"], variables["send_data"]
    if (
        type(data) is not dict
        or set(data)
        != {
            "is_shh_mode",
            "media_id",
            "reaction_emoji",
            "reel_id",
            "sampled",
            "share_client_context",
            "text",
        }
        or type(send) is not dict
        or set(send)
        != {
            "forwarded_from_thread_id",
            "is_forwarded_from_own_message",
            "offline_threading_id",
            "recipient_users",
            "thread_id",
        }
        or data["is_shh_mode"] is not False
        or data["media_id"] != media_id
        or data["reel_id"] != owner_id
        or data["reaction_emoji"] is not None
        or data["sampled"] is not False
        or data["share_client_context"] != send["offline_threading_id"]
        or type(data["text"]) is not dict
        or data["text"] != {"sensitive_string_value": text}
        or any(
            send[key] is not None
            for key in (
                "thread_id",
                "forwarded_from_thread_id",
                "is_forwarded_from_own_message",
            )
        )
    ):
        return False
    for value in (data["share_client_context"], send["offline_threading_id"]):
        if (
            type(value) is not str
            or not 1 <= len(value) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            return False
    recipients = send["recipient_users"]
    if type(recipients) is not str or len(recipients) > 128:
        return False
    try:
        return json.loads(recipients) == [owner_id]
    except ValueError:
        return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("ambiguous JSON")
        output[key] = value
    return output


def _graphql(url: str) -> bool:
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() in _HOSTS
        and parsed.path.rstrip("/") in _GRAPHQL_PATHS
    )


def _nodes(payload: object):
    pending = [(payload, 0)]
    visited = 0
    while pending:
        node, depth = pending.pop()
        visited += 1
        if visited > 256 or depth > 8:
            raise ValueError("acknowledgment exceeds bounds")
        if type(node) is dict:
            yield node
            pending.extend(
                (v, depth + 1) for v in node.values() if type(v) in (dict, list)
            )
        elif type(node) is list:
            pending.extend((v, depth + 1) for v in node if type(v) in (dict, list))


def _restriction(status: int, payload: object) -> HealthSignal | None:
    if status == 429:
        return HealthSignal.RATE_LIMIT
    if status == 401:
        return HealthSignal.LOGIN_REQUIRED
    try:
        for node in _nodes(payload):
            if node.get("require_login") is True:
                return HealthSignal.LOGIN_REQUIRED
            if node.get("checkpoint_url"):
                return HealthSignal.CHECKPOINT
            if node.get("challenge"):
                return HealthSignal.CHALLENGE
            for key in ("message", "error_type", "error_user_title", "error_user_msg"):
                value = node.get(key)
                if type(value) is not str:
                    continue
                message = value[:512].lower()
                if "checkpoint" in message:
                    return HealthSignal.CHECKPOINT
                if "challenge_required" in message:
                    return HealthSignal.CHALLENGE
                if "login_required" in message or "login required" in message:
                    return HealthSignal.LOGIN_REQUIRED
                if any(
                    term in message
                    for term in ("please wait", "rate limit", "feedback_required")
                ):
                    return HealthSignal.RATE_LIMIT
    except ValueError:
        return HealthSignal.HEALTH_UNAVAILABLE
    return None


def _acknowledged(
    payload: object,
    media_id: str,
    owner_id: str,
    *,
    operation: str = "seen",
) -> bool:
    if type(payload) is not dict or type(payload.get("data")) is not dict:
        return False
    data = payload["data"]
    if len(data) != 1:
        return False
    expected = _RECEIPT_SCHEMAS.get(operation) if type(operation) is str else None
    if expected is None:
        return False
    field, typename = expected
    result = data.get(field)
    if type(result) is not dict or result.get("__typename") != typename:
        return False
    try:
        for node in _nodes(payload):
            if node.get("errors") or node.get("error") or node.get("success") is False:
                return False
            if "status" in node and node["status"] != "ok":
                return False
            for key in ("media_id", "reelMediaId"):
                if key in node and node[key] != media_id:
                    return False
            for key in ("owner_id", "reelMediaOwnerId", "reelId"):
                if key in node and node[key] != owner_id:
                    return False
    except ValueError:
        return False
    return True


def _reply_acknowledged(
    payload: object,
    media_id: str,
    owner_id: str,
    *,
    attempt_started_at: datetime | None,
    received_at: datetime,
) -> bool:
    """Confirm server acceptance, never recipient delivery or reading.

    The measured publisher uses message_id as its authoritative message ID.
    Its Relay selection requests message_id, timestamp_ms and id, not __typename.
    """
    if type(payload) is not dict or type(payload.get("data")) is not dict:
        return False
    data = payload["data"]
    field = "direct_story_share_reply_with_slide_message_response"
    if set(data) != {field} or type(data[field]) is not dict:
        return False
    result = data[field]
    for key in ("message_id", "id"):
        value = result.get(key)
        if (
            type(value) is not str
            or not 1 <= len(value) <= 128
            or any(ord(character) <= 32 or ord(character) >= 127 for character in value)
        ):
            return False
    stamp = result.get("timestamp_ms")
    if type(stamp) is not str or re.fullmatch(r"[0-9]{13}", stamp) is None:
        return False
    if (
        type(attempt_started_at) is not datetime
        or type(received_at) is not datetime
        or attempt_started_at.tzinfo is None
        or received_at.tzinfo is None
    ):
        return False
    if not 0 <= (received_at - attempt_started_at).total_seconds() <= 30:
        return False
    if (
        not int(attempt_started_at.timestamp() * 1000)
        <= int(stamp)
        <= int(received_at.timestamp() * 1000)
    ):
        return False
    try:
        for node in _nodes(payload):
            if node.get("errors") or node.get("error"):
                return False
            if "success" in node and node["success"] is not True:
                return False
            if "status" in node and node["status"] != "ok":
                return False
            for key in ("media_id", "reelMediaId"):
                if key in node and node[key] != media_id:
                    return False
            for key in ("owner_id", "reelMediaOwnerId", "reelId", "reel_id"):
                if key in node and node[key] != owner_id:
                    return False
    except ValueError:
        return False
    return _restriction(200, payload) is None


def _operation_schema(payload: object) -> dict | None:
    """Capture a direct Relay result type for measurement, never confirmation."""
    if type(payload) is not dict or type(payload.get("data")) is not dict:
        return None
    data = payload["data"]
    if len(data) != 1:
        return None
    field, operation = next(iter(data.items()))
    if (
        type(field) is not str
        or _SAFE_KEY.fullmatch(field) is None
        or type(operation) is not dict
    ):
        return None
    typename = operation.get("__typename")
    if type(typename) is not str or _SCHEMA_TYPE.fullmatch(typename) is None:
        return None
    return {"field": field, "typename": typename}


def _shape(payload: object) -> dict:
    """Return bounded field/type names and flags, without other scalar values."""
    entries: list[dict] = []
    try:
        for node in _nodes(payload):
            if len(entries) >= 12:
                break
            fields = {
                key: type(value).__name__
                for key, value in list(node.items())[:16]
                if type(key) is str and _SAFE_KEY.fullmatch(key)
            }
            entries.append(
                {
                    "fields": fields,
                    "status_ok": node.get("status") == "ok",
                    "has_errors": bool(node.get("errors") or node.get("error")),
                }
            )
    except ValueError:
        return {"objects": entries, "truncated": True}
    result = {"objects": entries, "truncated": False}
    schema = _operation_schema(payload)
    if schema is not None:
        result["operation_schema"] = schema
    return result


class LiveStoryAdapter:
    """Use one exact opportunity, one seen POST, and one selected action POST."""

    def __init__(
        self,
        page: Any,
        sink: Any,
        opportunity: StoryOpportunity,
        preflight_health: Callable[[], Awaitable[HealthSignal]],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_ms: int = 15_000,
        allow_reply_measurement: bool = False,
        reply_authorized: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        if type(allow_reply_measurement) is not bool:
            raise ValueError("allow_reply_measurement must be a boolean")
        self._allow_reply_measurement = allow_reply_measurement
        self._reply_authorized = reply_authorized
        now = clock()
        try:
            self._identity = verify_opportunity_identity(opportunity, now=now)
        except StoryIdentityError as exc:
            if (
                exc.reason != "story_expired_or_future"
                or (now - opportunity.taken_at).total_seconds() < 86_400
            ):
                raise
            # Retain full identity validation for an expired claim. _perform
            # records a known no-write skip without opening a browser page.
            self._identity = verify_opportunity_identity(opportunity, now=opportunity.taken_at)
        self._handle = opportunity.target_handle.lower()
        self._page, self._sink = page, sink
        self._preflight_health, self._clock = preflight_health, clock
        self._timeout_ms = max(100, min(timeout_ms, 30_000))
        self._target_url = f"https://www.instagram.com/stories/{self._handle}/{self._identity.media_id}/"
        self._action_page: Any = None
        self._cdp: Any = None
        self._preflight_signal: HealthSignal | None = None
        self._signal: HealthSignal | None = None
        self._attempted = False
        self._closing = False
        self._confirmed_before_cleanup = False
        self._cleanup_failed = False
        self._viewer_healthy = False
        self._like_armed = False
        self._reply_armed = False
        self._reply_text: str | None = None
        self._reply_attempt_started_at: datetime | None = None
        self._gate_clicked = False
        self._allowed: dict[str, Any] = {}
        self._receipts: dict[str, bool] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task] = set()
        self._diagnostics: list[dict] = []
        self._action = "view"
        self._stage = "preflight"
        self._skip_reason: str | None = None

    @property
    def confirmed_before_cleanup(self) -> bool:
        """A matched native action receipt survives operational cleanup."""
        return self._confirmed_before_cleanup

    @property
    def skip_reason(self) -> str | None:
        return self._skip_reason

    def _skip(self, reason: str) -> None:
        operation = "seen" if self._action == "view" else self._action
        # A Story disappearing after a forwarded write does not prove no send.
        if operation not in self._allowed and self._signal is None:
            self._skip_reason = reason
            for event in self._events.values():
                event.set()

    def _unexpired(self) -> bool:
        age = (self._clock() - self._identity.taken_at).total_seconds()
        if age >= 86_400:
            self._skip("story_expired")
        return 0 <= age < 86_400

    def _failed_outcome(self) -> ActionOutcome:
        operation = "seen" if self._action == "view" else self._action
        return (
            ActionOutcome.SKIPPED
            if self._skip_reason and self._signal is None and operation not in self._allowed
            else ActionOutcome.FAILED
        )

    def _latch(self, signal: HealthSignal) -> None:
        if self._signal is None or self._signal is HealthSignal.HEALTH_UNAVAILABLE:
            self._signal = signal

    @property
    def ack_diagnostics(self) -> tuple[dict, ...]:
        return tuple(json.loads(json.dumps(entry)) for entry in self._diagnostics)

    @property
    def action_evidence(self) -> str:
        """Durable fixed codes; never include response bodies, IDs, or message text."""
        operation = "seen" if self._action == "view" else self._action
        forwarded = "forwarded" if operation in self._allowed else "not_forwarded"
        receipt = self._receipts.get(operation)
        receipt_code = (
            "missing" if receipt is None else "accepted" if receipt else "rejected"
        )
        status = next(
            (item["http_status"] for item in self._diagnostics
             if item["operation"] == operation),
            0,
        )
        status = status if type(status) is int and 100 <= status <= 599 else 0
        health = self._signal or (
            (HealthSignal.NONE if self._viewer_healthy else HealthSignal.HEALTH_UNAVAILABLE)
            if self._attempted else self._preflight_signal
        ) or HealthSignal.HEALTH_UNAVAILABLE
        return (
            f"action={self._action},stage={self._stage},request={forwarded},"
            f"receipt={receipt_code},http={status},health={health.value}"
            + (",cleanup=failed" if self._cleanup_failed else "")
        )

    async def detect_health_signal(self) -> HealthSignal:
        if self._signal is not None:
            return self._signal
        if not self._attempted:
            try:
                signal = await self._preflight_health()
            except Exception:  # noqa: BLE001 -- Bound the injected health port without exposing diagnostics.
                signal = HealthSignal.HEALTH_UNAVAILABLE
            self._preflight_signal = (
                signal
                if type(signal) is HealthSignal
                else HealthSignal.HEALTH_UNAVAILABLE
            )
            return self._preflight_signal
        if self._action_page is None:
            return self._preflight_signal or HealthSignal.HEALTH_UNAVAILABLE
        if not self._closing:
            self._viewer_healthy = await self._viewer_matches()
        if self._signal is not None:
            return self._signal
        return (
            HealthSignal.NONE
            if self._viewer_healthy
            else HealthSignal.HEALTH_UNAVAILABLE
        )

    async def view_story(self, target_handle: str, fingerprint: str) -> ActionOutcome:
        return await self._perform(target_handle, fingerprint, like=False)

    async def like_story(self, target_handle: str, fingerprint: str) -> ActionOutcome:
        return await self._perform(target_handle, fingerprint, like=True)

    async def reply_to_story(
        self, target_handle: str, fingerprint: str, generated_text: str
    ) -> ActionOutcome:
        self._action = "reply"
        if (
            not self._allow_reply_measurement
            or type(generated_text) is not str
            or not generated_text.strip()
            or len(generated_text) > 200
        ):
            self._attempted = True
            return ActionOutcome.FAILED
        return await self._perform(
            target_handle, fingerprint, like=False, reply_text=generated_text
        )

    async def dm_about_story(
        self, target_handle: str, fingerprint: str, generated_text: str
    ) -> ActionOutcome:
        self._action = "dm"
        self._attempted = True
        return ActionOutcome.FAILED

    async def _route(self, route: Any, request: Any) -> None:
        if self._closing or self._signal is not None or self._skip_reason is not None:
            await route.abort()
            return
        if request.method in {"GET", "HEAD"}:
            if getattr(request, "resource_type", None) == "document":
                parsed = urlsplit(request.url)
                path = parsed.path.removesuffix("/")
                if parsed.netloc.lower() in _HOSTS:
                    if path.startswith(("/challenge", "/checkpoint")):
                        self._latch(HealthSignal.CHECKPOINT)
                    elif path.startswith("/accounts/"):
                        self._latch(HealthSignal.LOGIN_REQUIRED)
                if (
                    parsed.scheme != "https"
                    or parsed.netloc.lower() not in _HOSTS
                    or parsed.fragment
                    or parsed.query not in {"", "r=1"}
                    or path
                    not in {
                        f"/stories/{self._handle}",
                        f"/stories/{self._handle}/{self._identity.media_id}",
                    }
                ):
                    await route.abort()
                    return
            if _read_request(request, None):
                await route.continue_()
            else:
                await route.abort()
            return
        if request.method != "POST" or not _graphql(request.url):
            await route.abort()
            return
        parsed = _form(request)
        if parsed is None:
            await route.abort()
            return
        name, variables = parsed
        if _read_request(request, name):
            await route.continue_()
            return
        operation = None
        if name == _SEEN:
            now = self._clock()
            expired = (now - self._identity.taken_at).total_seconds() >= 86_400
            try:
                # Validate the entire native request even when expiry races it;
                # expiry alone must not become an account health failure.
                identity = parse_seen_mutation(
                    request.post_data,
                    expected_owner_id=self._identity.owner_id,
                    expected_handle=self._handle,
                    page_url=self._action_page.url,
                    now=self._identity.taken_at if expired else now,
                )
            except StoryIdentityError:
                identity = None
            if identity != self._identity:
                self._latch(HealthSignal.HEALTH_UNAVAILABLE)
            elif expired:
                self._skip("story_expired")
                await route.abort()
                return
            elif "seen" not in self._allowed:
                operation = "seen"
        elif name == _LIKE and self._like_armed and "like" not in self._allowed:
            values = variables.get("input")
            if (
                type(values) is dict
                and values.get("media_id") == self._identity.media_id
                and not (set(values) - {"media_id", "actor_id", "client_mutation_id"})
                and type(values.get("actor_id")) is str
                and _ID.fullmatch(values["actor_id"])
                and type(values.get("client_mutation_id")) is str
                and 0 < len(values["client_mutation_id"]) <= 128
            ):
                operation = "like"
        elif name == _REPLY and self._reply_armed and "reply" not in self._allowed:
            reply_url = urlsplit(request.url)
            if (
                reply_url.path == "/api/graphql"
                and not reply_url.query
                and not reply_url.fragment
                and self._reply_text is not None
                and _reply_variables_match(
                    variables,
                    self._identity.owner_id,
                    self._identity.media_id,
                    self._reply_text,
                )
            ):
                operation = "reply"
            else:
                self._latch(HealthSignal.HEALTH_UNAVAILABLE)
        if operation is None:
            await route.abort()
            return
        if operation in {"like", "reply"} and not await self._ready_for_action():
            await route.abort()
            return
        if operation == "reply" and self._reply_authorized is not None:
            try:
                authorized = await self._reply_authorized()
            except Exception:  # noqa: BLE001 - a revoked prerequisite fails closed
                authorized = False
            if authorized is not True:
                await route.abort()
                return
        # Identity/media checks await browser evidence. Recheck permission and
        # expiry after every await before atomically consuming the allowance.
        if (
            self._closing
            or self._signal is not None
            or operation in self._allowed
            or self._skip_reason is not None
            or not self._unexpired()
        ):
            await route.abort()
            return
        self._allowed[operation] = request
        await route.continue_()

    def _on_response(self, response: Any) -> None:
        task = asyncio.create_task(self._response(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _response(self, response: Any) -> None:
        operation = next(
            (
                key
                for key, request in self._allowed.items()
                if response.request is request
            ),
            None,
        )
        parsed_url = urlsplit(response.url)
        if parsed_url.hostname not in _HOSTS:
            return
        received_at = self._clock()
        status = response.status
        signal = _restriction(status, None)
        if signal is not None:
            self._latch(signal)
        if operation is None and not _graphql(response.url):
            return
        payload = None
        try:
            raw = await response.body()
            if type(raw) is bytes and len(raw) <= _MAX_ACK:
                payload = json.loads(raw, object_pairs_hook=_unique_object)
        except Exception:  # noqa: BLE001 -- Response decoding is an untrusted browser boundary.
            payload = None
        signal = _restriction(status, payload)
        if signal is not None:
            self._latch(signal)
        if operation is not None:
            confirmed = (
                200 <= status < 300
                and signal is None
                and (
                    _reply_acknowledged(
                        payload,
                        self._identity.media_id,
                        self._identity.owner_id,
                        attempt_started_at=self._reply_attempt_started_at,
                        received_at=received_at,
                    )
                    if operation == "reply"
                    else _acknowledged(
                        payload,
                        self._identity.media_id,
                        self._identity.owner_id,
                        operation=operation,
                    )
                )
            )
            self._receipts[operation] = confirmed
            self._diagnostics.append(
                {
                    "operation": operation,
                    "http_status": status,
                    "confirmed": confirmed,
                    "shape": _shape(payload),
                }
            )
            if operation == "reply":
                root = payload.get("data", {}) if type(payload) is dict else {}
                root = (
                    root.get("direct_story_share_reply_with_slide_message_response")
                    if type(root) is dict
                    else None
                )
                details = {"root_matches": type(root) is dict}
                if type(root) is dict:
                    for field in ("message_id", "timestamp_ms", "id"):
                        value = root.get(field)
                        details[field + "_type"] = type(value).__name__
                        if type(value) is str:
                            details[field + "_length"] = len(value)
                self._diagnostics[-1]["reply_receipt"] = details
            self._events[operation].set()

    async def _viewer_identity_matches(self) -> bool:
        try:
            parsed = urlsplit(self._action_page.url)
            path = parsed.path.removesuffix("/")
            if path.startswith(("/challenge", "/checkpoint")):
                self._latch(HealthSignal.CHECKPOINT)
                return False
            if path.startswith("/accounts/"):
                self._latch(HealthSignal.LOGIN_REQUIRED)
                return False
            if (
                parsed.scheme != "https"
                or parsed.netloc.lower() not in _HOSTS
                or parsed.fragment
                or parsed.query not in {"", "r=1"}
                or path
                not in {
                    f"/stories/{self._handle}",
                    f"/stories/{self._handle}/{self._identity.media_id}",
                }
            ):
                return False
            if await self._action_page.locator(
                'a[href^="/accounts/login/"], a[href^="/accounts/emailsignup/"]'
            ).count():
                self._latch(HealthSignal.LOGIN_REQUIRED)
                return False
            owner = self._action_page.locator(f'a[href="/{self._handle}/"]:visible')
            if not 1 <= await owner.count() <= 4:
                return False
            timestamp = self._action_page.locator("time[datetime]:visible")
            if await timestamp.count() != 1:
                return False
            raw_time = await timestamp.get_attribute("datetime")
            if type(raw_time) is not str or len(raw_time) > 64:
                return False
            taken = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            return (
                taken.tzinfo is not None
                and taken.astimezone(UTC) == self._identity.taken_at
            )
        except Exception:  # noqa: BLE001 -- Gate identity must be readable before its click.
            return False

    async def _viewer_matches(self) -> bool:
        if not self._unexpired():
            return False
        try:
            identity_matches = await self._viewer_identity_matches()
            if self._signal is not None:
                return False
            if await visible_story_unavailable(self._action_page):
                self._skip("story_unavailable")
                return False
            if not identity_matches:
                return False
            media = self._action_page.locator("video:visible, img:visible")
            large_loaded = 0
            count = await media.count()
            if count > 32:
                return False
            for index in range(count):
                item = media.nth(index)
                box = await item.bounding_box()
                if not box or box["width"] < 200 or box["height"] < 200:
                    continue
                loaded = await item.evaluate(
                    """el => ({tag: el.tagName, source: el.currentSrc || el.src,
                        loaded: el.tagName === 'VIDEO' ? el.readyState >= 2 :
                            el.tagName === 'IMG' && el.complete,
                        width: el.tagName === 'VIDEO' ? el.videoWidth : el.naturalWidth,
                        height: el.tagName === 'VIDEO' ? el.videoHeight : el.naturalHeight})"""
                )
                if story_media_unavailable(loaded):
                    self._skip("story_unavailable")
                    return False
                if loaded_story_media(loaded):
                    large_loaded += 1
            if await visible_story_unavailable(self._action_page):
                self._skip("story_unavailable")
                return False
            return large_loaded == 1 and self._unexpired()
        except Exception:  # noqa: BLE001 -- Unreadable viewer evidence must fail closed.
            return False

    async def _wait_viewer(self) -> bool:
        if self._signal is not None:
            await self._viewer_matches()
            return False
        async with asyncio.timeout(self._timeout_ms / 1000):
            while self._signal is None and self._skip_reason is None:
                if await self._viewer_matches():
                    self._viewer_healthy = True
                    return True
                if self._skip_reason is not None or self._signal is not None:
                    return False
                if not self._gate_clicked:
                    gate = self._action_page.get_by_role(
                        "button", name="View story", exact=False
                    )
                    count = await gate.count()
                    if count > 1:
                        self._latch(HealthSignal.HEALTH_UNAVAILABLE)
                        return False
                    # The gate can precede the profile anchors. Wait for
                    # exact identity evidence; never click during that gap.
                    if (
                        count == 1
                        and await gate.is_visible()
                        and await self._viewer_identity_matches()
                        and self._signal is None
                    ):
                        self._gate_clicked = True
                        await gate.click(timeout=self._timeout_ms)
                        continue
                await asyncio.sleep(0.1)
        return False

    async def _ready_for_action(self) -> bool:
        if await self._viewer_matches():
            return True
        self._skip("story_expired" if not self._unexpired() else "story_unavailable")
        return False

    async def _reply_action(self) -> bool:
        self._stage = "reply_control"
        reply = self._action_page.locator(
            f'textarea[placeholder^="Reply to {self._handle}"]:visible'
        )
        reply_count = await reply.count()
        if reply_count == 0:
            # Instagram omits the composer for Stories whose owner disabled
            # replies. No request can have been sent at this stage, so keep
            # the claim as a safe Story skip instead of raising an action
            # failure and consuming the reply allocation.
            self._skip("story_unavailable")
            return False
        if (
            reply_count != 1
            or not await reply.is_enabled()
            or not await self._ready_for_action()
            or self._reply_text is None
        ):
            return False
        placeholder = await reply.get_attribute("placeholder")
        if (
            type(placeholder) is not str
            or re.fullmatch(
                rf"Reply to {re.escape(self._handle)}(?:\.\.\.|…)",
                placeholder,
            )
            is None
        ):
            return False
        self._stage = "send_control"
        await reply.fill(self._reply_text, timeout=self._timeout_ms)
        send = self._action_page.get_by_role("button", name="Send", exact=True)
        while await send.count() == 0 and self._signal is None:
            if not await self._ready_for_action():
                return False
            await asyncio.sleep(0.05)
        if (
            await send.count() != 1
            or not await send.is_visible()
            or not await send.is_enabled()
            or await reply.input_value() != self._reply_text
            or not await self._ready_for_action()
            or self._signal is not None
        ):
            return False
        self._reply_armed = True
        self._reply_attempt_started_at = self._clock()
        self._stage = "submit"
        await send.click(timeout=self._timeout_ms)
        self._stage = "receipt"
        await self._events["reply"].wait()
        # Post-submit controls are corroboration only. A disappearing composer
        # must neither substitute for a receipt nor invalidate an accepted send.
        try:
            controls = {
                "reply_count": await reply.count(),
                "send_count": await send.count(),
            }
            if controls["reply_count"] == 1:
                controls["reply_empty"] = await reply.input_value() == ""
        except Exception:  # noqa: BLE001 - optional UI diagnostics cannot invalidate a receipt
            controls = {"unavailable": True}
        for diagnostic in self._diagnostics:
            if diagnostic["operation"] == "reply":
                diagnostic["reply_controls"] = controls
        # Clearing controls is optimistic UI. Only the correlated server
        # receipt confirms acceptance; it says nothing about recipient reading.
        return self._receipts.get("reply") is True and self._signal is None

    async def _perform(
        self,
        handle: str,
        fingerprint: str,
        *,
        like: bool,
        reply_text: str | None = None,
    ) -> ActionOutcome:
        if self._attempted:
            return ActionOutcome.FAILED
        self._attempted = True
        self._action = "reply" if reply_text is not None else "like" if like else "view"
        if (
            handle != self._handle
            or fingerprint != self._identity.fingerprint
            or self._preflight_signal is not HealthSignal.NONE
            or not self._unexpired()
        ):
            return self._failed_outcome()
        self._reply_text = reply_text
        self._events = {
            operation: asyncio.Event() for operation in ("seen", "like", "reply")
        }
        confirmed = False
        try:
            async with asyncio.timeout(self._timeout_ms / 1000):
                self._stage = "navigation"
                self._action_page = await self._page.context.new_page()
                await self._action_page.route("**/*", self._guard)
                self._action_page.on("response", self._on_response)
                await self._action_page.route_web_socket("**/*", self._block_socket)
                self._cdp = await self._page.context.new_cdp_session(self._action_page)
                await self._cdp.send("Network.setBypassServiceWorker", {"bypass": True})
                navigation = await self._action_page.goto(
                    self._target_url,
                    wait_until="domcontentloaded",
                    timeout=self._timeout_ms,
                )
                if navigation is not None:
                    signal = _restriction(navigation.status, None)
                    if signal is not None:
                        self._latch(signal)
                self._stage = "viewer"
                if not await self._wait_viewer():
                    return self._failed_outcome()
                self._stage = "seen_receipt"
                await self._events["seen"].wait()
                if self._tasks:
                    await asyncio.gather(*tuple(self._tasks))
                if not self._receipts.get("seen") or self._signal is not None:
                    return self._failed_outcome()
                if reply_text is not None:
                    confirmed = await self._reply_action()
                elif like:
                    self._stage = "like_control"
                    controls = self._action_page.locator(
                        'svg[aria-label="Like"]:visible'
                    )
                    if await controls.count() != 1:
                        return ActionOutcome.FAILED
                    button = controls.locator(
                        "xpath=ancestor::*[self::button or @role='button'][1]"
                    )
                    if await button.count() != 1 or not await button.is_visible():
                        return ActionOutcome.FAILED
                    if not await self._ready_for_action():
                        return self._failed_outcome()
                    self._like_armed = True
                    self._stage = "submit"
                    await button.click(timeout=self._timeout_ms)
                    self._stage = "receipt"
                    await self._events["like"].wait()
                    unlike = self._action_page.locator(
                        'svg[aria-label="Unlike"]:visible'
                    )
                    confirmed = (
                        self._receipts.get("like") is True and await unlike.count() == 1
                    )
                else:
                    confirmed = True
                if self._tasks:
                    await asyncio.gather(*tuple(self._tasks))
                if confirmed:
                    self._stage = "postcheck"
                viewer_matches = await self._viewer_matches()
                if not (reply_text is not None and self._receipts.get("reply") is True):
                    self._viewer_healthy = viewer_matches
                    confirmed = confirmed and viewer_matches
                confirmed = confirmed and self._signal is None
        except Exception:  # noqa: BLE001 -- Never retry a browser action with an uncertain outcome.
            self._unexpired()
            confirmed = False
        finally:
            # A validated reply acceptance is durable evidence even if a later
            # page check or cleanup fails. The guard remains closed below.
            self._confirmed_before_cleanup = (
                self._action == "reply" and self._receipts.get("reply") is True
            ) or (self._action == "like" and confirmed)
            self._closing = True
            self._reply_text = None
            cleanup = asyncio.create_task(
                self._cleanup(), name="engagemate-story-action-cleanup"
            )
            interrupted = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    interrupted = True
                except Exception:
                    break  # Inspect the completed cleanup task below.
            try:
                cleanup.result()
            except Exception:
                self._cleanup_failed = True
                self._latch(HealthSignal.HEALTH_UNAVAILABLE)
                if not self._confirmed_before_cleanup and not interrupted:
                    raise
            if interrupted:
                raise asyncio.CancelledError
        return (
            ActionOutcome.CONFIRMED
            if self._confirmed_before_cleanup or (confirmed and self._signal is None)
            else self._failed_outcome()
        )

    async def _guard(self, route: Any, request: Any) -> None:
        try:
            await self._route(route, request)
        except Exception:  # noqa: BLE001 -- A guard error may only abort, never bypass policy.
            self._latch(HealthSignal.HEALTH_UNAVAILABLE)
            try:
                await route.abort()
            except Exception:  # noqa: BLE001 -- Keep the page guarded when abort transport fails.
                self._closing = True

    async def _block_socket(self, socket: Any) -> None:
        try:
            await socket.close()
        except Exception:  # noqa: BLE001 -- A failed socket guard makes the action unsafe.
            self._latch(HealthSignal.HEALTH_UNAVAILABLE)

    async def _cleanup(self) -> None:
        # Never remove a live page's guard. Explicitly suppress beforeunload,
        # which could enqueue another native write while the page is closing.
        page_closed = False
        if self._action_page is not None:
            try:
                async with asyncio.timeout(3):
                    await self._action_page.close(run_before_unload=False)
                page_closed = True
            except Exception:  # noqa: BLE001 -- The guard and CDP bypass remain if page closing fails.
                self._cleanup_failed = True
                self._latch(HealthSignal.HEALTH_UNAVAILABLE)
            self._action_page.remove_listener("response", self._on_response)
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._cdp is not None and page_closed:
            try:
                async with asyncio.timeout(1):
                    await self._cdp.detach()
            except Exception:  # noqa: BLE001 -- The page is already closed and cannot send writes.
                return
