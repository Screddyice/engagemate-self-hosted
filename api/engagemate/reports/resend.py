"""Redacted Resend transport for deterministic weekly report requests."""

from __future__ import annotations

import hashlib
import json
import re
from email.utils import parseaddr
from uuid import UUID

import httpx

from engagemate.reports.render import RenderedEmail

_ENDPOINT = "https://api.resend.com/emails"
_IDEMPOTENCY_PREFIX = "engagemate-weekly/"
_PROVIDER_ID = re.compile(r"[A-Za-z0-9._:-]{1,255}")
_LOCAL_PART = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+")
_DOMAIN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
_MAX_ADDRESS_LENGTH = 320
_MAX_RESPONSE_BYTES = 4_096


class ResendError(Exception):
    """A stable provider outcome with no provider or customer data attached."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            raise ValueError("provider error code must be bounded and stable")
        if type(retryable) is not bool:
            raise ValueError("retryable must be a boolean")
        self.code = code
        self.retryable = retryable
        super().__init__(f"Resend request failed with code={code}")

    def __repr__(self) -> str:
        return f"ResendError(code={self.code!r}, retryable={self.retryable!r})"


def _address(value: object, *, allow_display_name: bool) -> str:
    if type(value) is not str:
        raise ValueError("email address is invalid")
    configured = value.strip()
    if (
        not configured
        or len(configured) > _MAX_ADDRESS_LENGTH
        or "\r" in configured
        or "\n" in configured
    ):
        raise ValueError("email address is invalid")
    display_name, mailbox = parseaddr(configured)
    if display_name:
        mailbox_suffix = f"<{mailbox}>"
        has_valid_display_form = (
            allow_display_name
            and configured.endswith(mailbox_suffix)
            and bool(configured.removesuffix(mailbox_suffix).strip())
        )
        if not has_valid_display_form:
            raise ValueError("email address is invalid")
    elif mailbox != configured:
        raise ValueError("email address is invalid")
    if mailbox.count("@") != 1:
        raise ValueError("email address is invalid")
    local, domain = mailbox.rsplit("@", 1)
    if (
        not local
        or not domain
        or _LOCAL_PART.fullmatch(local) is None
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or _DOMAIN.fullmatch(domain) is None
        or ".." in domain
        or "," in configured
        or ";" in configured
    ):
        raise ValueError("email address is invalid")
    return configured


def validate_recipient(value: object) -> str:
    """Return one validated mailbox without exposing it in validation errors."""

    return _address(value, allow_display_name=False)


def _payload(sender: str, recipient: str, rendered: RenderedEmail) -> dict[str, object]:
    if not isinstance(rendered, RenderedEmail):
        raise ValueError("rendered must be a RenderedEmail")
    return {
        "from": _address(sender, allow_display_name=True),
        "to": [validate_recipient(recipient)],
        "subject": rendered.subject,
        "text": rendered.text,
        "html": rendered.html,
    }


def _request_body(sender: str, recipient: str, rendered: RenderedEmail) -> bytes:
    return json.dumps(
        _payload(sender, recipient, rendered),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def request_payload_hash(
    sender: str,
    recipient: str,
    rendered: RenderedEmail,
) -> str:
    """Hash the exact canonical JSON body used by the provider request."""

    return hashlib.sha256(_request_body(sender, recipient, rendered)).hexdigest()


def _idempotency_key(value: object) -> str:
    if type(value) is not str or not value.startswith((_IDEMPOTENCY_PREFIX, "engagemate-action/", "engagemate-onboarding/", "engagemate-digest/", "instagram-reconnect/", "notification-copy/")):
        raise ValueError("idempotency key is invalid")
    prefix = value.split("/", 1)[0] + "/"
    raw_id = value.removeprefix(prefix)
    try:
        delivery_id = UUID(raw_id)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("idempotency key is invalid") from None
    expected = f"{prefix}{delivery_id}"
    if value != expected:
        raise ValueError("idempotency key is invalid")
    return value


def _provider_error_name(response: httpx.Response) -> str | None:
    if len(response.content) > _MAX_RESPONSE_BYTES:
        return None
    try:
        decoded = json.loads(response.content)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    if type(decoded) is not dict:
        return None
    value = decoded.get("name")
    if type(value) is str and len(value) <= 64:
        return value
    return None


def _successful_message_id(response: httpx.Response) -> str:
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise ResendError("provider_response_invalid", retryable=False)
    try:
        decoded = json.loads(response.content)
    except (TypeError, ValueError, UnicodeDecodeError):
        raise ResendError("provider_response_invalid", retryable=False) from None
    provider_id = decoded.get("id") if type(decoded) is dict else None
    if type(provider_id) is not str or _PROVIDER_ID.fullmatch(provider_id) is None:
        raise ResendError("provider_response_invalid", retryable=False)
    return provider_id


def _http_error(response: httpx.Response) -> ResendError:
    status = response.status_code
    if status == 408:
        return ResendError("provider_timeout", retryable=True)
    if status == 429:
        return ResendError("provider_rate_limited", retryable=True)
    if 500 <= status <= 599:
        return ResendError("provider_unavailable", retryable=True)
    if status == 409:
        if _provider_error_name(response) == "concurrent_idempotent_requests":
            return ResendError("provider_idempotency_in_progress", retryable=True)
        return ResendError("idempotency_payload_mismatch", retryable=False)
    if status in {400, 422}:
        return ResendError("recipient_rejected", retryable=False)
    if status in {401, 403}:
        return ResendError("provider_auth_rejected", retryable=False)
    return ResendError("provider_request_rejected", retryable=False)


class ResendClient:
    """Send one deterministic email request without retaining response bodies."""

    def __init__(
        self,
        *,
        api_key: str,
        sender: str,
        http: httpx.Client,
        timeout_seconds: float = 10.0,
    ) -> None:
        if type(api_key) is not str or not api_key.strip():
            raise ValueError("RESEND_API_KEY is required")
        if not isinstance(http, httpx.Client):
            raise ValueError("http must be an httpx.Client")
        if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 30:
            raise ValueError("provider timeout must be between 0 and 30 seconds")
        self._api_key = api_key
        self._sender = _address(sender, allow_display_name=True)
        self._http = http
        self._timeout = httpx.Timeout(float(timeout_seconds))

    @property
    def sender(self) -> str:
        """Return the configured sender used in the locked request body."""

        return self._sender

    def __repr__(self) -> str:
        return "ResendClient(<redacted>)"

    def send(
        self,
        recipient: str,
        rendered: RenderedEmail,
        idempotency_key: str,
    ) -> str:
        """Submit one provider request and return only its bounded message ID."""

        key = _idempotency_key(idempotency_key)
        body = _request_body(self._sender, recipient, rendered)
        try:
            response = self._http.post(
                _ENDPOINT,
                content=body,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": key,
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            raise ResendError("provider_timeout", retryable=True) from None
        except httpx.RequestError:
            raise ResendError("provider_network_error", retryable=True) from None

        if not 200 <= response.status_code <= 299:
            raise _http_error(response)
        return _successful_message_id(response)
