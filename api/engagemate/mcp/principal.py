"""Resolve a tenant-bound MCP principal from the SDK authorization context."""

from dataclasses import dataclass
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token


@dataclass(frozen=True)
class McpPrincipal:
    user_id: UUID
    client_id: str
    scopes: frozenset[str]


def require_principal(required_scope: str) -> McpPrincipal:
    """Return the authenticated principal or deny the tool invocation."""

    token = get_access_token()
    if token is None or token.subject is None or required_scope not in token.scopes:
        raise PermissionError("MCP scope denied")
    try:
        user_id = UUID(token.subject)
    except (TypeError, ValueError) as exc:
        raise PermissionError("MCP scope denied") from exc
    return McpPrincipal(
        user_id=user_id,
        client_id=token.client_id,
        scopes=frozenset(token.scopes),
    )

