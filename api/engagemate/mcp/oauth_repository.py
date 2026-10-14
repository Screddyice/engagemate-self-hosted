"""PostgreSQL persistence for OAuth clients, grants, and opaque tokens."""

from __future__ import annotations

import hashlib
from datetime import datetime
from functools import partial
from typing import Any, Callable, TypeVar

from anyio import to_thread
from mcp.shared.auth import OAuthClientInformationFull
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


T = TypeVar("T")


def token_hash(value: str) -> str:
    """Return the one-way representation stored for every OAuth secret."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OAuthRepository:
    """Keep OAuth state durable without persisting bearer credentials verbatim."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    async def _run(self, operation: Callable[..., T], *args: Any) -> T:
        return await to_thread.run_sync(partial(operation, *args))

    async def count_clients(self) -> int:
        return await self._run(self._count_clients)

    def _count_clients(self) -> int:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT count(*) AS count FROM mcp_oauth_clients"
            ).fetchone()
        return int(row["count"])

    async def get_client(self, client_id: str) -> dict[str, Any] | None:
        return await self._run(self._get_client, client_id)

    def _get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                "SELECT client_metadata FROM mcp_oauth_clients WHERE client_id = %s",
                (client_id,),
            ).fetchone()
        return None if row is None else row["client_metadata"]

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        await self._run(self._save_client, client)

    def _save_client(self, client: OAuthClientInformationFull) -> None:
        metadata = client.model_dump(mode="json")
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO mcp_oauth_clients (client_id, client_metadata)
                VALUES (%s, %s)
                ON CONFLICT (client_id) DO UPDATE
                SET client_metadata = EXCLUDED.client_metadata, updated_at = now()
                """,
                (client.client_id, Jsonb(metadata)),
            )

    async def save_authorization_request(self, state: str, **values: Any) -> None:
        await self._run(self._save_authorization_request, state, values)

    def _save_authorization_request(self, state: str, values: dict[str, Any]) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO mcp_authorization_requests (
                    state_hash, client_id, redirect_uri, code_challenge,
                    scopes, resource, client_state, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    token_hash(state),
                    values["client_id"],
                    values["redirect_uri"],
                    values["code_challenge"],
                    values["scopes"],
                    values["resource"],
                    values.get("client_state"),
                    values["expires_at"],
                ),
            )

    async def get_authorization_request(self, state: str) -> dict[str, Any] | None:
        return await self._run(self._get_authorization_request, state)

    def _get_authorization_request(self, state: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT client_id, redirect_uri, code_challenge, scopes, resource,
                       client_state, expires_at, consumed_at
                FROM mcp_authorization_requests
                WHERE state_hash = %s AND consumed_at IS NULL AND expires_at > now()
                """,
                (token_hash(state),),
            ).fetchone()
        return row

    async def approve_authorization_request(
        self,
        state: str,
        user_id: Any,
        approved_scopes: list[str],
        code: str,
        expires_at: datetime,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._approve_authorization_request,
            state,
            user_id,
            approved_scopes,
            code,
            expires_at,
        )

    def _approve_authorization_request(
        self,
        state: str,
        user_id: Any,
        approved_scopes: list[str],
        code: str,
        expires_at: datetime,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT client_id, redirect_uri, code_challenge, scopes, resource,
                       client_state, expires_at, consumed_at
                FROM mcp_authorization_requests
                WHERE state_hash = %s
                FOR UPDATE
                """,
                (token_hash(state),),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or row["expires_at"] <= datetime.now(row["expires_at"].tzinfo)
            ):
                return None
            if not set(approved_scopes).issubset(row["scopes"]):
                raise ValueError("approved scopes exceed requested scopes")
            connection.execute(
                """
                UPDATE mcp_authorization_requests
                SET consumed_at = now()
                WHERE state_hash = %s
                """,
                (token_hash(state),),
            )
            connection.execute(
                """
                INSERT INTO mcp_authorization_codes (
                    code_hash, client_id, user_id, redirect_uri,
                    code_challenge, scopes, resource, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    token_hash(code),
                    row["client_id"],
                    user_id,
                    row["redirect_uri"],
                    row["code_challenge"],
                    approved_scopes,
                    row["resource"],
                    expires_at,
                ),
            )
            return {**row, "scopes": approved_scopes, "code": code}

    async def deny_authorization_request(self, state: str) -> dict[str, Any] | None:
        return await self._run(self._deny_authorization_request, state)

    def _deny_authorization_request(self, state: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT client_id, redirect_uri, code_challenge, scopes, resource,
                       client_state, expires_at, consumed_at
                FROM mcp_authorization_requests
                WHERE state_hash = %s
                FOR UPDATE
                """,
                (token_hash(state),),
            ).fetchone()
            if (
                row is None
                or row["consumed_at"] is not None
                or row["expires_at"] <= datetime.now(row["expires_at"].tzinfo)
            ):
                return None
            connection.execute(
                "UPDATE mcp_authorization_requests SET consumed_at = now() WHERE state_hash = %s",
                (token_hash(state),),
            )
            return row

    async def save_authorization_code(self, code: str, **values: Any) -> None:
        await self._run(self._save_authorization_code, code, values)

    def _save_authorization_code(self, code: str, values: dict[str, Any]) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO mcp_authorization_codes (
                    code_hash, client_id, user_id, redirect_uri,
                    code_challenge, scopes, resource, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    token_hash(code),
                    values["client_id"],
                    values["user_id"],
                    values["redirect_uri"],
                    values["code_challenge"],
                    values["scopes"],
                    values["resource"],
                    values["expires_at"],
                ),
            )

    async def load_authorization_code(
        self, code: str, client_id: str
    ) -> dict[str, Any] | None:
        return await self._run(self._load_authorization_code, code, client_id)

    def _load_authorization_code(
        self, code: str, client_id: str
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT client_id, user_id, redirect_uri, code_challenge,
                       scopes, resource, expires_at, consumed_at
                FROM mcp_authorization_codes
                WHERE code_hash = %s AND client_id = %s AND consumed_at IS NULL
                """,
                (token_hash(code), client_id),
            ).fetchone()
        return None if row is None else {**row, "code": code}

    async def exchange_authorization_code(
        self,
        code: str,
        client_id: str,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._exchange_authorization_code,
            code,
            client_id,
            access_token,
            refresh_token,
            access_expires_at,
            refresh_expires_at,
        )

    def _exchange_authorization_code(
        self,
        code: str,
        client_id: str,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT client_id, user_id, redirect_uri, code_challenge,
                       scopes, resource, expires_at, consumed_at
                FROM mcp_authorization_codes
                WHERE code_hash = %s AND client_id = %s
                FOR UPDATE
                """,
                (token_hash(code), client_id),
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                return None
            if row["expires_at"] <= datetime.now(row["expires_at"].tzinfo):
                return None
            connection.execute(
                "UPDATE mcp_authorization_codes SET consumed_at = now() WHERE code_hash = %s",
                (token_hash(code),),
            )
            common = (
                client_id,
                row["user_id"],
                row["scopes"],
                row["resource"],
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO mcp_tokens (
                        token_hash, kind, client_id, user_id, scopes, resource, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (token_hash(access_token), "access", *common, access_expires_at),
                        (token_hash(refresh_token), "refresh", *common, refresh_expires_at),
                    ],
                )
            return row

    async def load_token(self, token: str, kind: str) -> dict[str, Any] | None:
        return await self._run(self._load_token, token, kind)

    def _load_token(self, token: str, kind: str) -> dict[str, Any] | None:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT kind, client_id, user_id, scopes, resource,
                       expires_at, revoked_at
                FROM mcp_tokens
                WHERE token_hash = %s AND kind = %s
                  AND revoked_at IS NULL AND expires_at > now()
                """,
                (token_hash(token), kind),
            ).fetchone()
        return None if row is None else {**row, "token": token}

    async def rotate_refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        scopes: list[str],
        access_token: str,
        next_refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> dict[str, Any] | None:
        return await self._run(
            self._rotate_refresh_token,
            refresh_token,
            client_id,
            scopes,
            access_token,
            next_refresh_token,
            access_expires_at,
            refresh_expires_at,
        )

    def _rotate_refresh_token(
        self,
        refresh_token: str,
        client_id: str,
        scopes: list[str],
        access_token: str,
        next_refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> dict[str, Any] | None:
        with self._pool.connection() as connection, connection.transaction():
            row = connection.execute(
                """
                SELECT client_id, user_id, scopes, resource, expires_at, revoked_at
                FROM mcp_tokens
                WHERE token_hash = %s AND kind = 'refresh' AND client_id = %s
                FOR UPDATE
                """,
                (token_hash(refresh_token), client_id),
            ).fetchone()
            if (
                row is None
                or row["revoked_at"] is not None
                or row["expires_at"] <= datetime.now(row["expires_at"].tzinfo)
            ):
                return None
            connection.execute(
                "UPDATE mcp_tokens SET revoked_at = now() WHERE token_hash = %s",
                (token_hash(refresh_token),),
            )
            common = (client_id, row["user_id"], scopes, row["resource"])
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO mcp_tokens (
                        token_hash, kind, client_id, user_id, scopes, resource, expires_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (token_hash(access_token), "access", *common, access_expires_at),
                        (token_hash(next_refresh_token), "refresh", *common, refresh_expires_at),
                    ],
                )
            return row

    async def revoke_token(self, token: str) -> None:
        await self._run(self._revoke_token, token)

    def _revoke_token(self, token: str) -> None:
        with self._pool.connection() as connection:
            connection.execute(
                "UPDATE mcp_tokens SET revoked_at = now() WHERE token_hash = %s",
                (token_hash(token),),
            )

    async def raw_token_exists(self, token: str) -> bool:
        return await self._run(self._raw_token_exists, token)

    def _raw_token_exists(self, token: str) -> bool:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT (
                    EXISTS (SELECT 1 FROM mcp_tokens WHERE token_hash = %s)
                    OR EXISTS (SELECT 1 FROM mcp_authorization_codes WHERE code_hash = %s)
                    OR EXISTS (SELECT 1 FROM mcp_authorization_requests WHERE state_hash = %s)
                ) AS found
                """,
                (token, token, token),
            ).fetchone()
        return bool(row["found"])
