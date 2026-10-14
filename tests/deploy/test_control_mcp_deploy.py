from __future__ import annotations

import base64
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from engagemate.config import Settings
from main import create_app


ROOT = Path(__file__).resolve().parents[2]


def _settings(**overrides: object) -> Settings:
    values = {
        "DATABASE_URL": "postgresql://localhost/test",
        "SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32))
        .decode()
        .rstrip("="),
        "INTERNAL_API_KEY": "internal-test-key",
        "MCP_ISSUER": "https://mcp.example.com",
        "MCP_RESOURCE_URL": "https://mcp.example.com/mcp",
        "MCP_REDIRECT_HOSTS": "localhost,127.0.0.1,::1",
        "MCP_CONSENT_URL": "https://app.example.com/mcp/authorize",
        "ENGAGEMATE_WEB_ORIGIN": "https://app.example.com",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_control_mcp_settings_accept_the_canonical_public_urls() -> None:
    settings = _settings()
    assert str(settings.mcp_issuer) == "https://mcp.example.com/"
    assert str(settings.mcp_resource_url) == "https://mcp.example.com/mcp"
    assert settings.mcp_redirect_hosts == ("localhost", "127.0.0.1", "::1")
    assert str(settings.mcp_consent_url) == "https://app.example.com/mcp/authorize"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MCP_ISSUER", "http://mcp.example.com"),
        ("MCP_ISSUER", "https://user@mcp.example.com"),
        ("MCP_RESOURCE_URL", "https://mcp.example.com/not-mcp"),
        ("MCP_RESOURCE_URL", "https://mcp.example.com/mcp?tenant=a"),
        ("MCP_RESOURCE_URL", "https://other.example.com/mcp"),
        ("MCP_CONSENT_URL", "https://app.example.com/mcp/authorize#fragment"),
    ],
)
def test_control_mcp_settings_reject_unsafe_urls(name: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{name: value})


def test_control_mcp_deployment_declares_its_public_urls_by_name_only() -> None:
    env = (ROOT / "deploy/.env.api.example").read_text(encoding="utf-8")
    lines = env.splitlines()
    for name in ("MCP_ISSUER", "MCP_RESOURCE_URL", "MCP_CONSENT_URL", "ENGAGEMATE_WEB_ORIGIN"):
        assert f"{name}=" in lines
    assert "MCP_REDIRECT_HOSTS=" in env
    assert "engagemate.io" not in env


def _unconfigured(**overrides: object) -> Settings:
    values = {
        "DATABASE_URL": "postgresql://localhost/test",
        "SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32))
        .decode()
        .rstrip("="),
        "INTERNAL_API_KEY": "internal-test-key",
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def test_unset_urls_trust_no_public_origin(monkeypatch) -> None:
    monkeypatch.delenv("ENGAGEMATE_WEB_ORIGIN", raising=False)
    settings = _unconfigured()
    assert settings.public_app_url is None
    assert settings.mcp_issuer is None
    assert settings.mcp_resource_url is None
    assert settings.mcp_consent_url is None
    assert settings.mcp_configured is False


def test_partial_mcp_configuration_is_rejected() -> None:
    with pytest.raises(ValidationError, match="MCP_CONSENT_URL"):
        _unconfigured(
            MCP_ISSUER="https://mcp.example.com",
            MCP_RESOURCE_URL="https://mcp.example.com/mcp",
        )


@pytest.mark.parametrize(
    "value",
    [
        "http://app.example.com",
        "https://app.example.com/path",
        "https://user@app.example.com",
        "https://app.example.com/?next=x",
    ],
)
def test_web_origin_must_be_a_bare_https_origin(value: str) -> None:
    with pytest.raises(ValidationError):
        _unconfigured(ENGAGEMATE_WEB_ORIGIN=value)


def test_web_origin_accepts_https_and_loopback_http() -> None:
    assert _unconfigured(ENGAGEMATE_WEB_ORIGIN="https://App.Example.com/").public_app_url == (
        "https://app.example.com"
    )
    assert _unconfigured(ENGAGEMATE_WEB_ORIGIN="http://localhost:3030").public_app_url == (
        "http://localhost:3030"
    )


def test_unconfigured_api_starts_with_the_mcp_disabled() -> None:
    with TestClient(create_app(settings=_unconfigured(), pool=_HealthPool())) as client:
        discovery = client.get("/.well-known/oauth-authorization-server")
        transport = client.post("/mcp", json={})
        health = client.get("/v1/mcp/health")

    assert discovery.status_code == 503
    assert transport.status_code == 503
    assert health.json() == {"status": "ok", "oauth": False}


def test_mcp_without_a_web_origin_fails_at_startup() -> None:
    settings = _settings(ENGAGEMATE_WEB_ORIGIN="")
    with pytest.raises(RuntimeError, match="ENGAGEMATE_WEB_ORIGIN"):
        with TestClient(create_app(settings=settings, pool=_HealthPool())):
            pass


class _HealthPool:
    def __init__(self) -> None:
        self.checks = 0

    @contextmanager
    def connection(self):
        yield self

    def execute(self, statement: str):
        assert statement == "SELECT 1"
        self.checks += 1
        return self

    def fetchone(self) -> tuple[int]:
        return (1,)


def test_mcp_health_checks_database_while_process_health_stays_db_free() -> None:
    pool = _HealthPool()
    with TestClient(create_app(settings=_settings(), pool=pool)) as client:
        assert client.get("/health").json() == {"status": "ok", "version": "1.0.0"}
        assert pool.checks == 0
        response = client.get("/v1/mcp/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "oauth": True}
    assert pool.checks == 1


def test_fastapi_serves_oauth_discovery_and_protects_the_mcp_transport() -> None:
    with TestClient(create_app(settings=_settings(), pool=_HealthPool())) as client:
        discovery = client.get("/.well-known/oauth-authorization-server")
        protected = client.post(
            "/mcp",
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "deploy-test", "version": "1"},
                },
            },
        )

    assert discovery.status_code == 200
    assert discovery.json()["issuer"] == "https://mcp.example.com/"
    assert discovery.json()["authorization_endpoint"] == "https://mcp.example.com/authorize"
    assert discovery.json()["token_endpoint"] == "https://mcp.example.com/token"
    assert discovery.json()["registration_endpoint"] == "https://mcp.example.com/register"
    assert protected.status_code == 401
    assert "resource_metadata=" in protected.headers["www-authenticate"]
