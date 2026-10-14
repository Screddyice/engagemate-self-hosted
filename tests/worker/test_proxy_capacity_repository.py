from __future__ import annotations

import base64
import os
import uuid

import psycopg
import pytest
from engagemate.db.pool import create_pool
from engagemate.db.proxy_capacity import PostgresCapacityRepository
from engagemate.proxy.base import Geo, ProxyEndpoint
from engagemate.proxy.capacity import (
    CapacityPolicy,
    GeoCatalogEntry,
    SubmittedCapacityOrder,
)
from engagemate.security.session_cipher import SessionCipher

SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan text NOT NULL DEFAULT 'starter',
    subscription_status text
);
CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    login_country text,
    login_region text,
    login_city text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE proxy_capacity_orders (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    country text NOT NULL,
    region text NOT NULL,
    city text NOT NULL,
    product_id integer NOT NULL,
    plan_id integer NOT NULL,
    location_id integer NOT NULL,
    quantity integer NOT NULL,
    quoted_cost_cents integer NOT NULL,
    status text NOT NULL,
    provider_order_id text,
    provider_status text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE proxies (
    id uuid PRIMARY KEY,
    provider text NOT NULL,
    provider_reference text NOT NULL,
    proxy_type text NOT NULL,
    country text NOT NULL,
    region text,
    city text,
    host text NOT NULL,
    port integer NOT NULL,
    encrypted_username text NOT NULL,
    encrypted_password text NOT NULL,
    credential_scope text NOT NULL,
    health_status text NOT NULL DEFAULT 'active',
    last_checked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, provider_reference)
);
CREATE UNIQUE INDEX proxy_capacity_orders_provider_order_geo_idx
ON proxy_capacity_orders (provider_order_id, country, region, city)
WHERE provider_order_id IS NOT NULL;
CREATE TABLE proxy_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    proxy_id uuid NOT NULL,
    released_at timestamptz
);
"""


@pytest.fixture()
def pool():
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is unavailable")
    schema = f"capacity_{uuid.uuid4().hex}"
    scoped_url = f"{database_url}?options=-csearch_path%3D{schema}"
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema}"')
        connection.execute(f'SET search_path TO "{schema}"')
        connection.execute(SCHEMA_SQL)
    pool = create_pool(scoped_url, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        pool.close()
        with psycopg.connect(database_url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


def _paying_owner(connection) -> uuid.UUID:
    """Create an owner whose subscription is active, and return their id.

    Demand counts only accounts owned by a paying subscriber, so a capacity
    test that wants an account to create demand has to give it an owner.
    """

    owner = uuid.uuid4()
    connection.execute(
        "INSERT INTO users (id, subscription_status) VALUES (%s, 'active')",
        (owner,),
    )
    return owner



def test_repository_counts_accounts_supply_pending_and_monthly_spend(pool) -> None:
    geo = Geo("US", "CA", "San Francisco")
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO ig_accounts (
                user_id, login_country, login_region, login_city
            )
            VALUES
                (%s, 'US', 'CA', 'San Francisco'),
                (%s, 'US', 'CA', 'San Francisco'),
                (%s, 'IN', 'MH', 'Mumbai')
            """,
            (
                _paying_owner(connection),
                _paying_owner(connection),
                _paying_owner(connection),
            ),
        )
        connection.execute(
            """
            INSERT INTO proxy_capacity_orders (
                country, region, city, product_id, plan_id, location_id,
                quantity, quoted_cost_cents, status
            )
            VALUES
                ('US', 'CA', 'San Francisco', 9, 4, 51, 3, 900, 'confirmed'),
                ('US', 'CA', 'San Francisco', 9, 4, 51, 2, 600, 'submitted'),
                ('US', 'CA', 'San Francisco', 9, 4, 51, 1, 300, 'unknown'),
                ('US', 'CA', 'San Francisco', 9, 4, 51, 7, 2100, 'failed')
            """
        )
        for offset in range(3):
            connection.execute(
                """
                INSERT INTO proxies (
                    id, provider, provider_reference, proxy_type,
                    country, region, city, host, port,
                    encrypted_username, encrypted_password, credential_scope
                ) VALUES (
                    %s, 'iproyal', %s, 'isp_static_residential',
                    'US', 'CA', 'San Francisco', %s, 12323,
                    'encrypted', 'encrypted', 'inventory'
                )
                """,
                (
                    uuid.uuid4(),
                    f"order:420:198.51.100.{offset + 1}",
                    f"198.51.100.{offset + 1}",
                ),
            )

    snapshot = PostgresCapacityRepository(pool).get_snapshot(geo)

    assert snapshot.connected_accounts == 2
    assert snapshot.confirmed_proxies == 3
    assert snapshot.pending_proxies == 3
    assert snapshot.monthly_spend_cents == 1_800


def test_repository_counts_monthly_spend_across_every_geography(pool) -> None:
    """The spend ceiling is global, because the money is.

    `monthly_spend_cents` used to be filtered to the geography being planned,
    which made `max_monthly_cost_cents` a per-city allowance: seven supported
    geographies turned a $200 ceiling into $1,400 of real exposure. Trials
    already bound their ceiling globally for exactly this reason. Spend now
    matches them.
    """

    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO proxy_capacity_orders (
                country, region, city, product_id, plan_id, location_id,
                quantity, quoted_cost_cents, status
            )
            VALUES
                ('US', 'CA', 'San Francisco', 9, 4, 51, 3, 900, 'confirmed'),
                ('IN', 'MH', 'Mumbai', 9, 4, 77, 2, 600, 'confirmed'),
                ('DK', '84', 'Copenhagen', 9, 4, 88, 1, 300, 'submitted'),
                ('DK', '84', 'Copenhagen', 9, 4, 88, 9, 9900, 'failed')
            """
        )

    snapshot = PostgresCapacityRepository(pool).get_snapshot(
        Geo("US", "CA", "San Francisco")
    )

    # 900 local + 600 Mumbai + 300 Copenhagen. The failed order never counts.
    assert snapshot.monthly_spend_cents == 1_800
    # Supply stays local: another city's leases cannot cover this one.
    assert snapshot.confirmed_proxies == 0
    assert snapshot.pending_proxies == 0


def test_repository_reserves_once_after_rechecking_limits(pool) -> None:
    geo = Geo("US", "CA", "San Francisco")
    entry = GeoCatalogEntry(product_id=9, plan_id=4, location_id=51)
    policy = CapacityPolicy(
        spare_per_geo=1,
        max_order_quantity=5,
        max_order_cost_cents=2_000,
        max_monthly_cost_cents=5_000,
    )
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO ig_accounts (
                user_id, login_country, login_region, login_city
            )
            VALUES
                (%s, 'US', 'CA', 'San Francisco'),
                (%s, 'US', 'CA', 'San Francisco')
            """,
            (_paying_owner(connection), _paying_owner(connection)),
        )

    repository = PostgresCapacityRepository(pool)
    intent = repository.reserve_submission(
        geo, entry, policy, quantity=3, quoted_cost_cents=900
    )
    duplicate = repository.reserve_submission(
        geo, entry, policy, quantity=3, quoted_cost_cents=900
    )

    assert intent is not None
    assert intent.quantity == 3
    assert duplicate is None
    repository.mark_submitted(intent.intent_id, "420", "in-progress")
    pending = repository.list_submitted_orders()
    assert pending == (
        SubmittedCapacityOrder(
            intent_id=intent.intent_id,
            provider_order_id="420",
            geo=geo,
        ),
    )
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT status, provider_order_id, provider_status "
            "FROM proxy_capacity_orders"
        ).fetchone()
    assert row == {
        "status": "submitted",
        "provider_order_id": "420",
        "provider_status": "in-progress",
    }


def test_synchronously_confirmed_purchase_stays_pending_until_inventory_import(
    pool,
) -> None:
    geo = Geo("US", "CA", "San Francisco")
    entry = GeoCatalogEntry(product_id=9, plan_id=4, location_id=51)
    policy = CapacityPolicy(1, 5, 2_000, 5_000)
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO ig_accounts (
                user_id, login_country, login_region, login_city
            ) VALUES (%s, 'US', 'CA', 'San Francisco')
            """,
            (_paying_owner(connection),),
        )
    repository = PostgresCapacityRepository(pool)
    intent = repository.reserve_submission(
        geo, entry, policy, quantity=2, quoted_cost_cents=600
    )
    assert intent is not None

    repository.mark_submitted(intent.intent_id, "420", "confirmed")

    assert repository.list_submitted_orders() == (
        SubmittedCapacityOrder(intent.intent_id, "420", geo),
    )


def test_repository_imports_confirmed_order_as_encrypted_unbound_inventory(
    pool,
) -> None:
    geo = Geo("US", "CA", "San Francisco")
    entry = GeoCatalogEntry(product_id=9, plan_id=4, location_id=51)
    policy = CapacityPolicy(
        spare_per_geo=1,
        max_order_quantity=5,
        max_order_cost_cents=2_000,
        max_monthly_cost_cents=5_000,
    )
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO ig_accounts (
                user_id, login_country, login_region, login_city
            ) VALUES (%s, 'US', 'CA', 'San Francisco')
            """,
            (_paying_owner(connection),),
        )
    repository = PostgresCapacityRepository(pool)
    intent = repository.reserve_submission(
        geo, entry, policy, quantity=2, quoted_cost_cents=600
    )
    assert intent is not None
    repository.mark_submitted(intent.intent_id, "420", "in-progress")
    endpoints = (
        ProxyEndpoint(
            provider="iproyal",
            provider_reference="order:420:198.51.100.248",
            proxy_type="isp_static_residential",
            geo=geo,
            host="198.51.100.248",
            port=12323,
            username="proxy-user-secret",
            password="proxy-password-secret",
        ),
        ProxyEndpoint(
            provider="iproyal",
            provider_reference="order:420:198.51.100.249",
            proxy_type="isp_static_residential",
            geo=geo,
            host="198.51.100.249",
            port=12323,
            username="proxy-user-secret",
            password="proxy-password-secret",
        ),
    )
    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")

    repository.confirm_order("420", endpoints, key)

    with pool.connection() as connection:
        order = connection.execute(
            "SELECT status, provider_status FROM proxy_capacity_orders"
        ).fetchone()
        inventory = connection.execute(
            """
            SELECT id, encrypted_username, encrypted_password, credential_scope
            FROM proxies ORDER BY host
            """
        ).fetchall()
    assert order == {"status": "confirmed", "provider_status": "confirmed"}
    assert len(inventory) == 2
    assert all(row["credential_scope"] == "inventory" for row in inventory)
    first = inventory[0]
    assert SessionCipher(key, first["id"]).decrypt(first["encrypted_username"]) == {
        "value": "proxy-user-secret"
    }
    assert SessionCipher(key, first["id"]).decrypt(first["encrypted_password"]) == {
        "value": "proxy-password-secret"
    }


def test_repository_registers_existing_order_idempotently(pool) -> None:
    geo = Geo("US", "CA", "San Francisco")
    entry = GeoCatalogEntry(product_id=9, plan_id=4, location_id=51)
    repository = PostgresCapacityRepository(pool)

    repository.register_existing_order(
        provider_order_id="420",
        geo=geo,
        entry=entry,
        quantity=2,
        paid_cost_cents=600,
    )
    repository.register_existing_order(
        provider_order_id="420",
        geo=geo,
        entry=entry,
        quantity=2,
        paid_cost_cents=600,
    )

    assert repository.list_submitted_orders() == (
        SubmittedCapacityOrder(
            intent_id=str(
                next(
                    row["id"]
                    for row in _capacity_orders(pool)
                    if row["provider_order_id"] == "420"
                )
            ),
            provider_order_id="420",
            geo=geo,
        ),
    )
    assert len(_capacity_orders(pool)) == 1


def test_repository_imports_one_provider_order_across_exact_geo_segments(pool) -> None:
    repository = PostgresCapacityRepository(pool)
    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    segments = (
        (
            Geo("US", "NY", "New York"),
            GeoCatalogEntry(product_id=9, plan_id=22, location_id=16),
            ProxyEndpoint(
                provider="iproyal",
                provider_reference="order:10000001:192.0.2.10",
                proxy_type="isp_static_residential",
                geo=Geo("US", "NY", "New York"),
                host="192.0.2.10",
                port=12323,
                username="us-user",
                password="us-password",
            ),
        ),
        (
            Geo("DK", "84", "Copenhagen"),
            GeoCatalogEntry(product_id=9, plan_id=22, location_id=26),
            ProxyEndpoint(
                provider="iproyal",
                provider_reference="order:10000001:192.0.2.20",
                proxy_type="isp_static_residential",
                geo=Geo("DK", "84", "Copenhagen"),
                host="192.0.2.20",
                port=12323,
                username="dk-user",
                password="dk-password",
            ),
        ),
    )

    for geo, entry, endpoint in segments:
        repository.register_existing_order(
            provider_order_id="10000001",
            geo=geo,
            entry=entry,
            quantity=1,
            paid_cost_cents=323,
        )
        repository.confirm_order_segment("10000001", geo, (endpoint,), key)

    orders = _capacity_orders(pool)
    assert {(row["country"], row["region"], row["city"]) for row in orders} == {
        ("US", "NY", "New York"),
        ("DK", "84", "Copenhagen"),
    }
    assert all(row["status"] == "confirmed" for row in orders)
    with pool.connection() as connection:
        inventory = connection.execute(
            "SELECT country, region, city FROM proxies ORDER BY country"
        ).fetchall()
    assert inventory == [
        {"country": "DK", "region": "84", "city": "Copenhagen"},
        {"country": "US", "region": "NY", "city": "New York"},
    ]


def test_confirmed_pilot_order_reuses_same_host_already_bound_in_import_mode(
    pool,
) -> None:
    geo = Geo("US", "CA", "San Francisco")
    entry = GeoCatalogEntry(product_id=9, plan_id=4, location_id=51)
    user_id = uuid.uuid4()
    account_id = uuid.uuid4()
    proxy_id = uuid.uuid4()
    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    cipher = SessionCipher(key, account_id)
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO ig_accounts (
                id, user_id, login_country, login_region, login_city
            ) VALUES (%s, %s, 'US', 'CA', 'San Francisco')
            """,
            (account_id, user_id),
        )
        connection.execute(
            """
            INSERT INTO proxies (
                id, provider, provider_reference, proxy_type,
                country, region, city, host, port,
                encrypted_username, encrypted_password, credential_scope
            ) VALUES (
                %s, 'iproyal', 'manual-pilot-reference',
                'isp_static_residential', 'US', 'CA', 'San Francisco',
                '198.51.100.248', 12323, %s, %s, 'account'
            )
            """,
            (
                proxy_id,
                cipher.encrypt({"value": "proxy-user-secret"}),
                cipher.encrypt({"value": "proxy-password-secret"}),
            ),
        )
        connection.execute(
            """
            INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id)
            VALUES (%s, %s, %s)
            """,
            (user_id, account_id, proxy_id),
        )
    repository = PostgresCapacityRepository(pool)
    repository.register_existing_order(
        provider_order_id="420",
        geo=geo,
        entry=entry,
        quantity=1,
        paid_cost_cents=300,
    )

    repository.confirm_order(
        "420",
        (
            ProxyEndpoint(
                provider="iproyal",
                provider_reference="order:420:198.51.100.248",
                proxy_type="isp_static_residential",
                geo=geo,
                host="198.51.100.248",
                port=12323,
                username="proxy-user-secret",
                password="proxy-password-secret",
            ),
        ),
        key,
    )

    with pool.connection() as connection:
        proxies = connection.execute(
            "SELECT id, provider_reference, credential_scope FROM proxies"
        ).fetchall()
        order = connection.execute(
            "SELECT status FROM proxy_capacity_orders"
        ).fetchone()
    assert proxies == [
        {
            "id": proxy_id,
            "provider_reference": "manual-pilot-reference",
            "credential_scope": "account",
        }
    ]
    assert order == {"status": "confirmed"}


def test_repository_lists_accounts_without_a_healthy_active_binding(pool) -> None:
    user_id = uuid.uuid4()
    with pool.connection() as connection:
        rows = connection.execute(
            """
            INSERT INTO ig_accounts (
                user_id, login_country, login_region, login_city
            ) VALUES
                (%s, 'US', 'CA', 'San Francisco'),
                (%s, 'US', 'CA', 'San Francisco'),
                (%s, 'US', 'CA', 'San Francisco')
            RETURNING id
            """,
            (user_id, user_id, user_id),
        ).fetchall()
        active_proxy = connection.execute(
            """
            INSERT INTO proxies (
                id, provider, provider_reference, proxy_type,
                country, region, city, host, port,
                encrypted_username, encrypted_password, credential_scope
            ) VALUES (
                %s, 'iproyal', 'active-ref', 'isp_static_residential',
                'US', 'CA', 'San Francisco', '198.51.100.1', 12323,
                'encrypted', 'encrypted', 'account'
            ) RETURNING id
            """,
            (_paying_owner(connection),),
        ).fetchone()["id"]
        degraded_proxy = connection.execute(
            """
            INSERT INTO proxies (
                id, provider, provider_reference, proxy_type,
                country, region, city, host, port,
                encrypted_username, encrypted_password, credential_scope,
                health_status
            ) VALUES (
                %s, 'iproyal', 'degraded-ref', 'isp_static_residential',
                'US', 'CA', 'San Francisco', '198.51.100.2', 12323,
                'encrypted', 'encrypted', 'account', 'degraded'
            ) RETURNING id
            """,
            (_paying_owner(connection),),
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id) VALUES
                (%s, %s, %s),
                (%s, %s, %s)
            """,
            (
                user_id,
                rows[0]["id"],
                active_proxy,
                user_id,
                rows[1]["id"],
                degraded_proxy,
            ),
        )

    accounts = PostgresCapacityRepository(pool).list_accounts_needing_binding()

    assert {account.account_id for account in accounts} == {
        rows[1]["id"],
        rows[2]["id"],
    }
    assert all(account.geo == Geo("US", "CA", "San Francisco") for account in accounts)


def test_repository_rejects_conflicting_existing_order_registration(pool) -> None:
    repository = PostgresCapacityRepository(pool)
    repository.register_existing_order(
        provider_order_id="420",
        geo=Geo("US", "CA", "San Francisco"),
        entry=GeoCatalogEntry(product_id=9, plan_id=4, location_id=51),
        quantity=2,
        paid_cost_cents=600,
    )

    with pytest.raises(Exception, match="existing order registration conflicts"):
        repository.register_existing_order(
            provider_order_id="420",
            geo=Geo("US", "CA", "San Francisco"),
            entry=GeoCatalogEntry(product_id=9, plan_id=4, location_id=52),
            quantity=2,
            paid_cost_cents=600,
        )


def test_repository_marks_terminal_provider_order_failed(pool) -> None:
    geo = Geo("US", "CA", "San Francisco")
    entry = GeoCatalogEntry(product_id=9, plan_id=4, location_id=51)
    policy = CapacityPolicy(
        spare_per_geo=1,
        max_order_quantity=5,
        max_order_cost_cents=2_000,
        max_monthly_cost_cents=5_000,
    )
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO ig_accounts (
                user_id, login_country, login_region, login_city
            ) VALUES (%s, 'US', 'CA', 'San Francisco')
            """,
            (_paying_owner(connection),),
        )
    repository = PostgresCapacityRepository(pool)
    intent = repository.reserve_submission(
        geo, entry, policy, quantity=2, quoted_cost_cents=600
    )
    assert intent is not None
    repository.mark_submitted(intent.intent_id, "421", "in-progress")

    repository.mark_provider_failed("421", "refunded")

    assert repository.list_submitted_orders() == ()
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT status, provider_status FROM proxy_capacity_orders"
        ).fetchone()
    assert row == {"status": "failed", "provider_status": "refunded"}


def _capacity_orders(pool):
    with pool.connection() as connection:
        return connection.execute(
            "SELECT id, provider_order_id, country, region, city, status "
            "FROM proxy_capacity_orders ORDER BY id"
        ).fetchall()


def _insert_proxy(
    connection,
    *,
    host: str,
    geo: Geo,
    scope: str = "inventory",
    health: str = "active",
) -> uuid.UUID:
    proxy_id = uuid.uuid4()
    connection.execute(
        """
        INSERT INTO proxies (
            id, provider, provider_reference, proxy_type,
            country, region, city, host, port,
            encrypted_username, encrypted_password,
            credential_scope, health_status
        ) VALUES (
            %s, 'iproyal', %s, 'isp_static_residential',
            %s, %s, %s, %s, 12323,
            'encrypted-username', 'encrypted-password', %s, %s
        )
        """,
        (
            proxy_id,
            f"order:420:{host}",
            geo.country,
            geo.region,
            geo.city,
            host,
            scope,
            health,
        ),
    )
    return proxy_id


def test_unbound_inventory_excludes_bound_and_account_scoped_leases(pool) -> None:
    """Only spares nobody is running on may be checked out of band."""

    phoenix = Geo("US", "AZ", "Phoenix")
    with pool.connection() as connection:
        free = _insert_proxy(connection, host="203.0.113.102", geo=phoenix)
        bound = _insert_proxy(connection, host="203.0.113.109", geo=phoenix)
        released = _insert_proxy(connection, host="203.0.113.150", geo=phoenix)
        _insert_proxy(
            connection, host="203.0.113.221", geo=phoenix, scope="account"
        )
        connection.execute(
            "INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id) "
            "VALUES (%s, %s, %s)",
            (uuid.uuid4(), uuid.uuid4(), bound),
        )
        connection.execute(
            "INSERT INTO proxy_bindings "
            "(user_id, ig_account_id, proxy_id, released_at) "
            "VALUES (%s, %s, %s, now())",
            (uuid.uuid4(), uuid.uuid4(), released),
        )

    inventory = PostgresCapacityRepository(pool).list_unbound_inventory()

    # A released binding frees the lease again, so it comes back into the pool.
    assert {proxy.id for proxy in inventory} == {free, released}
    assert {proxy.geo for proxy in inventory} == {phoenix}
    assert all(proxy.health_status == "active" for proxy in inventory)


def test_marking_health_records_the_result_and_the_check_time(pool) -> None:
    with pool.connection() as connection:
        proxy_id = _insert_proxy(
            connection, host="203.0.113.94", geo=Geo("US", "DC", "Washington")
        )

    repository = PostgresCapacityRepository(pool)

    assert repository.mark_inventory_health(proxy_id, False) is True
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT health_status, last_checked_at FROM proxies WHERE id = %s",
            (proxy_id,),
        ).fetchone()
    assert row["health_status"] == "degraded"
    assert row["last_checked_at"] is not None

    assert repository.mark_inventory_health(proxy_id, True) is True
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT health_status FROM proxies WHERE id = %s", (proxy_id,)
        ).fetchone()
    assert row["health_status"] == "active"


def test_marking_health_refuses_a_lease_an_account_is_running_on(pool) -> None:
    """A run owns its proxy's health; this command must not overwrite it."""

    with pool.connection() as connection:
        bound = _insert_proxy(
            connection, host="203.0.113.102", geo=Geo("US", "AZ", "Phoenix")
        )
        connection.execute(
            "INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id) "
            "VALUES (%s, %s, %s)",
            (uuid.uuid4(), uuid.uuid4(), bound),
        )
        account_scoped = _insert_proxy(
            connection,
            host="203.0.113.150",
            geo=Geo("US", "AL", "Centre"),
            scope="account",
        )

    repository = PostgresCapacityRepository(pool)

    assert repository.mark_inventory_health(bound, False) is False
    assert repository.mark_inventory_health(account_scoped, False) is False
    assert repository.mark_inventory_health(uuid.uuid4(), False) is False
    with pool.connection() as connection:
        rows = connection.execute(
            "SELECT health_status, last_checked_at FROM proxies "
            "WHERE id = ANY(%s)",
            ([bound, account_scoped],),
        ).fetchall()
    assert all(row["health_status"] == "active" for row in rows)
    assert all(row["last_checked_at"] is None for row in rows)


def _owner(connection, status: str | None) -> uuid.UUID:
    owner = uuid.uuid4()
    connection.execute(
        "INSERT INTO users (id, subscription_status) VALUES (%s, %s)",
        (owner, status),
    )
    return owner


def _account(connection, owner: uuid.UUID, geo: Geo) -> None:
    connection.execute(
        """
        INSERT INTO ig_accounts (user_id, login_country, login_region, login_city)
        VALUES (%s, %s, %s, %s)
        """,
        (owner, geo.country, geo.region, geo.city),
    )


def test_demand_counts_only_accounts_whose_owner_is_paying(pool) -> None:
    """A seven-day trial must never trigger a recurring lease purchase.

    A lease bills monthly. Buying one the moment someone starts a trial means
    paying for people who may never convert, so demand follows conversion and
    trials are served from spares instead.
    """

    geo = Geo("US", "AZ", "Phoenix")
    with pool.connection() as connection:
        _account(connection, _owner(connection, "active"), geo)
        _account(connection, _owner(connection, "trialing"), geo)
        _account(connection, _owner(connection, None), geo)
        _account(connection, _owner(connection, "canceled"), geo)

    snapshot = PostgresCapacityRepository(pool).get_snapshot(geo)

    assert snapshot.connected_accounts == 1


def test_demand_ignores_paying_accounts_in_another_geography(pool) -> None:
    with pool.connection() as connection:
        owner = _owner(connection, "active")
        _account(connection, owner, Geo("US", "AZ", "Phoenix"))
        _account(connection, owner, Geo("DK", "84", "Copenhagen"))

    snapshot = PostgresCapacityRepository(pool).get_snapshot(
        Geo("US", "AZ", "Phoenix")
    )

    assert snapshot.connected_accounts == 1


def test_an_account_with_no_owner_row_creates_no_demand(pool) -> None:
    """Orphaned rows must not buy proxies."""

    geo = Geo("US", "AL", "Centre")
    with pool.connection() as connection:
        _account(connection, uuid.uuid4(), geo)

    assert PostgresCapacityRepository(pool).get_snapshot(geo).connected_accounts == 0


def _trialing_owner(connection) -> uuid.UUID:
    owner = uuid.uuid4()
    connection.execute(
        "INSERT INTO users (id, subscription_status) VALUES (%s, 'trialing')",
        (owner,),
    )
    return owner


def test_the_snapshot_counts_trial_accounts_separately(pool) -> None:
    geo = Geo("US", "AZ", "Phoenix")
    with pool.connection() as connection:
        _account(connection, _paying_owner(connection), geo)
        _account(connection, _trialing_owner(connection), geo)
        _account(connection, _trialing_owner(connection), geo)

    snapshot = PostgresCapacityRepository(pool).get_snapshot(geo)

    assert snapshot.connected_accounts == 1
    assert snapshot.trial_accounts == 2


def test_leases_serving_trials_are_counted_across_every_geography(pool) -> None:
    """The ceiling is global, because the money is.

    Attribution is derived from the owner's subscription rather than stored,
    so a conversion frees a trial slot with nothing to clear and nothing to
    drift out of date.
    """

    phoenix = Geo("US", "AZ", "Phoenix")
    copenhagen = Geo("DK", "84", "Copenhagen")
    with pool.connection() as connection:
        for geo in (phoenix, copenhagen):
            owner = _trialing_owner(connection)
            account = connection.execute(
                """
                INSERT INTO ig_accounts (
                    user_id, login_country, login_region, login_city
                ) VALUES (%s, %s, %s, %s) RETURNING id
                """,
                (owner, geo.country, geo.region, geo.city),
            ).fetchone()["id"]
            proxy = _insert_proxy(
                connection, host=f"203.0.113.{abs(hash(geo.city)) % 200 + 1}", geo=geo
            )
            connection.execute(
                "INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id) "
                "VALUES (%s, %s, %s)",
                (owner, account, proxy),
            )

    snapshot = PostgresCapacityRepository(pool).get_snapshot(phoenix)

    assert snapshot.trial_leases_in_use == 2


def test_a_converted_owner_frees_their_trial_slot(pool) -> None:
    geo = Geo("US", "AZ", "Phoenix")
    with pool.connection() as connection:
        owner = _trialing_owner(connection)
        account = connection.execute(
            """
            INSERT INTO ig_accounts (user_id, login_country, login_region, login_city)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (owner, geo.country, geo.region, geo.city),
        ).fetchone()["id"]
        proxy = _insert_proxy(connection, host="203.0.113.55", geo=geo)
        connection.execute(
            "INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id) "
            "VALUES (%s, %s, %s)",
            (owner, account, proxy),
        )

    repository = PostgresCapacityRepository(pool)
    assert repository.get_snapshot(geo).trial_leases_in_use == 1

    with pool.connection() as connection:
        connection.execute(
            "UPDATE users SET subscription_status = 'active' WHERE id = %s", (owner,)
        )

    after = repository.get_snapshot(geo)
    assert after.trial_leases_in_use == 0
    assert after.connected_accounts == 1


def test_free_leases_by_geo_counts_only_assignable_inventory(pool) -> None:
    """The count login-geo selection trusts: active, inventory-scoped, unbound.

    Every excluded state is here so a regression that loosens one shows up as a
    wrong count rather than a proxy the binder cannot actually hand out.
    """

    from engagemate.db.proxies import ProxyRepository

    sf = Geo("US", "CA", "San Francisco")
    raleigh = Geo("US", "NC", "Raleigh")
    with pool.connection() as connection:
        # Two assignable SF leases.
        _insert_proxy(connection, host="203.0.113.1", geo=sf)
        _insert_proxy(connection, host="203.0.113.2", geo=sf)
        # One assignable Raleigh lease.
        _insert_proxy(connection, host="203.0.113.3", geo=raleigh)
        # Degraded: not assignable.
        _insert_proxy(connection, host="203.0.113.4", geo=sf, health="degraded")
        # Already claimed by an account (credential_scope='account'): not free.
        _insert_proxy(connection, host="203.0.113.5", geo=sf, scope="account")
        # Bound to a live account: not free even though inventory-scoped.
        owner = _paying_owner(connection)
        account = connection.execute(
            "INSERT INTO ig_accounts (user_id, login_country, login_region, "
            "login_city) VALUES (%s, %s, %s, %s) RETURNING id",
            (owner, sf.country, sf.region, sf.city),
        ).fetchone()["id"]
        bound = _insert_proxy(connection, host="203.0.113.6", geo=sf)
        connection.execute(
            "INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id) "
            "VALUES (%s, %s, %s)",
            (owner, account, bound),
        )

    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    free = ProxyRepository(pool, key).free_leases_by_geo()

    assert free == {sf: 2, raleigh: 1}
