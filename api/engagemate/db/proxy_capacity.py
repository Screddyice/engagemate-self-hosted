"""PostgreSQL state for at-most-once proxy capacity purchases."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from engagemate.db.text import text as _text
from engagemate.proxy.base import Geo, ProxyEndpoint
from engagemate.proxy.capacity import (
    CapacityIntent,
    CapacityPolicy,
    CapacitySnapshot,
    GeoCatalogEntry,
    SubmittedCapacityOrder,
    plan_capacity_order,
)
from engagemate.security.session_cipher import SessionCipher, SessionCipherError
from psycopg import Connection
from psycopg_pool import ConnectionPool

class ProxyCapacityRepositoryError(RuntimeError):
    """A secret-safe failure in capacity state transitions."""


@dataclass(frozen=True, slots=True)
class UnboundAccount:
    user_id: UUID
    account_id: UUID
    geo: Geo


@dataclass(frozen=True, slots=True)
class InventoryProxy:
    """One unbound spare lease, credentials still encrypted."""

    id: UUID
    provider: str
    provider_reference: str
    proxy_type: str
    geo: Geo
    host: str
    port: int
    encrypted_username: str = field(repr=False)
    encrypted_password: str = field(repr=False)
    health_status: str


class PostgresCapacityRepository:
    """Serialize purchase intents per geography before provider-side writes."""

    def __init__(self, pool: ConnectionPool) -> None:
        self.pool = pool

    def get_snapshot(self, geo: Geo) -> CapacitySnapshot:
        with self.pool.connection() as connection:
            return self._get_snapshot(connection, geo)

    def register_existing_order(
        self,
        *,
        provider_order_id: str,
        geo: Geo,
        entry: GeoCatalogEntry,
        quantity: int,
        paid_cost_cents: int,
    ) -> None:
        """Register one operator-purchased order without creating a charge."""

        if (
            not isinstance(provider_order_id, str)
            or not provider_order_id.isdecimal()
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity <= 0
            or isinstance(paid_cost_cents, bool)
            or not isinstance(paid_cost_cents, int)
            or paid_cost_cents < 0
        ):
            raise ProxyCapacityRepositoryError("existing order registration is invalid")
        values = {
            "country": geo.country,
            "region": geo.region,
            "city": geo.city,
            "product_id": entry.product_id,
            "plan_id": entry.plan_id,
            "location_id": entry.location_id,
            "quantity": quantity,
            "cost": paid_cost_cents,
            "provider_order_id": provider_order_id,
        }
        with self.pool.connection() as connection:
            connection.execute(
                """
                INSERT INTO proxy_capacity_orders (
                    country, region, city, product_id, plan_id, location_id,
                    quantity, quoted_cost_cents, status,
                    provider_order_id, provider_status
                )
                VALUES (
                    %(country)s, %(region)s, %(city)s,
                    %(product_id)s, %(plan_id)s, %(location_id)s,
                    %(quantity)s, %(cost)s, 'submitted',
                    %(provider_order_id)s, 'operator-import'
                )
                ON CONFLICT (provider_order_id, country, region, city)
                    WHERE provider_order_id IS NOT NULL
                DO NOTHING
                """,
                values,
            )
            row = connection.execute(
                """
                SELECT country, region, city, product_id, plan_id, location_id,
                       quantity, quoted_cost_cents, status
                FROM proxy_capacity_orders
                WHERE provider_order_id = %(provider_order_id)s
                  AND country = %(country)s
                  AND region = %(region)s
                  AND city = %(city)s
                """,
                values,
            ).fetchone()
        expected = (
            geo.country,
            geo.region,
            geo.city,
            entry.product_id,
            entry.plan_id,
            entry.location_id,
            quantity,
            paid_cost_cents,
        )
        stored = (
            row["country"],
            row["region"],
            row["city"],
            row["product_id"],
            row["plan_id"],
            row["location_id"],
            row["quantity"],
            row["quoted_cost_cents"],
        )
        if stored != expected or row["status"] not in {"submitted", "confirmed"}:
            raise ProxyCapacityRepositoryError(
                "existing order registration conflicts with stored state"
            )

    def reserve_submission(
        self,
        geo: Geo,
        entry: GeoCatalogEntry,
        policy: CapacityPolicy,
        quantity: int,
        quoted_cost_cents: int,
    ) -> CapacityIntent | None:
        """Recheck demand and spend under a geo lock, then record submission."""

        with self.pool.connection() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"proxy-capacity:{geo.country}:{geo.region}:{geo.city}",),
            )
            snapshot = self._get_snapshot(connection, geo)
            plan = plan_capacity_order(snapshot, policy)
            if plan is None or plan.quantity != quantity:
                return None
            if (
                isinstance(quoted_cost_cents, bool)
                or not isinstance(quoted_cost_cents, int)
                or quoted_cost_cents < 0
                or quoted_cost_cents > policy.max_order_cost_cents
                or snapshot.monthly_spend_cents + quoted_cost_cents
                > policy.max_monthly_cost_cents
            ):
                return None
            row = connection.execute(
                """
                INSERT INTO proxy_capacity_orders (
                    country, region, city, product_id, plan_id, location_id,
                    quantity, quoted_cost_cents, status
                )
                VALUES (
                    %(country)s, %(region)s, %(city)s,
                    %(product_id)s, %(plan_id)s, %(location_id)s,
                    %(quantity)s, %(cost)s, 'submitting'
                )
                RETURNING id
                """,
                {
                    "country": geo.country,
                    "region": geo.region,
                    "city": geo.city,
                    "product_id": entry.product_id,
                    "plan_id": entry.plan_id,
                    "location_id": entry.location_id,
                    "quantity": quantity,
                    "cost": quoted_cost_cents,
                },
            ).fetchone()
        return CapacityIntent(
            intent_id=str(row["id"]),
            geo=geo,
            catalog=entry,
            quantity=quantity,
            quoted_cost_cents=quoted_cost_cents,
        )

    def mark_submitted(
        self, intent_id: str, provider_order_id: str, provider_status: str
    ) -> None:
        if not provider_order_id.isdecimal() or not provider_status:
            raise ProxyCapacityRepositoryError("provider order state is invalid")
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE proxy_capacity_orders
                SET status = 'submitted',
                    provider_order_id = %(provider_order_id)s,
                    provider_status = %(provider_status)s,
                    updated_at = now()
                WHERE id = %(intent_id)s AND status = 'submitting'
                RETURNING id
                """,
                {
                    "intent_id": self._intent_uuid(intent_id),
                    "provider_order_id": provider_order_id,
                    "provider_status": provider_status,
                },
            ).fetchone()
        if row is None:
            raise ProxyCapacityRepositoryError("capacity intent transition failed")

    def mark_unknown(self, intent_id: str) -> None:
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE proxy_capacity_orders
                SET status = 'unknown', updated_at = now()
                WHERE id = %(intent_id)s AND status = 'submitting'
                RETURNING id
                """,
                {"intent_id": self._intent_uuid(intent_id)},
            ).fetchone()
        if row is None:
            raise ProxyCapacityRepositoryError("capacity intent transition failed")

    def list_submitted_orders(self) -> tuple[SubmittedCapacityOrder, ...]:
        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT id, provider_order_id, country, region, city
                FROM proxy_capacity_orders
                WHERE status = 'submitted'
                  AND provider_order_id IS NOT NULL
                ORDER BY created_at, id
                """
            ).fetchall()
        return tuple(
            SubmittedCapacityOrder(
                intent_id=str(row["id"]),
                provider_order_id=str(row["provider_order_id"]),
                geo=Geo(row["country"], row["region"], row["city"]),
            )
            for row in rows
        )

    def mark_provider_failed(self, provider_order_id: str, status: str) -> None:
        if not provider_order_id.isdecimal() or status not in {"expired", "refunded"}:
            raise ProxyCapacityRepositoryError("provider order state is invalid")
        with self.pool.connection() as connection:
            row = connection.execute(
                """
                UPDATE proxy_capacity_orders
                SET status = 'failed', provider_status = %(provider_status)s,
                    updated_at = now()
                WHERE provider_order_id = %(provider_order_id)s
                  AND status = 'submitted'
                RETURNING id
                """,
                {
                    "provider_order_id": provider_order_id,
                    "provider_status": status,
                },
            ).fetchone()
        if row is None:
            raise ProxyCapacityRepositoryError("capacity intent transition failed")

    def list_accounts_needing_binding(self) -> tuple[UnboundAccount, ...]:
        """Return accounts without a healthy active dedicated proxy."""

        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT account.user_id, account.id,
                       account.login_country, account.login_region,
                       account.login_city
                FROM ig_accounts account
                WHERE account.login_country IS NOT NULL
                  AND account.login_region IS NOT NULL
                  AND account.login_city IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM proxy_bindings binding
                      JOIN proxies proxy ON proxy.id = binding.proxy_id
                      WHERE binding.user_id = account.user_id
                        AND binding.ig_account_id = account.id
                        AND binding.released_at IS NULL
                        AND proxy.health_status = 'active'
                  )
                ORDER BY account.created_at, account.id
                """
            ).fetchall()
        return tuple(
            UnboundAccount(
                user_id=row["user_id"],
                account_id=row["id"],
                geo=Geo(
                    row["login_country"],
                    row["login_region"],
                    row["login_city"],
                ),
            )
            for row in rows
        )

    def list_unbound_inventory(self) -> tuple[InventoryProxy, ...]:
        """Return encrypted spare leases no account currently holds.

        Bound proxies are excluded: their health belongs to the run that owns
        them, which checks it under the tenant guard in `record_health`.
        """

        with self.pool.connection() as connection:
            rows = connection.execute(
                """
                SELECT proxy.id, proxy.provider, proxy.provider_reference,
                       proxy.proxy_type, proxy.country, proxy.region,
                       proxy.city, proxy.host, proxy.port,
                       proxy.encrypted_username, proxy.encrypted_password,
                       proxy.health_status
                FROM proxies proxy
                WHERE proxy.credential_scope = 'inventory'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM proxy_bindings binding
                      WHERE binding.proxy_id = proxy.id
                        AND binding.released_at IS NULL
                  )
                ORDER BY proxy.country, proxy.region, proxy.city,
                         proxy.created_at, proxy.id
                """
            ).fetchall()
        return tuple(
            InventoryProxy(
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
                encrypted_username=_text(row["encrypted_username"]),
                encrypted_password=_text(row["encrypted_password"]),
                health_status=_text(row["health_status"]),
            )
            for row in rows
        )

    def mark_inventory_health(self, proxy_id: UUID, healthy: bool) -> bool:
        """Record an operator health result for one unbound spare lease.

        Restricted to unbound inventory so this can never overwrite the health
        of a proxy an account is running on.
        """

        with self.pool.connection() as connection:
            updated = connection.execute(
                """
                UPDATE proxies proxy
                SET health_status = %(health_status)s,
                    last_checked_at = now()
                WHERE proxy.id = %(proxy_id)s
                  AND proxy.credential_scope = 'inventory'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM proxy_bindings binding
                      WHERE binding.proxy_id = proxy.id
                        AND binding.released_at IS NULL
                  )
                RETURNING proxy.id
                """,
                {
                    "health_status": "active" if healthy else "degraded",
                    "proxy_id": proxy_id,
                },
            ).fetchone()
        return updated is not None

    def confirm_order(
        self,
        provider_order_id: str,
        endpoints: Sequence[ProxyEndpoint],
        encryption_key: str,
    ) -> None:
        """Atomically import confirmed credentials, then expose the order."""

        if not provider_order_id.isdecimal() or not endpoints:
            raise ProxyCapacityRepositoryError("confirmed order inventory is invalid")
        with self.pool.connection() as connection:
            order = connection.execute(
                """
                SELECT id, country, region, city, quantity, status
                FROM proxy_capacity_orders
                WHERE provider_order_id = %(provider_order_id)s
                FOR UPDATE
                """,
                {"provider_order_id": provider_order_id},
            ).fetchone()
            if order is None or order["status"] not in {"submitted", "confirmed"}:
                raise ProxyCapacityRepositoryError("confirmed order state is invalid")
            geo = Geo(order["country"], order["region"], order["city"])
            if len(endpoints) != order["quantity"]:
                raise ProxyCapacityRepositoryError(
                    "confirmed order inventory is invalid"
                )
            for endpoint in endpoints:
                self._import_endpoint(
                    connection,
                    endpoint=endpoint,
                    expected_geo=geo,
                    encryption_key=encryption_key,
                )
            connection.execute(
                """
                UPDATE proxy_capacity_orders
                SET status = 'confirmed', provider_status = 'confirmed',
                    updated_at = now()
                WHERE id = %(intent_id)s
                """,
                {"intent_id": order["id"]},
            )

    def confirm_order_segment(
        self,
        provider_order_id: str,
        geo: Geo,
        endpoints: Sequence[ProxyEndpoint],
        encryption_key: str,
    ) -> None:
        """Import one exact-geography segment from a mixed provider order."""

        if not provider_order_id.isdecimal() or not endpoints:
            raise ProxyCapacityRepositoryError("confirmed order inventory is invalid")
        with self.pool.connection() as connection:
            order = connection.execute(
                """
                SELECT id, quantity, status
                FROM proxy_capacity_orders
                WHERE provider_order_id = %(provider_order_id)s
                  AND country = %(country)s
                  AND region = %(region)s
                  AND city = %(city)s
                FOR UPDATE
                """,
                {
                    "provider_order_id": provider_order_id,
                    "country": geo.country,
                    "region": geo.region,
                    "city": geo.city,
                },
            ).fetchone()
            if (
                order is None
                or order["status"] not in {"submitted", "confirmed"}
                or len(endpoints) != order["quantity"]
            ):
                raise ProxyCapacityRepositoryError(
                    "confirmed order inventory is invalid"
                )
            for endpoint in endpoints:
                self._import_endpoint(
                    connection,
                    endpoint=endpoint,
                    expected_geo=geo,
                    encryption_key=encryption_key,
                )
            connection.execute(
                """
                UPDATE proxy_capacity_orders
                SET status = 'confirmed', provider_status = 'confirmed',
                    updated_at = now()
                WHERE id = %(intent_id)s
                """,
                {"intent_id": order["id"]},
            )

    @staticmethod
    def _intent_uuid(intent_id: str) -> UUID:
        try:
            return UUID(intent_id)
        except (TypeError, ValueError):
            raise ProxyCapacityRepositoryError("capacity intent is invalid") from None

    @staticmethod
    def _import_endpoint(
        connection: Connection,
        *,
        endpoint: ProxyEndpoint,
        expected_geo: Geo,
        encryption_key: str,
    ) -> None:
        if (
            endpoint.provider != "iproyal"
            or endpoint.proxy_type != "isp_static_residential"
            or endpoint.geo != expected_geo
        ):
            raise ProxyCapacityRepositoryError("confirmed order inventory is invalid")
        current = connection.execute(
            """
            SELECT proxy.id, proxy.provider, proxy.proxy_type,
                   proxy.country, proxy.region, proxy.city,
                   proxy.host, proxy.port, proxy.credential_scope,
                   proxy.encrypted_username, proxy.encrypted_password,
                   (
                       SELECT binding.ig_account_id
                       FROM proxy_bindings binding
                       WHERE binding.proxy_id = proxy.id
                         AND binding.released_at IS NULL
                       ORDER BY binding.id
                       LIMIT 1
                   ) AS bound_account_id
            FROM proxies proxy
            WHERE proxy.provider = %(provider)s
              AND (
                  proxy.provider_reference = %(reference)s
                  OR (proxy.host = %(host)s AND proxy.port = %(port)s)
              )
            ORDER BY (proxy.provider_reference = %(reference)s) DESC,
                     proxy.created_at, proxy.id
            LIMIT 1
            FOR UPDATE OF proxy
            """,
            {
                "provider": endpoint.provider,
                "reference": endpoint.provider_reference,
                "host": endpoint.host,
                "port": endpoint.port,
            },
        ).fetchone()
        metadata = (
            endpoint.provider,
            endpoint.proxy_type,
            endpoint.country,
            endpoint.region,
            endpoint.city,
            endpoint.host,
            endpoint.port,
        )
        if current is not None:
            stored = (
                current["provider"],
                current["proxy_type"],
                current["country"],
                current["region"],
                current["city"],
                current["host"],
                current["port"],
            )
            scope = current["credential_scope"]
            cipher_subject = (
                current["id"] if scope == "inventory" else current["bound_account_id"]
            )
            try:
                cipher = SessionCipher(encryption_key, cipher_subject)
                username = cipher.decrypt(current["encrypted_username"])["value"]
                password = cipher.decrypt(current["encrypted_password"])["value"]
            except (KeyError, TypeError, ValueError, SessionCipherError):
                raise ProxyCapacityRepositoryError(
                    "confirmed order inventory is invalid"
                ) from None
            if (
                stored != metadata
                or scope not in {"inventory", "account"}
                or cipher_subject is None
                or username != endpoint.username
                or password != endpoint.password
            ):
                raise ProxyCapacityRepositoryError(
                    "confirmed order inventory is invalid"
                )
            return

        proxy_id = uuid4()
        cipher = SessionCipher(encryption_key, proxy_id)
        connection.execute(
            """
            INSERT INTO proxies (
                id, provider, provider_reference, proxy_type,
                country, region, city, host, port,
                encrypted_username, encrypted_password, credential_scope
            )
            VALUES (
                %(id)s, %(provider)s, %(reference)s, %(proxy_type)s,
                %(country)s, %(region)s, %(city)s, %(host)s, %(port)s,
                %(username)s, %(password)s, 'inventory'
            )
            """,
            {
                "id": proxy_id,
                "provider": endpoint.provider,
                "reference": endpoint.provider_reference,
                "proxy_type": endpoint.proxy_type,
                "country": endpoint.country,
                "region": endpoint.region,
                "city": endpoint.city,
                "host": endpoint.host,
                "port": endpoint.port,
                "username": cipher.encrypt({"value": endpoint.username}),
                "password": cipher.encrypt({"value": endpoint.password}),
            },
        )

    @staticmethod
    def _get_snapshot(connection: Connection, geo: Geo) -> CapacitySnapshot:
        row = connection.execute(
            """
            SELECT
                (
                    -- Owner accounts determine demand only in explicitly enabled managed mode.
                    -- The join excludes orphaned account rows.
                    SELECT count(*)
                    FROM ig_accounts account
                    JOIN users owner ON owner.id = account.user_id
                    WHERE account.login_country = %(country)s
                      AND account.login_region = %(region)s
                      AND account.login_city = %(city)s

                ) AS connected_accounts,
                -- Retain the planner's compatibility counters; owner installs have no trials.
                0::bigint AS trial_accounts,
                0::bigint AS trial_leases_in_use,
                (
                    SELECT count(*)
                    FROM proxies proxy
                    WHERE proxy.provider = 'iproyal'
                      AND proxy.proxy_type = 'isp_static_residential'
                      AND proxy.country = %(country)s
                      AND proxy.region = %(region)s
                      AND proxy.city = %(city)s
                      AND proxy.health_status = 'active'
                      AND (
                          proxy.credential_scope = 'inventory'
                          OR EXISTS (
                              SELECT 1
                              FROM proxy_bindings binding
                              WHERE binding.proxy_id = proxy.id
                                AND binding.released_at IS NULL
                          )
                      )
                ) AS confirmed_proxies,
                COALESCE(
                    sum(quantity) FILTER (
                        WHERE status IN ('submitting', 'submitted', 'unknown')
                    ),
                    0
                ) AS pending_proxies,
                (
                    -- Every geography, deliberately. Scoped to one city this
                    -- made `max_monthly_cost_cents` a per-city allowance, so
                    -- seven supported geographies turned a $200 ceiling into
                    -- $1,400 of real exposure. `trial_leases_in_use` above
                    -- already bounds itself globally for the same reason: the
                    -- ceiling is global because the money is.
                    SELECT COALESCE(
                        sum(quoted_cost_cents) FILTER (
                            WHERE status <> 'failed'
                              AND created_at >= date_trunc('month', now())
                        ),
                        0
                    )
                    FROM proxy_capacity_orders
                ) AS monthly_spend_cents
            FROM proxy_capacity_orders
            WHERE country = %(country)s
              AND region = %(region)s
              AND city = %(city)s
            """,
            {
                "country": geo.country,
                "region": geo.region,
                "city": geo.city,
            },
        ).fetchone()
        return CapacitySnapshot(
            connected_accounts=int(row["connected_accounts"]),
            trial_accounts=int(row["trial_accounts"]),
            trial_leases_in_use=int(row["trial_leases_in_use"]),
            confirmed_proxies=int(row["confirmed_proxies"]),
            pending_proxies=int(row["pending_proxies"]),
            monthly_spend_cents=int(row["monthly_spend_cents"]),
        )
