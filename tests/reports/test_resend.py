"""Redacted and idempotent Resend transport contracts."""

from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest

from engagemate.reports.render import RenderedEmail
from engagemate.reports.resend import (
    ResendClient,
    ResendError,
    request_payload_hash,
)


def _rendered(
    subject: str = "Weekly report",
    text: str = "Plain report",
    html: str = "<p>HTML report</p>",
) -> RenderedEmail:
    return RenderedEmail(subject=subject, text=text, html=html)


def _client(
    handler,
    *,
    api_key: str = "re_secret_value",
    sender: str = "EngageMate <reports@example.test>",
) -> ResendClient:
    return ResendClient(
        api_key=api_key,
        sender=sender,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=8.0,
    )


def test_send_posts_complete_payload_with_stable_key_and_bounded_timeout() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "email_123", "ignored": "provider-body"})

    delivery_id = uuid.uuid4()
    key = f"engagemate-weekly/{delivery_id}"
    client = _client(handler)

    provider_id = client.send("manager@example.test", _rendered(), key)

    assert provider_id == "email_123"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.resend.com/emails"
    assert request.headers["Idempotency-Key"] == key
    assert request.headers["Authorization"] == "Bearer re_secret_value"
    assert json.loads(request.content) == {
        "from": "EngageMate <reports@example.test>",
        "to": ["manager@example.test"],
        "subject": "Weekly report",
        "text": "Plain report",
        "html": "<p>HTML report</p>",
    }
    timeout = request.extensions["timeout"]
    assert all(0 < seconds <= 8.0 for seconds in timeout.values())


def test_request_hash_covers_every_complete_payload_field_and_is_lowercase_sha256() -> None:
    baseline = request_payload_hash(
        "reports@example.test",
        "manager@example.test",
        _rendered(),
    )

    variants = (
        request_payload_hash(
            "other@example.test", "manager@example.test", _rendered()
        ),
        request_payload_hash(
            "reports@example.test", "other@example.test", _rendered()
        ),
        request_payload_hash(
            "reports@example.test",
            "manager@example.test",
            _rendered(subject="Changed"),
        ),
        request_payload_hash(
            "reports@example.test",
            "manager@example.test",
            _rendered(text="Changed"),
        ),
        request_payload_hash(
            "reports@example.test",
            "manager@example.test",
            _rendered(html="<p>Changed</p>"),
        ),
    )

    assert len(baseline) == 64
    assert baseline == baseline.lower()
    assert all(character in "0123456789abcdef" for character in baseline)
    assert len(set(variants)) == len(variants)
    assert baseline not in variants
    assert baseline == request_payload_hash(
        "reports@example.test",
        "manager@example.test",
        _rendered(),
    )


@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
def test_retryable_http_statuses_raise_stable_redacted_error(status_code: int) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            json={"message": "recipient@example.test raw-provider-body"},
        )

    with pytest.raises(ResendError) as caught:
        _client(handler).send(
            "recipient@example.test",
            _rendered(),
            f"engagemate-weekly/{uuid.uuid4()}",
        )

    assert caught.value.retryable is True
    assert caught.value.code in {
        "provider_timeout",
        "provider_rate_limited",
        "provider_unavailable",
    }
    assert "recipient@example.test" not in str(caught.value)
    assert "raw-provider-body" not in repr(caught.value)


@pytest.mark.parametrize("exception", [httpx.TimeoutException, httpx.NetworkError])
def test_network_failures_are_retryable_without_leaking_exception_text(exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception("re_secret_value raw-provider-body", request=request)

    with pytest.raises(ResendError) as caught:
        _client(handler).send(
            "recipient@example.test",
            _rendered(),
            f"engagemate-weekly/{uuid.uuid4()}",
        )

    assert caught.value.retryable is True
    assert caught.value.code in {"provider_timeout", "provider_network_error"}
    serialized = f"{caught.value!s} {caught.value!r}"
    assert "re_secret_value" not in serialized
    assert "raw-provider-body" not in serialized


@pytest.mark.parametrize(
    ("provider_name", "retryable", "code"),
    [
        ("concurrent_idempotent_requests", True, "provider_idempotency_in_progress"),
        ("invalid_idempotent_request", False, "idempotency_payload_mismatch"),
    ],
)
def test_idempotency_conflicts_keep_the_key_and_are_classified(
    provider_name: str,
    retryable: bool,
    code: str,
) -> None:
    seen_keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers["Idempotency-Key"])
        return httpx.Response(
            409,
            json={"name": provider_name, "message": "raw-provider-body"},
        )

    key = f"engagemate-weekly/{uuid.uuid4()}"
    with pytest.raises(ResendError) as caught:
        _client(handler).send("recipient@example.test", _rendered(), key)

    assert seen_keys == [key]
    assert caught.value.retryable is retryable
    assert caught.value.code == code
    assert "raw-provider-body" not in str(caught.value)


def test_rejected_recipient_is_permanent_and_redacted(caplog) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "name": "validation_error",
                "message": "recipient@example.test raw-provider-body",
            },
        )

    with caplog.at_level(logging.DEBUG), pytest.raises(ResendError) as caught:
        _client(handler).send(
            "recipient@example.test",
            _rendered(),
            f"engagemate-weekly/{uuid.uuid4()}",
        )

    assert caught.value.retryable is False
    assert caught.value.code == "recipient_rejected"
    serialized = f"{caught.value!s} {caught.value!r} {caplog.text}"
    assert "recipient@example.test" not in serialized
    assert "raw-provider-body" not in serialized


@pytest.mark.parametrize("provider_id", ["", "x" * 256, "id with spaces"])
def test_success_requires_a_bounded_provider_message_id(provider_id: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": provider_id, "private": "raw-provider-body"},
        )

    with pytest.raises(ResendError) as caught:
        _client(handler).send(
            "recipient@example.test",
            _rendered(),
            f"engagemate-weekly/{uuid.uuid4()}",
        )

    assert caught.value.retryable is False
    assert caught.value.code == "provider_response_invalid"
    assert "raw-provider-body" not in str(caught.value)


def test_client_and_error_repr_redact_credentials_and_provider_data() -> None:
    client = _client(lambda _request: httpx.Response(500))
    error = ResendError("provider_unavailable", retryable=True)

    assert repr(client) == "ResendClient(<redacted>)"
    assert repr(error) == "ResendError(code='provider_unavailable', retryable=True)"
    assert "re_secret_value" not in repr(client)


@pytest.mark.parametrize(
    "sender",
    [
        "reports @example.test",
        "reports..weekly@example.test",
        "reports@example..test",
    ],
)
def test_client_rejects_malformed_sender_before_any_request(sender: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "email_123"})

    with pytest.raises(ValueError, match="email address is invalid"):
        _client(handler, sender=sender)

    assert requests == []


@pytest.mark.parametrize('prefix', ['instagram-reconnect/', 'notification-copy/'])
def test_new_notification_keys_require_canonical_uuid(prefix):
    from engagemate.reports.resend import _idempotency_key
    identity = '12345678-abcd-4234-9234-123456789abc'
    assert _idempotency_key(prefix + identity) == prefix + identity
    for invalid in [identity.upper(), identity.replace('-', ''), '{' + identity + '}', identity + '/', '']:
        with pytest.raises(ValueError):
            _idempotency_key(prefix + invalid)
