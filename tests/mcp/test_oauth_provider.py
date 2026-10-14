from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from mcp.server.auth.provider import AuthorizeError, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from engagemate.mcp.oauth_provider import (
    ACCESS_TTL,
    CODE_TTL,
    EngageMateOAuthProvider,
    pkce_challenge,
)
from engagemate.mcp.oauth_repository import token_hash
from engagemate.mcp.oauth_repository import OAuthRepository

RESOURCE_URL = "https://mcp.example.com/mcp"


USER_ID = UUID("11111111-1111-4111-8111-111111111111")
CLIENT_ID = "client-test"
REDIRECT = "https://agent.example/callback"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()
).decode().rstrip("=")
SCOPES = ["accounts:read", "stats:read"]


class MemoryOAuthRepository:
    def __init__(self) -> None:
        self.clients: dict[str, dict] = {}
        self.codes: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}
        self.requests: dict[str, dict] = {}
        self.now = datetime.now(UTC)

    async def count_clients(self) -> int:
        return len(self.clients)

    async def get_client(self, client_id: str) -> dict | None:
        return self.clients.get(client_id)

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        self.clients[client.client_id] = client.model_dump(mode="json")

    async def save_authorization_request(self, state: str, **values) -> None:
        self.requests[token_hash(state)] = dict(values)

    async def save_authorization_code(self, code: str, **values) -> None:
        self.codes[token_hash(code)] = dict(values)

    async def load_authorization_code(self, code: str, client_id: str) -> dict | None:
        row = self.codes.get(token_hash(code))
        if row is None or row["client_id"] != client_id or row.get("consumed_at"):
            return None
        return {**row, "code": code}

    async def exchange_authorization_code(
        self,
        code: str,
        client_id: str,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> dict | None:
        row = await self.load_authorization_code(code, client_id)
        if row is None or row["expires_at"] <= self.now:
            return None
        self.codes[token_hash(code)]["consumed_at"] = self.now
        common = {
            "client_id": client_id,
            "user_id": row["user_id"],
            "scopes": row["scopes"],
            "resource": row["resource"],
            "revoked_at": None,
        }
        self.tokens[token_hash(access_token)] = {
            **common,
            "kind": "access",
            "expires_at": access_expires_at,
        }
        self.tokens[token_hash(refresh_token)] = {
            **common,
            "kind": "refresh",
            "expires_at": refresh_expires_at,
        }
        return row

    async def load_token(self, token: str, kind: str) -> dict | None:
        row = self.tokens.get(token_hash(token))
        if (
            row is None
            or row["kind"] != kind
            or row["expires_at"] <= self.now
            or row.get("revoked_at") is not None
        ):
            return None
        return {**row, "token": token}

    async def rotate_refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        scopes: list[str],
        access_token: str,
        next_refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> dict | None:
        row = await self.load_token(refresh_token, "refresh")
        if row is None or row["client_id"] != client_id:
            return None
        self.tokens[token_hash(refresh_token)]["revoked_at"] = self.now
        common = {
            "client_id": client_id,
            "user_id": row["user_id"],
            "scopes": scopes,
            "resource": row["resource"],
            "revoked_at": None,
        }
        self.tokens[token_hash(access_token)] = {
            **common,
            "kind": "access",
            "expires_at": access_expires_at,
        }
        self.tokens[token_hash(next_refresh_token)] = {
            **common,
            "kind": "refresh",
            "expires_at": refresh_expires_at,
        }
        return row

    async def revoke_token(self, token: str) -> None:
        row = self.tokens.get(token_hash(token))
        if row is not None:
            row["revoked_at"] = self.now

    async def raw_token_exists(self, token: str) -> bool:
        return token in self.codes or token in self.tokens or token in self.requests


@pytest.fixture()
def repository() -> MemoryOAuthRepository:
    return MemoryOAuthRepository()


@pytest.fixture()
def oauth(repository: MemoryOAuthRepository) -> EngageMateOAuthProvider:
    settings = SimpleNamespace(
        mcp_resource_url=RESOURCE_URL,
        mcp_consent_url="https://app.example.com/mcp/authorize",
        mcp_redirect_hosts=("agent.example", "127.0.0.1", "localhost", "::1"),
    )
    return EngageMateOAuthProvider(repository, settings)


def client(client_id: str = CLIENT_ID, redirect: str = REDIRECT) -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=client_id,
        client_name="Test agent",
        redirect_uris=[redirect],
        scope=" ".join(SCOPES),
        token_endpoint_auth_method="none",
    )


@pytest.mark.asyncio
async def test_code_exchange_is_single_use_pkce_bound_and_hash_only(
    oauth: EngageMateOAuthProvider,
    repository: MemoryOAuthRepository,
) -> None:
    code = await oauth.issue_code(
        USER_ID, CLIENT_ID, REDIRECT, CHALLENGE, SCOPES, RESOURCE_URL
    )

    with pytest.raises(TokenError, match="invalid_grant"):
        await oauth.exchange_code(CLIENT_ID, code, "wrong-verifier", REDIRECT)

    tokens = await oauth.exchange_code(CLIENT_ID, code, VERIFIER, REDIRECT)
    assert tokens.access_token.startswith("em_at_")
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.startswith("em_rt_")
    assert await repository.raw_token_exists(code) is False
    assert await repository.raw_token_exists(tokens.access_token) is False

    with pytest.raises(TokenError, match="invalid_grant"):
        await oauth.exchange_code(CLIENT_ID, code, VERIFIER, REDIRECT)


@pytest.mark.asyncio
async def test_expired_code_and_wrong_resource_are_rejected(
    oauth: EngageMateOAuthProvider,
    repository: MemoryOAuthRepository,
) -> None:
    with pytest.raises(TokenError, match="invalid_target"):
        await oauth.issue_code(
            USER_ID,
            CLIENT_ID,
            REDIRECT,
            CHALLENGE,
            SCOPES,
            "https://other.example/mcp",
        )

    code = await oauth.issue_code(
        USER_ID, CLIENT_ID, REDIRECT, CHALLENGE, SCOPES, RESOURCE_URL
    )
    repository.codes[token_hash(code)]["expires_at"] = repository.now - timedelta(seconds=1)
    with pytest.raises(TokenError, match="invalid_grant"):
        await oauth.exchange_code(CLIENT_ID, code, VERIFIER, REDIRECT)


@pytest.mark.asyncio
async def test_refresh_rotates_scopes_and_revocation_invalidates_token(
    oauth: EngageMateOAuthProvider,
) -> None:
    code = await oauth.issue_code(
        USER_ID, CLIENT_ID, REDIRECT, CHALLENGE, SCOPES, RESOURCE_URL
    )
    first = await oauth.exchange_code(CLIENT_ID, code, VERIFIER, REDIRECT)
    assert first.refresh_token is not None

    registered = client()
    loaded = await oauth.load_refresh_token(registered, first.refresh_token)
    assert loaded is not None
    second = await oauth.exchange_refresh_token(
        registered, loaded, ["accounts:read"]
    )
    assert second.refresh_token not in {None, first.refresh_token}
    assert await oauth.load_refresh_token(registered, first.refresh_token) is None

    access = await oauth.load_access_token(second.access_token)
    assert access is not None
    assert access.subject == str(USER_ID)
    assert access.scopes == ["accounts:read"]
    await oauth.revoke_token(access)
    assert await oauth.load_access_token(second.access_token) is None


@pytest.mark.asyncio
async def test_registration_enforces_redirect_hosts_scopes_and_cap(
    oauth: EngageMateOAuthProvider,
    repository: MemoryOAuthRepository,
) -> None:
    await oauth.register_client(client())
    assert await oauth.get_client(CLIENT_ID) is not None

    with pytest.raises(Exception, match="redirect"):
        await oauth.register_client(client("bad-redirect", "https://evil.example/callback"))

    bad_scope = client("bad-scope")
    bad_scope.scope = "instagram:write"
    with pytest.raises(Exception, match="scope"):
        await oauth.register_client(bad_scope)

    repository.clients.update(
        {f"client-{index}": client(f"client-{index}").model_dump(mode="json") for index in range(31)}
    )
    with pytest.raises(Exception, match="limit"):
        await oauth.register_client(client("one-too-many"))


@pytest.mark.asyncio
async def test_authorize_persists_only_hashed_pending_state(
    oauth: EngageMateOAuthProvider,
    repository: MemoryOAuthRepository,
) -> None:
    from mcp.server.auth.provider import AuthorizationParams

    registered = client()
    await oauth.register_client(registered)
    redirect = await oauth.authorize(
        registered,
        AuthorizationParams(
            state="client-state",
            scopes=SCOPES,
            code_challenge=pkce_challenge(VERIFIER),
            redirect_uri=REDIRECT,
            redirect_uri_provided_explicitly=True,
            resource=RESOURCE_URL,
        ),
    )

    state = redirect.rsplit("state=", 1)[1]
    assert state.startswith("em_as_")
    assert state not in repository.requests
    assert token_hash(state) in repository.requests
    request = repository.requests[token_hash(state)]
    assert request["expires_at"] <= repository.now + CODE_TTL + timedelta(seconds=1)
    assert request["expires_at"] > repository.now


@pytest.mark.asyncio
async def test_authorize_rejects_a_redirect_not_registered_to_the_client(
    oauth: EngageMateOAuthProvider,
) -> None:
    from mcp.server.auth.provider import AuthorizationParams

    registered = client()
    await oauth.register_client(registered)

    with pytest.raises(AuthorizeError, match="redirect URI is not registered"):
        await oauth.authorize(
            registered,
            AuthorizationParams(
                state="client-state",
                scopes=SCOPES,
                code_challenge=pkce_challenge(VERIFIER),
                redirect_uri="https://agent.example/different-callback",
                redirect_uri_provided_explicitly=True,
                resource=RESOURCE_URL,
            ),
        )


def test_oauth_ttls_are_bounded() -> None:
    assert ACCESS_TTL == timedelta(hours=1)
    assert CODE_TTL == timedelta(minutes=5)


def test_repository_inserts_token_pair_through_a_psycopg_cursor() -> None:
    class Result:
        def fetchone(self):
            return {
                "client_id": CLIENT_ID,
                "user_id": USER_ID,
                "redirect_uri": REDIRECT,
                "code_challenge": CHALLENGE,
                "scopes": SCOPES,
                "resource": RESOURCE_URL,
                "expires_at": datetime.now(UTC) + CODE_TTL,
                "consumed_at": None,
            }

    class Cursor:
        def __init__(self) -> None:
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def executemany(self, _sql, rows) -> None:
            self.rows.extend(rows)

    class Context:
        def __init__(self, value) -> None:
            self.value = value

        def __enter__(self):
            return self.value

        def __exit__(self, *_args):
            return None

    class Connection:
        def __init__(self) -> None:
            self.token_cursor = Cursor()

        def transaction(self):
            return Context(None)

        def execute(self, sql, _params=()):
            return Result() if "SELECT client_id" in sql else None

        def cursor(self):
            return self.token_cursor

    connection = Connection()
    pool = SimpleNamespace(connection=lambda: Context(connection))
    repository = OAuthRepository(pool)
    now = datetime.now(UTC)

    row = repository._exchange_authorization_code(
        "em_ac_raw",
        CLIENT_ID,
        "em_at_raw",
        "em_rt_raw",
        now + ACCESS_TTL,
        now + timedelta(days=30),
    )

    assert row is not None
    assert len(connection.token_cursor.rows) == 2
    serialized = repr(connection.token_cursor.rows)
    assert "em_at_raw" not in serialized
    assert "em_rt_raw" not in serialized
