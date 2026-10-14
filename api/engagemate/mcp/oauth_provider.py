"""OAuth 2.1 provider for the customer control MCP server."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse
from uuid import UUID

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=30)
CODE_TTL = timedelta(minutes=5)
VALID_SCOPES = frozenset(
    {
        "accounts:read",
        "stats:read",
        "config:read",
        "config:write",
        "automation:write",
    }
)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_CLIENTS = 32


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _timestamp(value: datetime) -> int:
    return int(value.timestamp())


class EngageMateOAuthProvider:
    """Database-backed MCP SDK provider with tenant-bound opaque grants."""

    def __init__(self, repository: Any, settings: Any) -> None:
        self.repository = repository
        self.resource_url = str(settings.mcp_resource_url)
        self.consent_url = str(settings.mcp_consent_url)
        configured_hosts = settings.mcp_redirect_hosts
        if isinstance(configured_hosts, str):
            configured_hosts = configured_hosts.split(",")
        self.redirect_hosts = frozenset(
            str(host).strip().lower() for host in configured_hosts if str(host).strip()
        )

    def _require_resource(self, resource: str | None) -> str:
        if resource != self.resource_url:
            raise TokenError("invalid_target", "MCP resource does not match")
        return resource

    def _validate_scopes(self, scopes: list[str] | None) -> list[str]:
        requested = list(dict.fromkeys(scopes or []))
        if not requested or not set(requested).issubset(VALID_SCOPES):
            raise TokenError("invalid_scope", "Unsupported MCP scope")
        return requested

    def _validate_redirect(self, value: str) -> None:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if host not in self.redirect_hosts:
            raise RegistrationError("invalid_redirect_uri", "redirect host is not allowed")
        if parsed.username or parsed.password or parsed.fragment:
            raise RegistrationError("invalid_redirect_uri", "redirect URI is malformed")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and host in LOOPBACK_HOSTS
        ):
            raise RegistrationError("invalid_redirect_uri", "redirect URI must use HTTPS")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        stored = await self.repository.get_client(client_id)
        return None if stored is None else OAuthClientInformationFull.model_validate(stored)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if await self.repository.count_clients() >= MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "client registration limit reached")
        if not client_info.redirect_uris:
            raise RegistrationError("invalid_redirect_uri", "redirect URI is required")
        for redirect_uri in client_info.redirect_uris:
            self._validate_redirect(str(redirect_uri))
        requested = [] if client_info.scope is None else client_info.scope.split()
        if not set(requested).issubset(VALID_SCOPES):
            raise RegistrationError("invalid_client_metadata", "scope is not allowed")
        await self.repository.save_client(client_info)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        try:
            resource = self._require_resource(params.resource)
            scopes = self._validate_scopes(params.scopes)
        except TokenError as exc:
            error = "invalid_target" if exc.error == "invalid_target" else "invalid_scope"
            raise AuthorizeError(error, exc.error_description) from exc
        redirect_uri = str(params.redirect_uri)
        self._validate_redirect(redirect_uri)
        registered_redirects = {str(value) for value in client.redirect_uris or []}
        if redirect_uri not in registered_redirects:
            raise AuthorizeError(
                "invalid_request", "redirect URI is not registered for this client"
            )
        state = "em_as_" + secrets.token_urlsafe(32)
        await self.repository.save_authorization_request(
            state,
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            code_challenge=params.code_challenge,
            scopes=scopes,
            resource=resource,
            client_state=params.state,
            expires_at=datetime.now(UTC) + CODE_TTL,
        )
        return f"{self.consent_url}?{urlencode({'state': state})}"

    async def issue_code(
        self,
        user_id: UUID,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        scopes: list[str],
        resource: str,
    ) -> str:
        resource = self._require_resource(resource)
        scopes = self._validate_scopes(scopes)
        code = "em_ac_" + secrets.token_urlsafe(32)
        await self.repository.save_authorization_code(
            code,
            client_id=client_id,
            user_id=user_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            scopes=scopes,
            resource=resource,
            expires_at=datetime.now(UTC) + CODE_TTL,
        )
        return code

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = await self.repository.load_authorization_code(
            authorization_code, client.client_id
        )
        if row is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(row["scopes"]),
            expires_at=row["expires_at"].timestamp(),
            client_id=row["client_id"],
            code_challenge=row["code_challenge"],
            redirect_uri=row["redirect_uri"],
            redirect_uri_provided_explicitly=True,
            resource=row["resource"],
            subject=str(row["user_id"]),
        )

    async def exchange_code(
        self, client_id: str, code: str, verifier: str, redirect_uri: str
    ) -> OAuthToken:
        client = OAuthClientInformationFull(client_id=client_id)
        authorization_code = await self.load_authorization_code(client, code)
        if authorization_code is None or authorization_code.expires_at <= time.time():
            raise TokenError("invalid_grant", "authorization code is invalid")
        if str(authorization_code.redirect_uri) != redirect_uri:
            raise TokenError("invalid_grant", "redirect URI does not match")
        if not hmac.compare_digest(
            authorization_code.code_challenge, pkce_challenge(verifier)
        ):
            raise TokenError("invalid_grant", "PKCE verifier does not match")
        return await self.exchange_authorization_code(client, authorization_code)

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        now = datetime.now(UTC)
        access_token = "em_at_" + secrets.token_urlsafe(32)
        refresh_token = "em_rt_" + secrets.token_urlsafe(32)
        row = await self.repository.exchange_authorization_code(
            authorization_code.code,
            client.client_id,
            access_token,
            refresh_token,
            now + ACCESS_TTL,
            now + REFRESH_TTL,
        )
        if row is None:
            raise TokenError("invalid_grant", "authorization code is invalid")
        return OAuthToken(
            access_token=access_token,
            expires_in=int(ACCESS_TTL.total_seconds()),
            scope=" ".join(row["scopes"]),
            refresh_token=refresh_token,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = await self.repository.load_token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=list(row["scopes"]),
            expires_at=_timestamp(row["expires_at"]),
            subject=str(row["user_id"]),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        requested = self._validate_scopes(scopes)
        if not set(requested).issubset(refresh_token.scopes):
            raise TokenError("invalid_scope", "scope exceeds the original grant")
        now = datetime.now(UTC)
        access_token = "em_at_" + secrets.token_urlsafe(32)
        next_refresh_token = "em_rt_" + secrets.token_urlsafe(32)
        row = await self.repository.rotate_refresh_token(
            refresh_token.token,
            client.client_id,
            requested,
            access_token,
            next_refresh_token,
            now + ACCESS_TTL,
            now + REFRESH_TTL,
        )
        if row is None:
            raise TokenError("invalid_grant", "refresh token is invalid")
        return OAuthToken(
            access_token=access_token,
            expires_in=int(ACCESS_TTL.total_seconds()),
            scope=" ".join(requested),
            refresh_token=next_refresh_token,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = await self.repository.load_token(token, "access")
        if row is None:
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=list(row["scopes"]),
            expires_at=_timestamp(row["expires_at"]),
            resource=row["resource"],
            subject=str(row["user_id"]),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        await self.repository.revoke_token(token.token)
