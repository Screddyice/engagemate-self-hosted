"""The deploy gate that refuses a stack with no LLM backend behind it.

Production ran for days with a worker that started cleanly, reported healthy,
and could not generate a single reply: the Hermes gateway was never migrated to
the new host, so nothing listened on the port every service was pointed at. The
documented `healthcheck.py worker` preflight exited 0 throughout, because every
check in it reads strings. These tests pin the probe that opens the socket.
"""

from __future__ import annotations

import pytest

from deploy.healthcheck import probe_intelligence_backend

_LOOPBACK = "http://127.0.0.1:8643/v1/chat/completions"


def _refuse(host: str, port: int, timeout: float) -> None:
    del timeout
    raise ConnectionRefusedError(f"[Errno 61] Connection refused: {host}:{port}")


def _answer(host: str, port: int, timeout: float) -> None:
    del host, port, timeout


def test_probe_fails_when_nothing_listens_on_the_configured_endpoint() -> None:
    """The exact production state: settings complete, backend absent."""

    with pytest.raises(ValueError) as raised:
        probe_intelligence_backend(
            {
                "INTELLIGENCE_PROVIDER": "hermes_agent",
                "HERMES_AGENT_URL": _LOOPBACK,
            },
            connect=_refuse,
        )

    message = str(raised.value)
    # The address is the whole point: the worker and the API are pointed at
    # different ones, and a failure that does not name which was unreachable
    # sends the operator to the wrong service.
    assert "127.0.0.1:8643" in message
    assert "ConnectionRefusedError" in message


def test_probe_passes_when_the_backend_answers() -> None:
    probe_intelligence_backend(
        {"INTELLIGENCE_PROVIDER": "hermes_agent", "HERMES_AGENT_URL": _LOOPBACK},
        connect=_answer,
    )


def test_probe_reports_the_bridge_address_the_api_container_actually_uses() -> None:
    """The API reaches Hermes through a forwarder, not through loopback.

    Its bridge gateway changes whenever the docker network is recreated, so a
    hand-copied address in deploy/.env.api goes stale silently.
    """

    with pytest.raises(ValueError, match=r"172\.21\.0\.1:8643"):
        probe_intelligence_backend(
            {
                "INTELLIGENCE_PROVIDER": "hermes_agent",
                "HERMES_AGENT_URL": "http://172.21.0.1:8643/v1/chat/completions",
            },
            connect=_refuse,
        )


def test_probe_treats_an_unset_provider_as_hermes_and_still_probes() -> None:
    """Settings defaults to hermes_agent, so an unset value is not 'no LLM'.

    deploy/.env.api carries no INTELLIGENCE_PROVIDER at all; the worker gets
    one only from deploy/.env.worker. A preflight that skipped the empty case
    would pass on the file that actually configures the API and scheduler.
    """

    with pytest.raises(ValueError, match="no LLM backend answered"):
        probe_intelligence_backend(
            {"HERMES_AGENT_URL": _LOOPBACK},
            connect=_refuse,
        )


def test_probe_requires_a_url_for_the_hermes_provider() -> None:
    with pytest.raises(ValueError, match="HERMES_AGENT_URL is required"):
        probe_intelligence_backend(
            {"INTELLIGENCE_PROVIDER": "hermes_agent"},
            connect=_answer,
        )


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1/v1/chat/completions", "not-a-url", "://8643"],
)
def test_probe_rejects_an_endpoint_with_no_host_and_port(endpoint: str) -> None:
    with pytest.raises(ValueError, match="must name a host and port"):
        probe_intelligence_backend(
            {"INTELLIGENCE_PROVIDER": "hermes_agent", "HERMES_AGENT_URL": endpoint},
            connect=_answer,
        )


def test_probe_requires_a_key_for_the_anthropic_emergency_provider() -> None:
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        probe_intelligence_backend(
            {"INTELLIGENCE_PROVIDER": "anthropic"},
            connect=_answer,
        )


def test_probe_never_opens_a_socket_for_the_anthropic_provider() -> None:
    """Anthropic is a public API. Reachability is not what breaks there."""

    def forbidden(host: str, port: int, timeout: float) -> None:
        del host, port, timeout
        raise AssertionError("the anthropic provider must not be probed")

    probe_intelligence_backend(
        {"INTELLIGENCE_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-value"},
        connect=forbidden,
    )


def test_probe_rejects_a_provider_the_application_cannot_construct() -> None:
    with pytest.raises(ValueError, match="must be hermes_agent or anthropic"):
        probe_intelligence_backend(
            {"INTELLIGENCE_PROVIDER": "ollama", "HERMES_AGENT_URL": _LOOPBACK},
            connect=_answer,
        )
