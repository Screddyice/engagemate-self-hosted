from datetime import datetime, timedelta, timezone

import pytest
from engagemate.worker.circuit_breaker import (
    AccountHealthStatus,
    CircuitBreaker,
    HealthSignal,
)

BASE = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("signal", "stop", "status", "reason", "pause"),
    [
        (HealthSignal.NONE, False, AccountHealthStatus.ACTIVE, "none", None),
        (HealthSignal.NETWORK_ERROR, False, AccountHealthStatus.ACTIVE, "network_error", None),
        (HealthSignal.PROXY_ERROR, False, AccountHealthStatus.ACTIVE, "proxy_error", None),
        (HealthSignal.RATE_LIMIT, True, AccountHealthStatus.PAUSED, "rate_limit", BASE + timedelta(hours=24)),
        (HealthSignal.CHALLENGE, True, AccountHealthStatus.CHALLENGED, "challenge", None),
        (HealthSignal.CHECKPOINT, True, AccountHealthStatus.CHALLENGED, "checkpoint", None),
        (HealthSignal.LOGIN_REQUIRED, True, AccountHealthStatus.PAUSED, "login_required", None),
        (HealthSignal.SUSPICIOUS_ACTIVITY, True, AccountHealthStatus.PAUSED, "suspicious_activity", None),
    ],
)
def test_observe_maps_each_bounded_signal(signal, stop, status, reason, pause):
    decision = CircuitBreaker(BASE).observe(signal)
    assert (decision.stop, decision.health_status, decision.reason_code, decision.pause_until) == (
        stop,
        status,
        reason,
        pause,
    )
    assert decision.observed_at == BASE


@pytest.mark.parametrize(
    "clock",
    [
        datetime(2026, 7, 31, 8, 0),  # noqa: DTZ001 - intentional naive input
        "2026-07-31T08:00:00Z",
        datetime(2026, 7, 31, 8, 0, tzinfo=timezone(timedelta(hours=1))),
    ],
)
def test_breaker_rejects_non_exact_utc_clock(clock):
    with pytest.raises(ValueError, match="UTC clock"):
        CircuitBreaker(clock)


def test_breaker_rejects_untyped_signal():
    with pytest.raises(ValueError, match="health signal"):
        CircuitBreaker(BASE).observe("network_error")


def test_restriction_signals_are_exactly_what_the_breaker_stops_on() -> None:
    """Every stopping health signal belongs to the safety breaker set."""

    from engagemate.worker.circuit_breaker import RESTRICTION_SIGNALS

    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stopping = {
        signal
        for signal in HealthSignal
        if CircuitBreaker(clock).observe(signal).stop
    }

    assert RESTRICTION_SIGNALS == stopping
