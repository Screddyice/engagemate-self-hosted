from __future__ import annotations

from uuid import UUID

import pytest
from mcp.server.auth.provider import AccessToken

from engagemate.mcp import principal


USER_ID = UUID("11111111-1111-4111-8111-111111111111")


def test_require_principal_returns_tenant_client_and_scopes(monkeypatch) -> None:
    token = AccessToken(
        token="opaque",
        client_id="agent-client",
        scopes=["stats:read", "accounts:read"],
        subject=str(USER_ID),
    )
    monkeypatch.setattr(principal, "get_access_token", lambda: token)

    result = principal.require_principal("stats:read")

    assert result.user_id == USER_ID
    assert result.client_id == "agent-client"
    assert result.scopes == frozenset({"stats:read", "accounts:read"})


@pytest.mark.parametrize(
    "token",
    [
        None,
        AccessToken(token="opaque", client_id="c", scopes=[], subject=str(USER_ID)),
        AccessToken(token="opaque", client_id="c", scopes=["stats:read"]),
        AccessToken(token="opaque", client_id="c", scopes=["stats:read"], subject="bad-uuid"),
    ],
)
def test_require_principal_denies_missing_or_malformed_identity(monkeypatch, token) -> None:
    monkeypatch.setattr(principal, "get_access_token", lambda: token)

    with pytest.raises(PermissionError, match="MCP scope denied"):
        principal.require_principal("stats:read")
