"""Construction of the OAuth-protected customer control MCP server."""

from __future__ import annotations

from typing import Any

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer

from engagemate.mcp.control_repository import ControlRepository
from engagemate.mcp.control_service import ControlService
from engagemate.mcp.control_tools import register_control_tools
from engagemate.mcp.oauth_provider import EngageMateOAuthProvider, VALID_SCOPES
from engagemate.mcp.oauth_repository import OAuthRepository


def build_control_mcp(settings: Any, pool: Any) -> MCPServer:
    required = {
        "MCP_ISSUER": getattr(settings, "mcp_issuer", None),
        "MCP_RESOURCE_URL": getattr(settings, "mcp_resource_url", None),
        "MCP_CONSENT_URL": getattr(settings, "mcp_consent_url", None),
        "ENGAGEMATE_WEB_ORIGIN": getattr(settings, "public_app_url", None),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "The Control MCP needs these settings and has no defaults: "
            + ", ".join(missing)
        )
    provider = EngageMateOAuthProvider(OAuthRepository(pool), settings)
    server = MCPServer(
        "engagemate-control",
        version="1.0.0",
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.mcp_issuer,
            resource_server_url=settings.mcp_resource_url,
            required_scopes=[],
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=sorted(VALID_SCOPES),
                default_scopes=["accounts:read", "stats:read", "config:read"],
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )
    service = ControlService(
        ControlRepository(pool),
        public_app_url=str(settings.public_app_url),
    )
    register_control_tools(server, service)
    return server
