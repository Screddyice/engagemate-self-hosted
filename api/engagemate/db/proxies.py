"""Transactional, tenant-scoped dedicated proxy bindings."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from engagemate.db.text import text as _text
from engagemate.proxy.base import (
    Geo,
    ProxyEndpoint,
    ProxyHealth,
    ProxyHealthStatus,
    ProxyProvider,
)
from engagemate.security.session_cipher import SessionCipher, SessionCipherError
from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg_pool import ConnectionPool


class ProxyBindingError(ValueError):
    """A secret-safe binding error suitable for an operator response."""

    def __init__(
        self,
        message: str,
        *,
        transient: bool = False,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.transient = transient
        self.reason = reason


_TRANSIENT_HEALTH_REASONS = frozenset(
    {
        "proxy_access_denied",
        "connection_failed",
        "provider_error",
        "unexpected_status",
    }
)


class AccountUnavailableError(ProxyBindingError):
    """The requested account is not owned by the caller."""

_ACTIVE_BINDING_SQL = """
SELECT proxy.id,
       proxy.provider,
       proxy.provider_reference,
       proxy.proxy_type,
       proxy.country,
       proxy.region,
       proxy.city,
       proxy.host,
       proxy.port,
       proxy.encrypted_username,
       proxy.encrypted_password
FROM proxy_bindings binding
JOIN proxies proxy ON proxy.id = binding.proxy_id
WHERE binding.user_id = %(user_id)s
  AND binding.ig_account_id = %(account_id)s
  AND binding.released_at IS NULL
"""


class ProxyRepository:
    """Persist one active, account-bound proxy under PostgreSQL locks."""

    def __init__(self, pool: ConnectionPool, session_encryption_key: str) -> None:
        self.pool = pool
        self._session_encryption_key = session_encryption_key

    def __repr__(self) -> str:
        return "ProxyRepository(pool=<configured>, encryption_key=<redacted>)"

    def free_leases_by_geo(self) -> dict[Geo, int]:
        """Count assignable ISP leases per geography.

        Free means active, still credential-scoped to inventory, and not held
        by a live binding -- the same three conditions `_claim_managed_inventory`
        selects on, so this cannot report a lease the binder could not actually
        hand out. Feeds login-geography selection: a geography with zero here is
        one an account must not be told to call home.
        """

        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT proxy.country, proxy.region, proxy.city, count(*)
                FROM proxies proxy
                WHERE proxy.provider = 'iproyal'
                  AND proxy.proxy_type = 'isp_static_residential'
                  AND proxy.health_status = 'active'
                  AND proxy.credential_scope = 'inventory'
                  AND NOT (proxy.id = ANY(%(reserved)s::uuid[]))
                  AND NOT EXISTS (
                      SELECT 1 FROM proxy_bindings binding
                      WHERE binding.proxy_id = proxy.id
                        AND binding.released_at IS NULL
                  )
                GROUP BY proxy.country, proxy.region, proxy.city
                """,
                {"reserved": self._reserved_proxy_ids(connection)},
            ).fetchall()
        return {
            Geo(
                _text(row["country"]), _text(row["region"]), _text(row["city"])
            ): int(row["count"])
            for row in rows
        }

    @staticmethod
    def _reserved_proxy_ids(connection: Connection) -> list[UUID]:
        # Older deployments can keep the extension path while this optional
        # service's migration is staged. Hosted login itself requires the table.
        exists = connection.execute(
            "SELECT to_regclass('hosted_login_reservations') AS relation"
        ).fetchone()
        if exists["relation"] is None:
            return []
        rows = connection.execute(
            "SELECT proxy_id FROM hosted_login_reservations "
            "WHERE released_at IS NULL AND expires_at > now()"
        ).fetchall()
        return [row["proxy_id"] for row in rows]

    def active_provider_references(self, provider_name: str) -> frozenset[str]:
        """Return physical leases held by an active account binding."""

        with self.pool.connection() as connection:
            return frozenset(
                self._active_provider_references(connection, provider_name)
            )

    def get_or_create(
        self,
        *,
        user_id: UUID,
        ig_account_id: UUID,
        geo: Geo,
        allocate: Callable[..., ProxyEndpoint],
        provider_name: str,
    ) -> tuple[ProxyEndpoint, bool]:
        """Reuse an active binding or create exactly one under the account lock."""

        try:
            with self.pool.connection() as connection:
                self._lock_and_validate_account(
                    connection, user_id=user_id, ig_account_id=ig_account_id, geo=geo
                )
                current = connection.execute(
                    _ACTIVE_BINDING_SQL,
                    {"user_id": user_id, "account_id": ig_account_id},
                ).fetchone()
                if current is not None:
                    endpoint = self._endpoint_from_row(current, ig_account_id)
                    self._validate_endpoint(
                        endpoint, geo=geo, provider_name=provider_name
                    )
                    return endpoint, False

                self._lock_provider_inventory(connection, provider_name)
                excluded = self._active_provider_references(connection, provider_name)
                allocated = self._claim_managed_inventory(
                    connection,
                    geo=geo,
                    ig_account_id=ig_account_id,
                    provider_name=provider_name,
                )
                if allocated is None:
                    allocated = self._allocate(
                        allocate, geo, excluded_references=frozenset(excluded)
                    )
                    self._validate_endpoint(
                        allocated, geo=geo, provider_name=provider_name
                    )
                    proxy_id = self._prepare_unbound_proxy(
                        connection,
                        endpoint=allocated,
                        ig_account_id=ig_account_id,
                    )
                else:
                    proxy_id = allocated.id
                    if proxy_id is None:
                        raise ProxyBindingError("managed proxy identity unavailable")
                connection.execute(
                    """
                    INSERT INTO proxy_bindings (
                        user_id, ig_account_id, proxy_id
                    )
                    VALUES (%(user_id)s, %(account_id)s, %(proxy_id)s)
                    """,
                    {
                        "user_id": user_id,
                        "account_id": ig_account_id,
                        "proxy_id": proxy_id,
                    },
                )
                return self._with_id(allocated, proxy_id), True
        except UniqueViolation as error:
            raise ProxyBindingError(
                "proxy or account already has an active binding"
            ) from error

    def rebind(
        self,
        *,
        user_id: UUID,
        ig_account_id: UUID,
        geo: Geo,
        reason: str,
        allocate: Callable[..., ProxyEndpoint],
        check: Callable[[ProxyEndpoint], ProxyHealth],
        provider_name: str,
    ) -> ProxyEndpoint:
        """Atomically replace a binding only through an explicit admin action."""

        reason = self._validate_rebind_reason(reason)
        try:
            with self.pool.connection() as connection:
                self._lock_and_validate_account(
                    connection, user_id=user_id, ig_account_id=ig_account_id, geo=geo
                )
                current = connection.execute(
                    _ACTIVE_BINDING_SQL + " FOR UPDATE OF binding",
                    {"user_id": user_id, "account_id": ig_account_id},
                ).fetchone()
                if current is None:
                    raise ProxyBindingError("active proxy binding unavailable")

                self._lock_provider_inventory(connection, provider_name)
                if current["id"] in self._reserved_proxy_ids(connection):
                    raise ProxyBindingError("account login is in progress")
                excluded = self._active_provider_references(connection, provider_name)
                allocated = self._claim_managed_inventory(
                    connection,
                    geo=geo,
                    ig_account_id=ig_account_id,
                    provider_name=provider_name,
                )
                if allocated is None:
                    allocated = self._allocate(
                        allocate, geo, excluded_references=frozenset(excluded)
                    )
                    self._validate_endpoint(
                        allocated, geo=geo, provider_name=provider_name
                    )
                candidate_health = check(allocated)
                if (
                    not isinstance(candidate_health, ProxyHealth)
                    or candidate_health.status is not ProxyHealthStatus.HEALTHY
                ):
                    raise ProxyBindingError("replacement proxy health check failed")
                proxy_id = allocated.id
                if proxy_id is None:
                    proxy_id = self._prepare_unbound_proxy(
                        connection,
                        endpoint=allocated,
                        ig_account_id=ig_account_id,
                    )
                if proxy_id == current["id"]:
                    raise ProxyBindingError("replacement proxy must be different")

                released = connection.execute(
                    """
                    UPDATE proxy_bindings
                    SET released_at = now()
                    WHERE user_id = %(user_id)s
                      AND ig_account_id = %(account_id)s
                      AND proxy_id = %(old_proxy_id)s
                      AND released_at IS NULL
                    RETURNING id
                    """,
                    {
                        "user_id": user_id,
                        "account_id": ig_account_id,
                        "old_proxy_id": current["id"],
                    },
                ).fetchone()
                if released is None:
                    raise ProxyBindingError("active proxy binding unavailable")

                connection.execute(
                    """
                    INSERT INTO proxy_bindings (
                        user_id, ig_account_id, proxy_id
                    )
                    VALUES (%(user_id)s, %(account_id)s, %(proxy_id)s)
                    """,
                    {
                        "user_id": user_id,
                        "account_id": ig_account_id,
                        "proxy_id": proxy_id,
                    },
                )
                connection.execute(
                    """
                    UPDATE proxies
                    SET health_status = 'active', last_checked_at = now()
                    WHERE id = %(proxy_id)s
                    """,
                    {"proxy_id": proxy_id},
                )
                connection.execute(
                    """
                    INSERT INTO account_health_events (
                        user_id, ig_account_id, event_type, severity, evidence
                    )
                    VALUES (
                        %(user_id)s, %(account_id)s, 'proxy_rebound', 'info',
                        jsonb_build_object(
                            'reason', CAST(%(reason)s AS text),
                            'previous_proxy_id', CAST(%(old_proxy_id)s AS text),
                            'proxy_id', CAST(%(proxy_id)s AS text)
                        )
                    )
                    """,
                    {
                        "user_id": user_id,
                        "account_id": ig_account_id,
                        "reason": reason,
                        "old_proxy_id": current["id"],
                        "proxy_id": proxy_id,
                    },
                )
                return self._with_id(allocated, proxy_id)
        except UniqueViolation as error:
            raise ProxyBindingError(
                "proxy or account already has an active binding"
            ) from error

    def record_health(
        self,
        *,
        user_id: UUID,
        ig_account_id: UUID,
        endpoint: ProxyEndpoint,
        health: ProxyHealth,
        job_id: UUID | None = None,
        transient: bool = False,
    ) -> None:
        """Update only the account's current proxy and record fail-closed evidence."""

        if endpoint.id is None:
            raise ProxyBindingError("persisted proxy identity unavailable")
        if health.status is ProxyHealthStatus.INDETERMINATE:
            # Nobody could tell us where this proxy is. That is a fact about
            # the geolocation providers, not about the lease, so the ledger
            # keeps whatever it last knew. Writing 'degraded' here would take
            # good inventory out of supply every time an oracle had a bad hour.
            return
        health_status = (
            "active"
            if health.status is ProxyHealthStatus.HEALTHY or transient
            else "degraded"
        )
        with self.pool.connection() as connection:
            updated = connection.execute(
                """
                UPDATE proxies proxy
                SET health_status = %(health_status)s,
                    last_checked_at = now()
                FROM proxy_bindings binding
                WHERE proxy.id = %(proxy_id)s
                  AND binding.proxy_id = proxy.id
                  AND binding.user_id = %(user_id)s
                  AND binding.ig_account_id = %(account_id)s
                  AND binding.released_at IS NULL
                RETURNING proxy.id
                """,
                {
                    "health_status": health_status,
                    "proxy_id": endpoint.id,
                    "user_id": user_id,
                    "account_id": ig_account_id,
                },
            ).fetchone()
            if updated is None:
                raise ProxyBindingError("active proxy binding unavailable")
            if health.status is ProxyHealthStatus.DEGRADED:
                # Tag only a current, tenant-owned job. Other proxy callers
                # retain untagged health evidence and cannot imply job causality.
                owned_job = None
                if job_id is not None and type(job_id) is UUID:
                    owned_job = connection.execute(
                        """SELECT id FROM jobs WHERE id=%s AND user_id=%s
                        AND ig_account_id=%s AND status='running'
                        AND lease_expires_at > now()""",
                        (job_id, user_id, ig_account_id),
                    ).fetchone()
                connection.execute(
                    """
                    INSERT INTO account_health_events (
                        user_id, ig_account_id, event_type, severity, evidence
                    )
                    VALUES (
                        %(user_id)s, %(account_id)s,
                        'proxy_health_failed', 'warning',
                        jsonb_build_object(
                            'proxy_id', CAST(%(proxy_id)s AS text),
                            'reason', CAST(%(reason)s AS text)
                        ) || CASE WHEN CAST(%(job_id)s AS text) IS NULL THEN '{}'::jsonb
                                  ELSE jsonb_build_object('job_id', CAST(%(job_id)s AS text)) END
                    )
                    """,
                    {
                        "user_id": user_id,
                        "account_id": ig_account_id,
                        "proxy_id": endpoint.id,
                        "reason": health.reason,
                        "job_id": job_id if owned_job is not None else None,
                    },
                )

    @staticmethod
    def _validate_rebind_reason(reason: str) -> str:
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or reason != reason.strip()
            or len(reason) > 500
            or any(ord(character) < 32 for character in reason)
        ):
            raise ProxyBindingError("explicit rebind reason is required")
        return reason

    @staticmethod
    def _lock_and_validate_account(
        connection: Connection,
        *,
        user_id: UUID,
        ig_account_id: UUID,
        geo: Geo,
    ) -> None:
        account = connection.execute(
            """
            SELECT login_country, login_region, login_city
            FROM ig_accounts
            WHERE id = %(account_id)s AND user_id = %(user_id)s
            FOR UPDATE
            """,
            {"account_id": ig_account_id, "user_id": user_id},
        ).fetchone()
        if account is None:
            raise AccountUnavailableError("Instagram account unavailable")
        try:
            declared = Geo(
                _text(account["login_country"]),
                _text(account["login_region"]),
                _text(account["login_city"]),
            )
        except (TypeError, ValueError) as error:
            raise ProxyBindingError(
                "Instagram account has incomplete login geography"
            ) from error
        if declared != geo:
            raise ProxyBindingError(
                "requested proxy geography does not match declared login geography"
            )

    @staticmethod
    def _validate_endpoint(
        endpoint: ProxyEndpoint,
        *,
        geo: Geo,
        provider_name: str,
    ) -> None:
        if endpoint.provider != provider_name:
            raise ProxyBindingError("proxy provider identity mismatch")
        if endpoint.proxy_type != "isp_static_residential":
            raise ProxyBindingError("proxy is not a dedicated ISP/static lease")
        if endpoint.geo != geo:
            raise ProxyBindingError(
                "provider endpoint geography does not match login geography"
            )

    @staticmethod
    def _lock_provider_inventory(connection: Connection, provider_name: str) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"proxy-provider-inventory:{provider_name}",),
        )

    @staticmethod
    def _active_provider_references(
        connection: Connection, provider_name: str
    ) -> set[str]:
        rows = connection.execute(
            """
            SELECT proxy.provider_reference
            FROM proxy_bindings binding
            JOIN proxies proxy ON proxy.id = binding.proxy_id
            WHERE proxy.provider = %(provider)s
              AND binding.released_at IS NULL
            """,
            {"provider": provider_name},
        ).fetchall()
        reserved = ProxyRepository._reserved_proxy_ids(connection)
        held = connection.execute(
            "SELECT provider_reference FROM proxies WHERE provider = %s "
            "AND id = ANY(%s::uuid[])", (provider_name, reserved),
        ).fetchall() if reserved else []
        return {_text(row["provider_reference"]) for row in [*rows, *held]}

    @staticmethod
    def _allocate(
        allocate: Callable[..., ProxyEndpoint],
        geo: Geo,
        *,
        excluded_references: frozenset[str],
    ) -> ProxyEndpoint:
        try:
            return allocate(geo, excluded_references=excluded_references)
        except Exception:  # noqa: BLE001 -- provider errors may expose credentials
            # Provider errors can contain credential-bearing proxy URLs.
            raise ProxyBindingError("proxy inventory unavailable") from None

    def _prepare_unbound_proxy(
        self,
        connection: Connection,
        *,
        endpoint: ProxyEndpoint,
        ig_account_id: UUID,
    ) -> UUID:
        # Serialize a physical lease across all accounts. Reusing one proxy row
        # keeps the active-proxy partial unique index authoritative.
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"proxy:{endpoint.provider}:{endpoint.provider_reference}",),
        )
        row = connection.execute(
            """
            SELECT id, proxy_type, country, region, city, host, port
            FROM proxies
            WHERE provider = %(provider)s
              AND provider_reference = %(reference)s
            ORDER BY created_at, id
            LIMIT 1
            FOR UPDATE
            """,
            {
                "provider": endpoint.provider,
                "reference": endpoint.provider_reference,
            },
        ).fetchone()

        if row is not None:
            if row["id"] in self._reserved_proxy_ids(connection):
                raise ProxyBindingError("provider lease is reserved")
            active = connection.execute(
                """
                SELECT ig_account_id
                FROM proxy_bindings
                WHERE proxy_id = %(proxy_id)s AND released_at IS NULL
                """,
                {"proxy_id": row["id"]},
            ).fetchone()
            if active is not None:
                raise ProxyBindingError("provider lease is already bound")
            self._validate_stored_metadata(row, endpoint)
            proxy_id = row["id"]
            connection.execute(
                """
                UPDATE proxies
                SET encrypted_username = %(username)s,
                    encrypted_password = %(password)s,
                    health_status = 'active',
                    last_checked_at = NULL
                WHERE id = %(proxy_id)s
                """,
                {
                    "username": self._encrypt(endpoint.username, ig_account_id),
                    "password": self._encrypt(endpoint.password, ig_account_id),
                    "proxy_id": proxy_id,
                },
            )
            return proxy_id

        inserted = connection.execute(
            """
            INSERT INTO proxies (
                provider, provider_reference, proxy_type,
                country, region, city, host, port,
                encrypted_username, encrypted_password
            )
            VALUES (
                %(provider)s, %(reference)s, %(proxy_type)s,
                %(country)s, %(region)s, %(city)s, %(host)s, %(port)s,
                %(username)s, %(password)s
            )
            RETURNING id
            """,
            {
                "provider": endpoint.provider,
                "reference": endpoint.provider_reference,
                "proxy_type": endpoint.proxy_type,
                "country": endpoint.country,
                "region": endpoint.region,
                "city": endpoint.city,
                "host": endpoint.host,
                "port": endpoint.port,
                "username": self._encrypt(endpoint.username, ig_account_id),
                "password": self._encrypt(endpoint.password, ig_account_id),
            },
        ).fetchone()
        return inserted["id"]

    def _claim_managed_inventory(
        self,
        connection: Connection,
        *,
        geo: Geo,
        ig_account_id: UUID,
        provider_name: str,
    ) -> ProxyEndpoint | None:
        row = connection.execute(
            """
            SELECT proxy.id,
                   proxy.provider,
                   proxy.provider_reference,
                   proxy.proxy_type,
                   proxy.country,
                   proxy.region,
                   proxy.city,
                   proxy.host,
                   proxy.port,
                   proxy.encrypted_username,
                   proxy.encrypted_password
            FROM proxies proxy
            WHERE proxy.provider = %(provider)s
              AND proxy.proxy_type = 'isp_static_residential'
              AND proxy.country = %(country)s
              AND proxy.region = %(region)s
              AND proxy.city = %(city)s
              AND proxy.health_status = 'active'
              AND proxy.credential_scope = 'inventory'
              AND NOT (proxy.id = ANY(%(reserved)s::uuid[]))
              AND NOT EXISTS (
                  SELECT 1
                  FROM proxy_bindings binding
                  WHERE binding.proxy_id = proxy.id
                    AND binding.released_at IS NULL
              )
            ORDER BY proxy.created_at, proxy.id
            LIMIT 1
            FOR UPDATE OF proxy SKIP LOCKED
            """,
            {
                "provider": provider_name,
                "reserved": self._reserved_proxy_ids(connection),
                "country": geo.country,
                "region": geo.region,
                "city": geo.city,
            },
        ).fetchone()
        if row is None:
            return None
        try:
            username = self._decrypt(row["encrypted_username"], row["id"])
            password = self._decrypt(row["encrypted_password"], row["id"])
            endpoint = ProxyEndpoint(
                id=row["id"],
                provider=_text(row["provider"]),
                provider_reference=_text(row["provider_reference"]),
                proxy_type=_text(row["proxy_type"]),
                geo=Geo(
                    _text(row["country"]),
                    _text(row["region"]),
                    _text(row["city"]),
                ),
                host=_text(row["host"]),
                port=row["port"],
                username=username,
                password=password,
            )
            self._validate_endpoint(endpoint, geo=geo, provider_name=provider_name)
        except (KeyError, TypeError, ValueError, SessionCipherError):
            raise ProxyBindingError("managed proxy inventory is invalid") from None
        updated = connection.execute(
            """
            UPDATE proxies
            SET encrypted_username = %(username)s,
                encrypted_password = %(password)s,
                credential_scope = 'account'
            WHERE id = %(proxy_id)s AND credential_scope = 'inventory'
            RETURNING id
            """,
            {
                "proxy_id": endpoint.id,
                "username": self._encrypt(endpoint.username, ig_account_id),
                "password": self._encrypt(endpoint.password, ig_account_id),
            },
        ).fetchone()
        if updated is None:
            raise ProxyBindingError("managed proxy inventory unavailable")
        return endpoint

    @staticmethod
    def _validate_stored_metadata(
        row: Mapping[str, Any], endpoint: ProxyEndpoint
    ) -> None:
        stored = (
            _text(row["proxy_type"]),
            _text(row["country"]),
            _text(row["region"]),
            _text(row["city"]),
            _text(row["host"]),
            row["port"],
        )
        supplied = (
            endpoint.proxy_type,
            endpoint.country,
            endpoint.region,
            endpoint.city,
            endpoint.host,
            endpoint.port,
        )
        if stored != supplied:
            raise ProxyBindingError("provider lease metadata mismatch")

    def _endpoint_from_row(
        self, row: Mapping[str, Any], ig_account_id: UUID
    ) -> ProxyEndpoint:
        try:
            username = self._decrypt(row["encrypted_username"], ig_account_id)
            password = self._decrypt(row["encrypted_password"], ig_account_id)
            return ProxyEndpoint(
                id=row["id"],
                provider=_text(row["provider"]),
                provider_reference=_text(row["provider_reference"]),
                proxy_type=_text(row["proxy_type"]),
                geo=Geo(
                    _text(row["country"]),
                    _text(row["region"]),
                    _text(row["city"]),
                ),
                host=_text(row["host"]),
                port=row["port"],
                username=username,
                password=password,
            )
        except (KeyError, TypeError, ValueError, SessionCipherError) as error:
            raise ProxyBindingError("stored proxy binding is invalid") from error

    def _encrypt(self, value: str, ig_account_id: UUID) -> str:
        return SessionCipher(self._session_encryption_key, ig_account_id).encrypt(
            {"value": value}
        )

    def _decrypt(self, value: str | bytes, ig_account_id: UUID) -> str:
        payload = SessionCipher(self._session_encryption_key, ig_account_id).decrypt(
            _text(value)
        )
        secret = payload.get("value")
        if not isinstance(secret, str):
            raise ProxyBindingError("stored proxy binding is invalid")
        return secret

    @staticmethod
    def _with_id(endpoint: ProxyEndpoint, proxy_id: UUID) -> ProxyEndpoint:
        return ProxyEndpoint(
            id=proxy_id,
            provider=endpoint.provider,
            provider_reference=endpoint.provider_reference,
            proxy_type=endpoint.proxy_type,
            geo=endpoint.geo,
            host=endpoint.host,
            port=endpoint.port,
            username=endpoint.username,
            password=endpoint.password,
        )


class ProxyBindingService:
    """Apply sticky allocation, exact geo, and fail-closed health rules."""

    def __init__(self, repository: ProxyRepository, provider: ProxyProvider) -> None:
        self.repository = repository
        self.provider = provider

    def __repr__(self) -> str:
        return "ProxyBindingService(repository=<configured>, provider=<configured>)"

    def ensure_binding(
        self, user_id: UUID, ig_account_id: UUID, geo: Geo, *, job_id: UUID | None = None
    ) -> ProxyEndpoint:
        endpoint, _created = self.repository.get_or_create(
            user_id=user_id,
            ig_account_id=ig_account_id,
            geo=geo,
            allocate=self.provider.allocate_static,
            provider_name=self.provider.name,
        )
        health = self._check(endpoint)
        transient = (
            health.status is ProxyHealthStatus.DEGRADED
            and health.reason in _TRANSIENT_HEALTH_REASONS
        )
        self.repository.record_health(
            user_id=user_id,
            ig_account_id=ig_account_id,
            endpoint=endpoint,
            health=health,
            job_id=job_id,
            transient=transient,
        )
        if transient:
            raise ProxyBindingError(
                "proxy health check temporarily unavailable",
                transient=True,
                reason=health.reason,
            )
        # Exhaustive on purpose. This used to test DEGRADED and return the
        # endpoint for anything else, so a third status bound an account to a
        # proxy nobody had confirmed, silently.
        if health.status is ProxyHealthStatus.DEGRADED:
            raise ProxyBindingError("proxy health check failed", reason=health.reason)
        if health.status is not ProxyHealthStatus.HEALTHY:
            raise ProxyBindingError("proxy geography unverified")
        return endpoint

    def rebind(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        geo: Geo,
        *,
        reason: str,
    ) -> ProxyEndpoint:
        endpoint = self.repository.rebind(
            user_id=user_id,
            ig_account_id=ig_account_id,
            geo=geo,
            reason=reason,
            allocate=self.provider.allocate_static,
            check=self._check,
            provider_name=self.provider.name,
        )
        return endpoint

    def _check(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        try:
            health = self.provider.check(endpoint)
        except Exception:  # noqa: BLE001 -- provider errors may expose credentials
            # Third-party exceptions can contain proxy URLs and credentials.
            return ProxyHealth.degraded("provider_error")
        if not isinstance(health, ProxyHealth):
            return ProxyHealth.degraded("invalid_provider_response")
        return health
