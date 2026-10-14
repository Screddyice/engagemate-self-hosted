"""The Hermes endpoint must stay non-public, but reachable from a container.

The gateway binds the host loopback, which the API container cannot reach from
its bridge network, so the setting has to accept a private address. It must NOT
accept a public one: prompts and brand data would otherwise be sent off-box to
an arbitrary LLM endpoint.
"""

import base64
import os

import pytest
from pydantic import ValidationError

from engagemate.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(
        DATABASE_URL="postgresql://localhost/test",
        SESSION_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
        INTERNAL_API_KEY="internal-test-key",
        **overrides,
    )


def test_defaults_to_host_loopback() -> None:
    assert _settings().hermes_agent_url == "http://127.0.0.1:8657/v1/chat/completions"


@pytest.mark.parametrize(
    "url",
    [
        "http://172.18.0.1:8643/v1/chat/completions",  # docker bridge gateway
        "http://10.0.0.5:8643/v1/chat/completions",
        "http://192.168.1.10:8643/v1/chat/completions",
        "http://127.0.0.1:8643/v1/chat/completions",
    ],
)
def test_accepts_loopback_and_private_addresses(url: str) -> None:
    assert _settings(HERMES_AGENT_URL=url).hermes_agent_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:8643/v1/chat/completions",
        "https://api.openai.com/v1/chat/completions",
        "http://93.184.216.34/v1/chat/completions",
    ],
)
def test_rejects_publicly_routable_endpoints(url: str) -> None:
    with pytest.raises(ValidationError):
        _settings(HERMES_AGENT_URL=url)


def test_rejects_hostname_that_could_resolve_anywhere() -> None:
    # A hostname is not checkable here, so it is refused rather than trusted.
    with pytest.raises(ValidationError):
        _settings(HERMES_AGENT_URL="http://evil.example.com:8643/v1/chat/completions")


def test_rejects_non_http_scheme() -> None:
    with pytest.raises(ValidationError):
        _settings(HERMES_AGENT_URL="ftp://127.0.0.1:8643/v1/chat/completions")


@pytest.mark.parametrize("value", [60, 1800, 3600])
def test_execution_timeout_is_independent_of_brand_analysis(value):
    settings = _settings(HERMES_EXECUTION_TIMEOUT_SECONDS=value, HERMES_AGENT_TIMEOUT_SECONDS=2)
    assert settings.hermes_execution_timeout_seconds == value
    assert settings.hermes_agent_timeout_seconds == 2


def test_execution_timeout_defaults_to_thirty_minutes(monkeypatch):
    monkeypatch.delenv("HERMES_EXECUTION_TIMEOUT_SECONDS", raising=False)
    assert _settings().hermes_execution_timeout_seconds == 1800
    assert _settings().hermes_agent_timeout_seconds == 60


@pytest.mark.parametrize("value", [0, 59, 3601, "nan", "inf", "invalid"])
def test_execution_timeout_rejects_out_of_range_values(value):
    with pytest.raises(ValidationError):
        _settings(HERMES_EXECUTION_TIMEOUT_SECONDS=value)
