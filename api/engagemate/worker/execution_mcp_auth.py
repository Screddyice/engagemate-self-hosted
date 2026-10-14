"""Static bearer verification for the loopback-only Hermes MCP server."""

from __future__ import annotations

import hmac
import re

from mcp.server.auth.provider import AccessToken


_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_PLACEHOLDERS = ("change-me", "example", "placeholder", "test")


class ExecutionTokenVerifier:
    """Verify one process secret without retaining a printable string."""

    def __init__(self, key: str) -> None:
        normalized = key if isinstance(key, str) else ""
        lowered = normalized.lower()
        if (
            _KEY_PATTERN.fullmatch(normalized) is None
            or len(set(normalized)) < 8
            or any(marker in lowered for marker in _PLACEHOLDERS)
        ):
            raise ValueError("execution MCP key must be a strong URL-safe secret")
        self._key = normalized.encode("ascii")

    def __repr__(self) -> str:
        return "ExecutionTokenVerifier(<configured>)"

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str):
            return None
        try:
            candidate = token.encode("utf-8")
        except UnicodeError:
            return None
        if not hmac.compare_digest(candidate, self._key):
            return None
        return AccessToken(
            token="<verified>",
            client_id="engagemate-agent-hermes",
            scopes=["instagram:execute"],
            subject=None,
        )
