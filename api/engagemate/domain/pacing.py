"""Deterministic human-shaped delays between Instagram actions."""

from __future__ import annotations

import hashlib
import math
import random
from datetime import timedelta

from engagemate.domain.models import Action


class PacingError(Exception):
    """Base class for bounded pacing failures."""


class PacingInputError(PacingError):
    """Raised when a pacing action or seed is unsupported."""


class PacingPolicy:
    """Derive bounded log-normal delays without shared random state."""

    _BOUNDS = {
        # Use the existing like delay until real view throughput is measured.
        Action.VIEW: (25.0, 75.0, 40.0),
        Action.LIKE: (25.0, 75.0, 40.0),
        Action.REPLY: (60.0, 180.0, 90.0),
        Action.DM: (60.0, 180.0, 90.0),
    }
    _SIGMA = 0.36
    _MAX_SEED_BYTES = 256

    @classmethod
    def delay(cls, action: Action, seed: str | bytes) -> timedelta:
        """Return the stable delay for an action and private seed."""

        if not isinstance(action, Action):
            raise PacingInputError("invalid pacing action")
        seed_bytes = cls._seed_bytes(seed)
        digest = hashlib.sha256(
            b"engagemate:pacing:v1\0"
            + action.value.encode("ascii")
            + b"\0"
            + seed_bytes
        ).digest()
        rng = random.Random(int.from_bytes(digest, "big"))

        lower, upper, median_seconds = cls._BOUNDS[action]
        while True:
            sample = rng.lognormvariate(math.log(median_seconds), cls._SIGMA)
            if lower <= sample <= upper:
                return timedelta(seconds=sample)

    @classmethod
    def _seed_bytes(cls, seed: str | bytes) -> bytes:
        if isinstance(seed, str):
            try:
                seed_bytes = seed.encode("utf-8")
            except UnicodeEncodeError:
                raise PacingInputError("invalid pacing seed") from None
        elif isinstance(seed, bytes):
            seed_bytes = seed
        else:
            raise PacingInputError("invalid pacing seed")

        if not seed_bytes or len(seed_bytes) > cls._MAX_SEED_BYTES:
            raise PacingInputError("invalid pacing seed")
        return seed_bytes
