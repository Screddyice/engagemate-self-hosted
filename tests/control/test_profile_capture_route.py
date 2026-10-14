"""Trusted-user authorization and safe request/error contracts for profile capture."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from engagemate.browser.lease import BrowserBusy
from engagemate.control.routes.profiles import profile_service
from engagemate.onboarding.profile_capture import ProfileCaptureError
from fastapi.testclient import TestClient
from main import create_app
from pydantic import SecretStr


@pytest.fixture
def world():
    user, account = uuid4(), uuid4()
    service = SimpleNamespace(capture=AsyncMock(return_value=[{"username": "sourceone"}]))
    app = create_app()
    app.state.settings = SimpleNamespace(internal_api_key=SecretStr("test-internal-key"))
    app.state.db_pool = object()
    app.dependency_overrides[profile_service] = lambda: service
    client = TestClient(app)
    headers = {"X-Internal-API-Key": "test-internal-key", "X-User-Id": str(user)}
    body = {"ig_account_id": str(account), "usernames": ["sourceone", "sourcetwo", "sourcethree"]}
    yield SimpleNamespace(client=client, app=app, service=service, user=user, account=account,
                          headers=headers, body=body)
    client.close()


@pytest.mark.parametrize("headers", [{}, {"X-User-Id": str(uuid4())},
                                    {"X-Internal-API-Key": "wrong", "X-User-Id": str(uuid4())},
                                    {"X-Internal-API-Key": "test-internal-key"},
                                    {"X-Internal-API-Key": "test-internal-key", "X-User-Id": "invalid"}])
def test_profile_route_requires_internal_key_and_valid_explicit_user(world, headers):
    response = world.client.post("/v1/onboarding/profiles", json=world.body, headers=headers)
    assert response.status_code == 401
    world.service.capture.assert_not_awaited()


def test_profile_route_passes_verified_owner_and_account_to_service(world):
    response = world.client.post("/v1/onboarding/profiles", json=world.body, headers=world.headers)
    assert response.status_code == 200
    assert response.json() == {"profiles": [{"username": "sourceone"}]}
    world.service.capture.assert_awaited_once_with(world.user, world.account, world.body["usernames"])


@pytest.mark.parametrize("changes", [
    {"ig_account_id": "not-a-uuid"}, {"usernames": []}, {"usernames": ["one", "two"]},
    {"usernames": ["one", "two", "three", "four", "five", "six"]},
    {"usernames": ["one", "one", "three"]}, {"usernames": ["one", "two", 3]},
    {"usernames": ["one", "two", "../stories/"]}, {"usernames": ["one", "two", "UPPERCASE"]},
    {"usernames": ["one", "two", "x" * 31]}, {"usernames": ["one", "two", "three\n"]},
    {"user_id": str(uuid4())}, {"session_cookie": "never-echo-this-cookie"},
])
def test_profile_route_rejects_malformed_or_extra_input_without_echo(world, changes):
    response = world.client.post("/v1/onboarding/profiles", json={**world.body, **changes}, headers=world.headers)
    assert response.status_code == 422
    assert "never-echo-this-cookie" not in response.text
    detail = response.json()["detail"]
    if isinstance(detail, list):
        assert all("input" not in item for item in detail)
    world.service.capture.assert_not_awaited()


@pytest.mark.parametrize("reason,retry,status", [("source_accounts_changed", None, 422),
                                                ("reconnect_required", None, 422),
                                                ("proxy_binding_required", None, 422),
                                                ("rate_limited", 1800, 429),
                                                ("profile_check_cooldown", 120, 429),
                                                ("profile_check_timeout", 120, 429)])
def test_profile_route_returns_bounded_known_failure(world, reason, retry, status):
    world.service.capture.side_effect = ProfileCaptureError(reason, retry)
    response = world.client.post("/v1/onboarding/profiles", json=world.body, headers=world.headers)
    assert response.status_code == status
    assert response.json()["detail"]["code"] == reason
    assert len(response.json()["detail"]["message"]) <= 256
    assert response.headers.get("Retry-After") == (str(retry) if retry else None)


def test_profile_route_busy_response_has_short_retry_without_raw_diagnostics(world):
    world.service.capture.side_effect = BrowserBusy("private proxy diagnostic")
    response = world.client.post("/v1/onboarding/profiles", json=world.body, headers=world.headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "account_browser_busy"
    assert response.headers["Retry-After"] == "30"
    assert "private proxy diagnostic" not in response.text


def test_profile_route_never_returns_unknown_error_diagnostics(world):
    world.service.capture.side_effect = ProfileCaptureError("proxy password=never-echo-test-secret " + "x" * 1000, 999999)
    response = world.client.post("/v1/onboarding/profiles", json=world.body, headers=world.headers)
    assert response.status_code in (422, 429)
    assert response.json()["detail"]["code"] == "profile_unreadable"
    assert "never-echo-test-secret" not in response.text
    assert len(response.text) < 512
    if "Retry-After" in response.headers:
        assert 0 < int(response.headers["Retry-After"]) <= 1800
