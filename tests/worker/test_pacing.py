from __future__ import annotations

from datetime import timedelta
from statistics import mean, median

import pytest

from engagemate.domain.models import Action
from engagemate.domain.pacing import PacingInputError, PacingPolicy


@pytest.mark.parametrize(
    ("action", "lower", "upper"),
    [
        (Action.VIEW, 25.0, 75.0),
        (Action.LIKE, 25.0, 75.0),
        (Action.REPLY, 60.0, 180.0),
        (Action.DM, 60.0, 180.0),
    ],
)
def test_delays_are_bounded_deterministic_and_right_skewed(
    action: Action, lower: float, upper: float
) -> None:
    seconds = [
        PacingPolicy.delay(action, f"account-seed-{index}").total_seconds()
        for index in range(1_000)
    ]

    assert min(seconds) >= lower
    assert max(seconds) <= upper
    assert len(set(seconds)) > 900
    assert mean(seconds) > median(seconds)
    assert sum(value > mean(seconds) for value in seconds) < 500

    ordered_steps = {
        round(right - left, 6)
        for left, right in zip(sorted(set(seconds)), sorted(set(seconds))[1:])
    }
    assert len(ordered_steps) > 100


def test_same_action_and_seed_returns_same_delay() -> None:
    first = PacingPolicy.delay(Action.LIKE, "account-123")
    second = PacingPolicy.delay(Action.LIKE, "account-123")

    assert first == second
    assert isinstance(first, timedelta)


def test_action_is_domain_separated_in_seed_derivation() -> None:
    delays = {
        PacingPolicy.delay(action, "shared-account-seed")
        for action in (Action.LIKE, Action.REPLY, Action.DM)
    }

    assert len(delays) == 3


def test_pacing_does_not_mutate_module_global_rng_state() -> None:
    import random

    random.seed(8675309)
    expected = [random.random() for _ in range(5)]
    random.seed(8675309)

    PacingPolicy.delay(Action.REPLY, "private-rng")
    observed = [random.random() for _ in range(5)]

    assert observed == expected


@pytest.mark.parametrize("seed", ["", b"", "x" * 257, b"x" * 257, 17, True, None])
def test_delay_rejects_empty_oversized_and_unsupported_seeds(seed: object) -> None:
    with pytest.raises(PacingInputError):
        PacingPolicy.delay(Action.LIKE, seed)  # type: ignore[arg-type]


@pytest.mark.parametrize("action", ["like", None, 1, True, object()])
def test_delay_rejects_unsupported_actions(action: object) -> None:
    with pytest.raises(PacingInputError):
        PacingPolicy.delay(action, "valid-seed")  # type: ignore[arg-type]
