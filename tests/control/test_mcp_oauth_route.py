from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

from fastapi.testclient import TestClient

from engagemate.config import Settings
from main import create_app


USER_ID = UUID("11111111-1111-4111-8111-111111111111")
STATE = "s" * 48
HEADERS = {
    "X-Internal-Api-Key": "internal-test-key",
    "X-User-Id": str(USER_ID),
}


class FakeOAuthRepository:
    def __init__(self) -> None:
        self.pending = {
            "client_id": "client-test",
            "redirect_uri": "https://agent.example/callback",
            "code_challenge": "challenge",
            "scopes": ["accounts:read", "stats:read"],
            "resource": "https://mcp.example.com/mcp",
            "client_state": "client-state",
            "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        }
        self.consumed = False

    async def get_authorization_request(self, state: str):
        if state != STATE or self.consumed:
            return None
        return self.pending

    async def get_client(self, client_id: str):
        assert client_id == "client-test"
        return {"client_id": client_id, "client_name": "Test agent"}

    async def approve_authorization_request(
        self,
        state: str,
        user_id: UUID,
        approved_scopes: list[str],
        code: str,
        expires_at: datetime,
    ):
        assert state == STATE
        assert user_id == USER_ID
        assert code.startswith("em_ac_")
        assert expires_at > datetime.now(UTC)
        self.consumed = True
        return {**self.pending, "scopes": approved_scopes, "code": code}

    async def deny_authorization_request(self, state: str):
        assert state == STATE
        self.consumed = True
        return self.pending


def settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql:///unused",
        SESSION_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32))
        .decode()
        .rstrip("="),
        INTERNAL_API_KEY="internal-test-key",
        HERMES_AGENT_URL="http://127.0.0.1:8646/v1/chat/completions",
        HERMES_AGENT_API_KEY="k" * 40,
        _env_file=None,
    )


def make_client(repository: FakeOAuthRepository):
    app = create_app(settings=settings(), pool=SimpleNamespace())
    app.state.oauth_repository = repository
    return TestClient(app)


def test_approval_requires_internal_key() -> None:
    with make_client(FakeOAuthRepository()) as client:
        response = client.post(
            "/v1/mcp/oauth/approve",
            json={"state": STATE, "approved_scopes": ["accounts:read"]},
        )
    assert response.status_code == 401


def test_pending_consent_returns_only_display_fields() -> None:
    with make_client(FakeOAuthRepository()) as client:
        response = client.get(
            f"/v1/mcp/oauth/pending?state={STATE}", headers=HEADERS
        )
    assert response.status_code == 200
    assert response.json() == {
        "client_name": "Test agent",
        "requested_scopes": ["accounts:read", "stats:read"],
    }


def test_approval_accepts_narrowed_scopes_and_is_single_use() -> None:
    repository = FakeOAuthRepository()
    with make_client(repository) as client:
        response = client.post(
            "/v1/mcp/oauth/approve",
            headers=HEADERS,
            json={"state": STATE, "approved_scopes": ["accounts:read"]},
        )
        replay = client.post(
            "/v1/mcp/oauth/approve",
            headers=HEADERS,
            json={"state": STATE, "approved_scopes": ["accounts:read"]},
        )
    assert response.status_code == 200
    assert response.json()["continue_url"].startswith(
        "https://agent.example/callback?code=em_ac_"
    )
    assert "state=client-state" in response.json()["continue_url"]
    assert replay.status_code == 404


def test_approval_rejects_broader_or_malformed_scopes() -> None:
    with make_client(FakeOAuthRepository()) as client:
        broad = client.post(
            "/v1/mcp/oauth/approve",
            headers=HEADERS,
            json={"state": STATE, "approved_scopes": ["config:write"]},
        )
        malformed = client.post(
            "/v1/mcp/oauth/approve",
            headers=HEADERS,
            json={"state": "too-short", "approved_scopes": ["accounts:read"]},
        )
    assert broad.status_code == 400
    assert malformed.status_code == 422


def test_denial_consumes_state_and_returns_oauth_error_redirect() -> None:
    repository = FakeOAuthRepository()
    with make_client(repository) as client:
        response = client.post(
            "/v1/mcp/oauth/approve",
            headers=HEADERS,
            json={"state": STATE, "approved": False, "approved_scopes": []},
        )
    assert response.status_code == 200
    assert "error=access_denied" in response.json()["continue_url"]
    assert repository.consumed is True

