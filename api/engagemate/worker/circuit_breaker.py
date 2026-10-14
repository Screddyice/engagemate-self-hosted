"""Strict, clock-stable account circuit-breaker decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class HealthSignal(StrEnum):
    NONE = "none"
    HEALTH_UNAVAILABLE = "health_unavailable"
    CHALLENGE = "challenge"
    CHECKPOINT = "checkpoint"
    LOGIN_REQUIRED = "login_required"
    SUSPICIOUS_ACTIVITY = "suspicious_activity"
    RATE_LIMIT = "rate_limit"
    NETWORK_ERROR = "network_error"
    PROXY_ERROR = "proxy_error"


#: Signals that stop a run because Instagram restricted the account. Network
#: and proxy faults are ours, not Instagram's, so they are absent on purpose:
#: an account must not lose its warm-up progress to our own infrastructure.
#: tests/worker/test_circuit_breaker.py pins this to what observe() stops on.
RESTRICTION_SIGNALS = frozenset(
    {
        HealthSignal.HEALTH_UNAVAILABLE,
        HealthSignal.CHALLENGE,
        HealthSignal.CHECKPOINT,
        HealthSignal.LOGIN_REQUIRED,
        HealthSignal.SUSPICIOUS_ACTIVITY,
        HealthSignal.RATE_LIMIT,
    }
)


class AccountHealthStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CHALLENGED = "challenged"


@dataclass(frozen=True, slots=True)
class BreakerDecision:
    stop: bool
    health_status: AccountHealthStatus
    reason_code: str
    pause_until: datetime | None
    observed_at: datetime


class CircuitBreaker:
    """Map one bounded signal against an exact base UTC clock."""

    def __init__(self, base_utc: datetime) -> None:
        if type(base_utc) is not datetime or base_utc.tzinfo is not timezone.utc:
            raise ValueError("circuit breaker requires an exact UTC clock")
        self._base_utc = base_utc

    def observe(self, signal: HealthSignal) -> BreakerDecision:
        if type(signal) is not HealthSignal:
            raise ValueError("invalid health signal")
        if signal in {HealthSignal.NETWORK_ERROR, HealthSignal.PROXY_ERROR, HealthSignal.NONE}:
            return BreakerDecision(
                False,
                AccountHealthStatus.ACTIVE,
                signal.value,
                None,
                self._base_utc,
            )
        if signal is HealthSignal.RATE_LIMIT:
            return BreakerDecision(
                True,
                AccountHealthStatus.PAUSED,
                signal.value,
                self._base_utc + timedelta(hours=24),
                self._base_utc,
            )
        if signal in {HealthSignal.CHALLENGE, HealthSignal.CHECKPOINT}:
            return BreakerDecision(
                True,
                AccountHealthStatus.CHALLENGED,
                signal.value,
                None,
                self._base_utc,
            )
        return BreakerDecision(
            True,
            AccountHealthStatus.PAUSED,
            signal.value,
            None,
            self._base_utc,
        )
