from __future__ import annotations

import pytest

from engagemate.worker.execution_mcp_auth import ExecutionTokenVerifier


VALID_KEY = "T9x_2AbCdEfGhJkLmNpQrStUvWxYz0123456789-"


@pytest.mark.asyncio
async def test_verifier_accepts_only_the_exact_key_without_returning_it() -> None:
    verifier = ExecutionTokenVerifier(VALID_KEY)

    accepted = await verifier.verify_token(VALID_KEY)

    assert accepted is not None
    assert accepted.token == "<verified>"
    assert accepted.client_id == "engagemate-agent-hermes"
    assert accepted.subject is None
    assert await verifier.verify_token(VALID_KEY[:-1] + "x") is None
    assert await verifier.verify_token("snowman-☃") is None


@pytest.mark.parametrize(
    "key",
    [
        "short",
        "k" * 48,
        "change-me-change-me-change-me-change-me",
        "contains spaces despite being long enough 12345",
    ],
)
def test_verifier_rejects_weak_or_non_url_safe_keys(key: str) -> None:
    with pytest.raises(ValueError, match="execution MCP key"):
        ExecutionTokenVerifier(key)


def test_verifier_representation_never_contains_the_key() -> None:
    verifier = ExecutionTokenVerifier(VALID_KEY)
    assert VALID_KEY not in repr(verifier)
    assert repr(verifier) == "ExecutionTokenVerifier(<configured>)"
