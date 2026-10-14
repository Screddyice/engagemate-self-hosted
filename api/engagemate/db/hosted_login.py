"""Short-lived, tenant-owned reservations of already purchased login proxies."""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg_pool import ConnectionPool

from engagemate.config import Settings
from engagemate.db.accounts import AccountRepository, account_seats, canonicalize_instagram_username
from engagemate.db.proxies import ProxyRepository, _ACTIVE_BINDING_SQL
from engagemate.db.text import text
from engagemate.proxy.base import Geo, ProxyEndpoint, ProxyHealthStatus
from engagemate.proxy.geo_anchors import GeoAnchorRegistry
from engagemate.proxy.geo_selection import GeoOption, recommend_login_geo, resolve_timezone_point
from engagemate.proxy.iproyal import IPRoyalProvider
from engagemate.browser.fingerprint import FingerprintGeoRegistry

if TYPE_CHECKING:
    from engagemate.control.routes.accounts import SessionCapture, SessionAccountResponse


logger = logging.getLogger(__name__)


class HostedLoginError(ValueError):
    """Only stable, secret-free codes cross the login boundary."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class Reservation:
    id: UUID
    user_id: UUID
    account_id: UUID
    endpoint: ProxyEndpoint = field(repr=False)
    geo: Geo
    expires_at: datetime
    expected_ig_user_id: str | None


class HostedLoginRepository:
    # A proxy can briefly return a transport or malformed-provider result while
    # the lease is still valid. Retry those observations once, but keep
    # authentication, IP, and geography mismatches fail-closed.
    _TRANSIENT_PROXY_HEALTH_REASONS = frozenset({
        "connection_failed",
        "unexpected_status",
        "invalid_response",
    })

    def __init__(self, pool: ConnectionPool, settings: Settings):
        self.pool = pool
        self.settings = settings
        self.proxies = ProxyRepository(pool, settings.session_encryption_key.get_secret_value())
        self.accounts = AccountRepository(pool, settings.session_encryption_key.get_secret_value())
        # Hosted sign-in has a short request budget. Keep the existing health
        # verdict, but bound each oracle request more tightly than worker jobs.
        health_settings = settings.model_copy(update={
            "proxy_connect_timeout_seconds": min(settings.proxy_connect_timeout_seconds, 2.0),
            "proxy_read_timeout_seconds": min(settings.proxy_read_timeout_seconds, 3.0),
        })
        self.provider = IPRoyalProvider.from_settings(health_settings)

    def __repr__(self) -> str:
        return "HostedLoginRepository(<configured>)"

    def _check_proxy_health(self, endpoint: ProxyEndpoint):
        """Return a bounded proxy verdict without condemning transient outages."""

        health = self.provider.check(endpoint)
        if (
            health.status is ProxyHealthStatus.HEALTHY
            or health.reason not in self._TRANSIENT_PROXY_HEALTH_REASONS
        ):
            return health
        # Keep the retry short enough for the hosted login start budget while
        # allowing a just-recovering proxy tunnel or health oracle to settle.
        time.sleep(0.2)
        return self.provider.check(endpoint)

    @staticmethod
    def _lock_user(connection: Connection, user_id: UUID) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(CAST(%s AS text), 0))",
            (str(user_id),),
        )

    def _check_capacity(self, connection: Connection, user_id: UUID, *, exclude: UUID | None = None) -> None:
        owner = connection.execute("SELECT plan FROM users WHERE id = %s", (user_id,)).fetchone()
        if owner is None:
            raise HostedLoginError("account_unavailable")
        owned = connection.execute("SELECT count(*) AS n FROM ig_accounts WHERE user_id = %s", (user_id,)).fetchone()["n"]
        if owned >= account_seats(owner["plan"]):
            raise HostedLoginError("account_limit")

    def reserve(self, user_id: UUID, username: str, timezone: str, *, account_id: UUID | None = None) -> Reservation:
        try:
            username = canonicalize_instagram_username(username.strip())
            if len(username) > 30:
                raise ValueError()
        except (ValueError, AttributeError):
            raise HostedLoginError("invalid_username") from None
        reservation = None
        ready = False
        try:
            reservation = self._reserve(user_id, username, timezone, account_id=account_id)
            # Commit the claim before network I/O. Other users can allocate
            # unrelated inventory while the provider's health oracle responds.
            health = self._check_proxy_health(reservation.endpoint)
            if health.status is not ProxyHealthStatus.HEALTHY:
                # ProxyHealth reasons are validated stable codes. Keep the
                # diagnostic useful without writing endpoint or credential data.
                logger.warning("hosted_login_proxy_check_failed reason=%s", health.reason)
                raise HostedLoginError("proxy_unavailable")
            with self.pool.connection() as connection:
                connection.execute("SET LOCAL lock_timeout = '5s'")
                connection.execute("SET LOCAL statement_timeout = '15s'")
                updated = connection.execute("""
                    UPDATE proxies SET health_status = 'active', last_checked_at = now()
                    WHERE id = %s AND EXISTS (
                        SELECT 1 FROM hosted_login_reservations
                        WHERE id = %s AND user_id = %s
                          AND released_at IS NULL AND expires_at > now()
                    )
                """, (reservation.endpoint.id, reservation.id, user_id))
                if updated.rowcount != 1:
                    raise HostedLoginError("session_unavailable")
            ready = True
            return reservation
        except HostedLoginError as error:
            logger.warning("hosted_login_reserve_failed code=%s", error.code)
            raise
        except Exception as error:
            # Provider and database failures can contain credential-bearing text.
            logger.warning(
                "hosted_login_reserve_failed code=connection_unavailable type=%s",
                type(error).__name__,
            )
            raise HostedLoginError("connection_unavailable") from None
        finally:
            if reservation is not None and not ready:
                # If the database is unavailable, the bounded claim expires.
                with suppress(Exception):
                    self.release(reservation.id, user_id)

    def _reserve(self, user_id: UUID, username: str, timezone: str, *, account_id: UUID | None = None) -> Reservation:
        with self.pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SET LOCAL statement_timeout = '15s'")
            self._lock_user(connection, user_id)
            # Match binder lock ordering: account row, then provider inventory.
            if account_id is None:
                account = connection.execute("""
                    SELECT id, ig_user_id, login_country, login_region, login_city
                    FROM ig_accounts WHERE user_id = %s AND username = %s FOR UPDATE
                """, (user_id, username)).fetchone()
            else:
                account = connection.execute("""
                    SELECT id, ig_user_id, login_country, login_region, login_city
                    FROM ig_accounts
                    WHERE user_id = %s AND id = %s AND lower(username) = lower(%s)
                    FOR UPDATE
                """, (user_id, account_id, username)).fetchone()
            self.proxies._lock_provider_inventory(connection, "iproyal")
            connection.execute("""
                UPDATE hosted_login_reservations SET released_at = now()
                WHERE released_at IS NULL AND expires_at <= now()
            """)
            if connection.execute("SELECT 1 FROM hosted_login_reservations WHERE user_id = %s AND released_at IS NULL", (user_id,)).fetchone():
                raise HostedLoginError("connection_in_progress")
            account_id = account["id"] if account else uuid4()
            expected = text(account["ig_user_id"]) if account and account["ig_user_id"] else None
            if account:
                if expected is None:
                    raise HostedLoginError("account_unavailable")
                row = connection.execute(_ACTIVE_BINDING_SQL, {"user_id": user_id, "account_id": account_id}).fetchone()
                if row is None:
                    raise HostedLoginError("proxy_unavailable")
                endpoint = self.proxies._endpoint_from_row(row, account_id)
                geo = Geo(text(account["login_country"]), text(account["login_region"]), text(account["login_city"]))
                self.proxies._validate_endpoint(endpoint, geo=geo, provider_name="iproyal")
            else:
                self._check_capacity(connection, user_id)
                endpoint = self._select_inventory(connection, timezone)
                geo = endpoint.geo
            FingerprintGeoRegistry.from_settings(self.settings).require(geo)
            row = connection.execute("""
                INSERT INTO hosted_login_reservations (user_id, account_id, username, expected_ig_user_id, proxy_id)
                VALUES (%s, %s, %s, %s, %s) RETURNING id, expires_at
            """, (user_id, account_id, username, expected, endpoint.id)).fetchone()
            return Reservation(row["id"], user_id, account_id, endpoint, geo, row["expires_at"], expected)

    def _select_inventory(self, connection: Connection, timezone_name: str) -> ProxyEndpoint:
        held = self.proxies._reserved_proxy_ids(connection)
        rows = connection.execute("""
            SELECT proxy.* FROM proxies proxy
            WHERE provider = 'iproyal' AND proxy_type = 'isp_static_residential'
              AND health_status = 'active' AND credential_scope = 'inventory'
              AND NOT (id = ANY(%s::uuid[]))
              AND NOT EXISTS (SELECT 1 FROM proxy_bindings b WHERE b.proxy_id = proxy.id AND b.released_at IS NULL)
            ORDER BY created_at, id FOR UPDATE OF proxy
        """, (held,)).fetchall()
        endpoints = [self.proxies._endpoint_from_row(row, row["id"]) for row in rows]
        excluded = self.proxies._active_provider_references(connection, "iproyal") | {e.provider_reference for e in endpoints}
        if self.settings.iproyal_mode == "import":
            # Provider allocation only reads explicitly configured paid leases.
            for geo in self.provider.free_leases_by_geo(excluded_references=frozenset(excluded)):
                endpoints.append(self.provider.allocate_static(geo, excluded_references=frozenset(excluded)))
        point = resolve_timezone_point(timezone_name)
        anchors = GeoAnchorRegistry.from_settings(self.settings).as_mapping()
        options = [GeoOption(e.geo, *anchors[e.geo], 1) for e in endpoints if e.geo in anchors]
        recommended = recommend_login_geo(*point, options) if point else None
        if recommended is None or not recommended.within_guard:
            raise HostedLoginError("geography_unavailable")
        endpoint = next(e for e in endpoints if e.geo == recommended.geo)
        if endpoint.id is None:
            proxy_id = self.proxies._prepare_unbound_proxy(connection, endpoint=endpoint, ig_account_id=uuid4())
            # Retain inventory AAD until completion. Expiry/cancel never needs
            # credentials or a browser to restore an assignable lease.
            connection.execute("""
                UPDATE proxies SET credential_scope = 'inventory', encrypted_username = %s, encrypted_password = %s WHERE id = %s
            """, (self.proxies._encrypt(endpoint.username, proxy_id), self.proxies._encrypt(endpoint.password, proxy_id), proxy_id))
            endpoint = self.proxies._with_id(endpoint, proxy_id)
        return endpoint

    def release(self, reservation_id: UUID, user_id: UUID) -> None:
        with self.pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SET LOCAL statement_timeout = '15s'")
            connection.execute("""
                UPDATE hosted_login_reservations SET released_at = coalesce(released_at, now())
                WHERE id = %s AND user_id = %s
            """, (reservation_id, user_id))

    def completed(self, reservation_id: UUID, user_id: UUID) -> dict | None:
        """Recover the atomic completion receipt after a service restart.

        complete updates captured_at and released_at with PostgreSQL now() in
        the SAME transaction. Cancellation/release never writes a session.
        """
        with self.pool.connection() as connection:
            row = connection.execute("""
                SELECT r.id, r.expires_at, a.id AS account_id, a.username,
                       a.health_status, s.captured_at
                FROM hosted_login_reservations r
                JOIN ig_accounts a ON a.id=r.account_id AND a.user_id=r.user_id
                    AND a.username=r.username
                    AND (r.expected_ig_user_id IS NULL OR a.ig_user_id=r.expected_ig_user_id)
                JOIN ig_sessions s ON s.ig_account_id=a.id AND s.user_id=a.user_id
                    AND s.captured_at=r.released_at
                JOIN proxy_bindings b ON b.ig_account_id=a.id AND b.user_id=a.user_id
                    AND b.proxy_id=r.proxy_id AND b.released_at IS NULL
                WHERE r.id=%s AND r.user_id=%s
                    AND r.released_at >= r.created_at
                    AND r.released_at < r.expires_at
                    AND r.released_at > now() - interval '1 day'
                LIMIT 1
            """, (reservation_id, user_id)).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]), "state": "connected",
            "expires_at": row["expires_at"].isoformat(),
            "username": text(row["username"]), "width": 1024, "height": 768,
            "account": {
                "id": str(row["account_id"]), "username": text(row["username"]),
                "health": text(row["health_status"]), "proxy_bound": True,
                "captured_at": row["captured_at"].isoformat(),
            },
        }

    def complete(self, reservation_id: UUID, user_id: UUID, capture: SessionCapture) -> SessionAccountResponse:
        try:
            return self._complete(reservation_id, user_id, capture)
        except HostedLoginError:
            raise
        except Exception:
            raise HostedLoginError("connection_unavailable") from None

    def _complete(self, reservation_id: UUID, user_id: UUID, capture: SessionCapture) -> SessionAccountResponse:
        from engagemate.control.routes.accounts import SessionAccountResponse

        with self.pool.connection() as connection:
            connection.execute("SET LOCAL lock_timeout = '5s'")
            connection.execute("SET LOCAL statement_timeout = '15s'")
            self._lock_user(connection, user_id)
            # Discover only owned identity first, then match allocator lock order.
            reservation = connection.execute("SELECT * FROM hosted_login_reservations WHERE id = %s AND user_id = %s", (reservation_id, user_id)).fetchone()
            if reservation is None:
                raise HostedLoginError("session_unavailable")
            account_id = reservation["account_id"]
            connection.execute("SELECT id FROM ig_accounts WHERE id = %s AND user_id = %s FOR UPDATE", (account_id, user_id)).fetchone()
            self.proxies._lock_provider_inventory(connection, "iproyal")
            reservation = connection.execute("SELECT * FROM hosted_login_reservations WHERE id = %s AND user_id = %s AND released_at IS NULL AND expires_at > now() FOR UPDATE", (reservation_id, user_id)).fetchone()
            if reservation is None:
                raise HostedLoginError("session_unavailable")
            expected = text(reservation["expected_ig_user_id"]) if reservation["expected_ig_user_id"] else None
            if capture.username != text(reservation["username"]) or (expected and capture.ig_user_id != expected):
                raise HostedLoginError("identity_mismatch")
            # Numeric cookie identity must agree with the authenticated identity
            # checked by the hosted browser, never just with the entered handle.
            ids = [c.value for c in capture.cookies if c.name == "ds_user_id"]
            if ids != [capture.ig_user_id]:
                raise HostedLoginError("identity_mismatch")
            existing = connection.execute("SELECT id FROM ig_accounts WHERE user_id = %s AND ig_user_id = %s", (user_id, capture.ig_user_id)).fetchone()
            if (existing is not None and existing["id"] != account_id) or (expected is not None and existing is None):
                raise HostedLoginError("identity_mismatch")
            if expected is None:
                self._check_capacity(connection, user_id, exclude=reservation_id)
            proxy = connection.execute("SELECT * FROM proxies WHERE id = %s FOR UPDATE", (reservation["proxy_id"],)).fetchone()
            geo = Geo(text(proxy["country"]), text(proxy["region"]), text(proxy["city"]))
            if (capture.login_geo.country, capture.login_geo.region, capture.login_geo.city) != (geo.country, geo.region, geo.city):
                raise HostedLoginError("identity_mismatch")
            if text(proxy["health_status"]) != "active":
                raise HostedLoginError("proxy_unavailable")
            binding = connection.execute("SELECT ig_account_id, user_id FROM proxy_bindings WHERE proxy_id = %s AND released_at IS NULL", (proxy["id"],)).fetchone()
            if expected:
                if not binding or binding["ig_account_id"] != account_id or binding["user_id"] != user_id:
                    raise HostedLoginError("proxy_unavailable")
            elif binding or text(proxy["credential_scope"]) != "inventory":
                raise HostedLoginError("proxy_unavailable")
            cookie = next(c for c in capture.cookies if c.name == "sessionid")
            expires = datetime.fromtimestamp(cookie.expiration_date, tz=timezone.utc) if cookie.expiration_date is not None else None
            account, _ = self.accounts.upsert_session(
                user_id=user_id, username=capture.username, ig_user_id=capture.ig_user_id,
                cookies=[c.model_dump(by_alias=True, exclude_none=True) for c in capture.cookies],
                user_agent=capture.user_agent, login_country=geo.country, login_region=geo.region,
                login_city=geo.city, expires_at=expires, connection=connection, account_id=account_id,
            )
            if account.id != account_id:
                raise HostedLoginError("identity_mismatch")
            if expected is None:
                endpoint = self.proxies._endpoint_from_row(proxy, proxy["id"])
                connection.execute("UPDATE proxies SET credential_scope = 'account', encrypted_username = %s, encrypted_password = %s WHERE id = %s", (self.proxies._encrypt(endpoint.username, account_id), self.proxies._encrypt(endpoint.password, account_id), proxy["id"]))
                connection.execute("INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id) VALUES (%s, %s, %s)", (user_id, account_id, proxy["id"]))
            health = connection.execute("""
                UPDATE ig_accounts SET health_status = CASE
                    WHEN paused_at IS NULL AND health_status IN ('challenged', 'degraded') THEN 'active'
                    ELSE health_status END
                WHERE id = %s AND user_id = %s RETURNING health_status
            """, (account_id, user_id)).fetchone()
            connection.execute("UPDATE hosted_login_reservations SET released_at = now() WHERE id = %s", (reservation_id,))
            return SessionAccountResponse(id=account.id, username=account.username, health=text(health["health_status"]), captured_at=account.captured_at, proxy_bound=True)
