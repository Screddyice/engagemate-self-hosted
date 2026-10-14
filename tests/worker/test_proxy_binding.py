from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import psycopg
import pytest
from pydantic import ValidationError

ROOT_DIR = Path(__file__).resolve().parents[2]
API_DIR = ROOT_DIR / "api"
sys.path.insert(0, str(API_DIR))

from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.db.proxies import (
    AccountUnavailableError,
    ProxyBindingError,
    ProxyBindingService,
    ProxyRepository,
    _text,
)
from engagemate.proxy.base import (
    Geo,
    ProxyEndpoint,
    ProxyHealth,
    ProxyHealthStatus,
)
from engagemate.proxy.iproyal import IPRoyalProvider
from engagemate.security.session_cipher import SessionCipher, SessionCipherError

SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY
);
CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    login_country text,
    login_region text,
    login_city text,
    health_status text NOT NULL DEFAULT 'active',
    UNIQUE (id, user_id)
);
CREATE TABLE proxies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
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
    credential_scope text NOT NULL DEFAULT 'account',
    health_status text NOT NULL DEFAULT 'active',
    last_checked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE proxy_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    proxy_id uuid NOT NULL REFERENCES proxies(id),
    bound_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz,
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id)
);
CREATE UNIQUE INDEX proxy_bindings_active_account_idx
    ON proxy_bindings (ig_account_id)
    WHERE released_at IS NULL;
CREATE UNIQUE INDEX proxy_bindings_active_proxy_idx
    ON proxy_bindings (proxy_id)
    WHERE released_at IS NULL;
CREATE TABLE account_health_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL,
    event_type text NOT NULL,
    severity text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (ig_account_id, user_id)
        REFERENCES ig_accounts(id, user_id)
);
CREATE TABLE jobs (
    id uuid PRIMARY KEY, user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL, status text NOT NULL,
    lease_expires_at timestamptz
);
"""


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


@pytest.fixture(scope="module")
def database_url() -> str:
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode:
        pytest.skip("local PostgreSQL is unavailable")
    database = f"engagemate_proxy_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    created = subprocess.run(["createdb", database], capture_output=True, text=True)
    if created.returncode:
        pytest.fail(
            f"could not create disposable PostgreSQL database: {created.stderr}"
        )
    url = f"postgresql://localhost/{database}"
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(SCHEMA_SQL)
        yield url
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", database],
            capture_output=True,
            check=False,
        )


@pytest.fixture()
def tenant(database_url: str):
    pool_a = create_pool(database_url, min_size=1, max_size=4)
    pool_b = create_pool(database_url, min_size=1, max_size=4)
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    account_a, account_b = uuid.uuid4(), uuid.uuid4()
    with pool_a.connection() as connection:
        connection.execute(
            "TRUNCATE jobs, account_health_events, proxy_bindings, proxies, "
            "ig_accounts, users CASCADE"
        )
        connection.execute(
            "INSERT INTO users (id) VALUES (%s), (%s)",
            (user_a, user_b),
        )
        connection.execute(
            """
            INSERT INTO ig_accounts (
                id, user_id, login_country, login_region, login_city
            )
            VALUES
                (%s, %s, 'IN', 'MH', 'Mumbai'),
                (%s, %s, 'US', 'NY', 'New York')
            """,
            (account_a, user_a, account_b, user_b),
        )
    try:
        yield pool_a, pool_b, user_a, user_b, account_a, account_b
    finally:
        pool_a.close()
        pool_b.close()


class FakeProvider:
    name = "iproyal"

    def __init__(
        self,
        endpoints: list[ProxyEndpoint],
        *,
        health: ProxyHealth | None = None,
    ) -> None:
        self._endpoints = tuple(endpoints)
        self._lock = threading.Lock()
        self.allocate_calls = 0
        self.check_calls = 0
        self.health = health or ProxyHealth.healthy()

    def allocate_static(
        self,
        geo: Geo,
        *,
        excluded_references: frozenset[str] = frozenset(),
    ) -> ProxyEndpoint:
        with self._lock:
            self.allocate_calls += 1
            for endpoint in self._endpoints:
                if (
                    endpoint.geo == geo
                    and endpoint.provider_reference not in excluded_references
                ):
                    return endpoint
        raise ValueError("fake inventory exhausted")

    def check(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        self.check_calls += 1
        return self.health


def _endpoint(
    reference: str = "isp-lease-1",
    *,
    geo: Geo | None = None,
    host: str = "203.0.113.10",
    username: str = "proxy-user-secret",
    password: str = "proxy-password-secret",
) -> ProxyEndpoint:
    return ProxyEndpoint(
        provider="iproyal",
        provider_reference=reference,
        proxy_type="isp_static_residential",
        geo=geo or Geo("IN", "MH", "Mumbai"),
        host=host,
        port=12345,
        username=username,
        password=password,
    )


def _service(pool, provider: FakeProvider, key: str) -> ProxyBindingService:
    return ProxyBindingService(ProxyRepository(pool, key), provider)


def test_sticky_binding_reuses_one_provider_allocation(tenant) -> None:
    pool, _, user_a, _, account_a, _ = tenant
    provider = FakeProvider([_endpoint()])
    service = _service(pool, provider, _key())
    geo = Geo("IN", "MH", "Mumbai")

    first = service.ensure_binding(user_a, account_a, geo)
    second = service.ensure_binding(user_a, account_a, geo)

    assert first.id == second.id
    assert first.country == "IN"
    assert provider.allocate_calls == 1


def test_binding_claims_encrypted_managed_inventory_before_provider(tenant) -> None:
    pool, _, user_a, _, account_a, _ = tenant
    provider = FakeProvider([])
    key = _key()
    proxy_id = uuid.uuid4()
    cipher = SessionCipher(key, proxy_id)
    with pool.connection() as connection:
        connection.execute(
            """
            INSERT INTO proxies (
                id, provider, provider_reference, proxy_type,
                country, region, city, host, port,
                encrypted_username, encrypted_password, credential_scope
            )
            VALUES (
                %(id)s, 'iproyal', 'order:420:203.0.113.99',
                'isp_static_residential', 'IN', 'MH', 'Mumbai',
                '203.0.113.99', 12323, %(username)s, %(password)s, 'inventory'
            )
            """,
            {
                "id": proxy_id,
                "username": cipher.encrypt({"value": "inventory-user-secret"}),
                "password": cipher.encrypt({"value": "inventory-pass-secret"}),
            },
        )

    endpoint = _service(pool, provider, key).ensure_binding(
        user_a, account_a, Geo("IN", "MH", "Mumbai")
    )

    assert endpoint.id == proxy_id
    assert endpoint.username == "inventory-user-secret"
    assert endpoint.password == "inventory-pass-secret"
    assert provider.allocate_calls == 0
    with pool.connection() as connection:
        row = connection.execute(
            """
            SELECT credential_scope, encrypted_username, encrypted_password
            FROM proxies WHERE id = %s
            """,
            (proxy_id,),
        ).fetchone()
    # A SQL_ASCII database returns text columns as bytes, which is why the
    # repository decodes with `_text` rather than comparing raw values.
    assert _text(row["credential_scope"]) == "account"
    account_cipher = SessionCipher(key, account_a)
    assert account_cipher.decrypt(_text(row["encrypted_username"])) == {
        "value": "inventory-user-secret"
    }
    assert account_cipher.decrypt(_text(row["encrypted_password"])) == {
        "value": "inventory-pass-secret"
    }


def test_concurrent_ensure_binding_serializes_per_account(tenant) -> None:
    pool_a, pool_b, user_a, _, account_a, _ = tenant
    provider = FakeProvider([_endpoint()])
    key = _key()
    service_a = _service(pool_a, provider, key)
    service_b = _service(pool_b, provider, key)
    barrier = threading.Barrier(2)

    def ensure(service: ProxyBindingService) -> ProxyEndpoint:
        barrier.wait()
        return service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(ensure, service_a)
        second = executor.submit(ensure, service_b)
        endpoints = [first.result(timeout=5), second.result(timeout=5)]

    assert endpoints[0].id == endpoints[1].id
    assert provider.allocate_calls == 1
    with pool_a.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM proxy_bindings WHERE released_at IS NULL"
            ).fetchone()["count"]
            == 1
        )


def test_cross_tenant_account_access_is_rejected_without_allocation(tenant) -> None:
    pool, _, _, user_b, account_a, _ = tenant
    provider = FakeProvider([_endpoint()])
    service = _service(pool, provider, _key())

    with pytest.raises(AccountUnavailableError, match="unavailable"):
        service.ensure_binding(user_b, account_a, Geo("IN", "MH", "Mumbai"))

    assert provider.allocate_calls == 0


def test_provider_allocation_traceback_suppresses_secret_cause(tenant) -> None:
    class LeakyProvider(FakeProvider):
        def allocate_static(
            self,
            geo: Geo,
            *,
            excluded_references: frozenset[str] = frozenset(),
        ) -> ProxyEndpoint:
            raise RuntimeError(
                "http://provider-user-secret:provider-password-secret@"
                "203.0.113.10:12345"
            )

    pool, _, user_a, _, account_a, _ = tenant
    service = _service(pool, LeakyProvider([]), _key())

    with pytest.raises(ProxyBindingError) as error:
        service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))

    formatted = "".join(
        traceback.format_exception(
            type(error.value), error.value, error.value.__traceback__
        )
    )
    assert "provider-user-secret" not in formatted
    assert "provider-password-secret" not in formatted
    assert "http://" not in formatted
    assert str(error.value) == "proxy inventory unavailable"


@pytest.mark.parametrize(
    ("requested", "provided"),
    [
        (Geo("US", "NY", "New York"), Geo("IN", "MH", "Mumbai")),
        (Geo("IN", "MH", "Mumbai"), Geo("IN", "KA", "Bengaluru")),
    ],
)
def test_declared_and_provider_geography_must_match_exactly(
    tenant, requested: Geo, provided: Geo
) -> None:
    class MisreportedProvider(FakeProvider):
        def allocate_static(
            self,
            geo: Geo,
            *,
            excluded_references: frozenset[str] = frozenset(),
        ) -> ProxyEndpoint:
            self.allocate_calls += 1
            return self._endpoints[0]

    pool, _, user_a, _, account_a, _ = tenant
    provider = MisreportedProvider([_endpoint(geo=provided)])
    service = _service(pool, provider, _key())

    with pytest.raises(ProxyBindingError, match="geography"):
        service.ensure_binding(user_a, account_a, requested)

    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM proxy_bindings"
            ).fetchone()["count"]
            == 0
        )


def test_one_active_proxy_cannot_be_shared_between_accounts(tenant) -> None:
    pool, _, user_a, user_b, account_a, account_b = tenant
    provider = FakeProvider(
        [
            _endpoint(),
            _endpoint(geo=Geo("US", "NY", "New York")),
        ]
    )
    service = _service(pool, provider, _key())
    service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))

    with pytest.raises(ProxyBindingError, match="inventory unavailable"):
        service.ensure_binding(user_b, account_b, Geo("US", "NY", "New York"))

    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM proxy_bindings WHERE released_at IS NULL"
            ).fetchone()["count"]
            == 1
        )


def test_credentials_are_encrypted_at_rest_and_bound_to_account(tenant) -> None:
    pool, _, user_a, _, account_a, account_b = tenant
    key = _key()
    endpoint = _service(pool, FakeProvider([_endpoint()]), key).ensure_binding(
        user_a, account_a, Geo("IN", "MH", "Mumbai")
    )

    with pool.connection() as connection:
        row = connection.execute(
            "SELECT encrypted_username, encrypted_password FROM proxies"
        ).fetchone()
    serialized = f"{row['encrypted_username']} {row['encrypted_password']}"
    assert endpoint.username == "proxy-user-secret"
    assert endpoint.password == "proxy-password-secret"
    assert "proxy-user-secret" not in serialized
    assert "proxy-password-secret" not in serialized
    encrypted_username = row["encrypted_username"]
    if isinstance(encrypted_username, bytes):
        encrypted_username = encrypted_username.decode()
    assert SessionCipher(key, account_a).decrypt(encrypted_username) == {
        "value": "proxy-user-secret"
    }
    with pytest.raises(SessionCipherError):
        SessionCipher(key, account_b).decrypt(encrypted_username)


def test_repr_errors_and_health_events_never_expose_credentials(
    tenant, caplog: pytest.LogCaptureFixture
) -> None:
    pool, _, user_a, _, account_a, _ = tenant
    endpoint = _endpoint()
    provider = FakeProvider(
        [endpoint],
        health=ProxyHealth.degraded("connection_failed"),
    )
    service = _service(pool, provider, _key())

    with pytest.raises(ProxyBindingError) as error:
        service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))
    assert error.value.transient is True
    assert error.value.reason == "connection_failed"

    with pool.connection() as connection:
        evidence = connection.execute(
            "SELECT evidence::text AS evidence FROM account_health_events"
        ).fetchone()["evidence"]
    exposed = f"{endpoint!r} {provider.health!r} {evidence} {caplog.text}"
    assert "proxy-user-secret" not in exposed
    assert "proxy-password-secret" not in exposed


def test_transient_proxy_denial_defers_without_degrading_lease(tenant) -> None:
    pool, _, user_a, _, account_a, _ = tenant
    provider = FakeProvider([_endpoint()])
    service = _service(pool, provider, _key())
    first = service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))
    provider.health = ProxyHealth.degraded("proxy_access_denied")

    with pytest.raises(ProxyBindingError) as error:
        service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))

    assert error.value.transient is True
    assert error.value.reason == "proxy_access_denied"
    assert provider.allocate_calls == 1
    with pool.connection() as connection:
        proxy = connection.execute(
            "SELECT health_status, last_checked_at FROM proxies"
        ).fetchone()
        health_status = proxy["health_status"]
        if isinstance(health_status, bytes):
            health_status = health_status.decode()
        assert health_status == "active"
        assert proxy["last_checked_at"] is not None
        event = connection.execute(
            "SELECT event_type, severity, evidence FROM account_health_events"
        ).fetchone()
        event_type = event["event_type"]
        severity = event["severity"]
        if isinstance(event_type, bytes):
            event_type = event_type.decode()
        if isinstance(severity, bytes):
            severity = severity.decode()
        assert event_type == "proxy_health_failed"
        assert severity == "warning"
        assert event["evidence"]["reason"] == "proxy_access_denied"
    assert first.id is not None


def test_failed_health_check_degrades_without_rotation_or_allocation(tenant) -> None:
    pool, _, user_a, _, account_a, _ = tenant
    provider = FakeProvider([_endpoint()])
    service = _service(pool, provider, _key())
    first = service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))
    provider.health = ProxyHealth.degraded("timeout")

    with pytest.raises(ProxyBindingError, match="health check failed"):
        service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))

    assert provider.allocate_calls == 1
    with pool.connection() as connection:
        proxy = connection.execute(
            "SELECT health_status, last_checked_at FROM proxies"
        ).fetchone()
        event = connection.execute(
            """
            SELECT event_type, severity, evidence
            FROM account_health_events
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    health_status = proxy["health_status"]
    if isinstance(health_status, bytes):
        health_status = health_status.decode()
    event_type = event["event_type"]
    severity = event["severity"]
    if isinstance(event_type, bytes):
        event_type = event_type.decode()
    if isinstance(severity, bytes):
        severity = severity.decode()
    assert health_status == "degraded"
    assert proxy["last_checked_at"] is not None
    assert event_type == "proxy_health_failed"
    assert severity == "warning"
    assert event["evidence"]["reason"] == "timeout"


@pytest.mark.parametrize("variant", ["current", "completed", "expired", "unknown", "other_tenant", "untagged", "invalid"])
def test_proxy_failure_tags_only_the_current_owned_job_without_changing_admission(tenant, variant):
    from datetime import UTC, datetime, timedelta
    pool, _, user_a, user_b, account_a, account_b = tenant
    provider = FakeProvider([_endpoint()])
    service = _service(pool, provider, _key())
    endpoint = service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))
    job_id = uuid.uuid4()
    with pool.connection() as connection:
        if variant not in {"unknown", "untagged", "invalid"}:
            connection.execute(
                "INSERT INTO jobs (id,user_id,ig_account_id,status,lease_expires_at) VALUES (%s,%s,%s,%s,%s)",
                (job_id, user_b if variant == "other_tenant" else user_a,
                 account_b if variant == "other_tenant" else account_a,
                 "completed" if variant == "completed" else "running",
                 datetime.now(UTC) + timedelta(seconds=-1 if variant == "expired" else 60)),
            )
        before = connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone()
    supplied = None if variant == "untagged" else str(job_id) if variant == "invalid" else job_id
    provider.health = ProxyHealth.degraded("proxy_access_denied")
    with pytest.raises(ProxyBindingError) as error:
        service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"), job_id=supplied)
    assert error.value.transient is True
    assert error.value.reason == "proxy_access_denied"
    with pool.connection() as connection:
        evidence = connection.execute("SELECT evidence FROM account_health_events ORDER BY created_at DESC LIMIT 1").fetchone()["evidence"]
        assert connection.execute("SELECT * FROM jobs WHERE id=%s", (job_id,)).fetchone() == before
        assert connection.execute("SELECT id FROM proxies").fetchone()["id"] == endpoint.id
    assert evidence["reason"] == "proxy_access_denied"
    assert evidence.get("job_id") == (str(job_id) if variant == "current" else None)
    assert provider.allocate_calls == 1


def test_explicit_rebind_is_atomic_and_records_reason(tenant) -> None:
    pool, _, user_a, _, account_a, _ = tenant
    provider = FakeProvider([_endpoint("old"), _endpoint("new")])
    service = _service(pool, provider, _key())
    old = service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))

    new = service.rebind(
        user_a,
        account_a,
        Geo("IN", "MH", "Mumbai"),
        reason="provider maintenance",
    )

    assert new.id != old.id
    assert provider.allocate_calls == 2
    with pool.connection() as connection:
        bindings = connection.execute(
            """
            SELECT proxy_id, released_at
            FROM proxy_bindings
            ORDER BY bound_at, id
            """
        ).fetchall()
        event = connection.execute(
            "SELECT event_type, evidence FROM account_health_events "
            "WHERE event_type = 'proxy_rebound'"
        ).fetchone()
    assert len(bindings) == 2
    assert bindings[0]["released_at"] is not None
    assert bindings[1]["released_at"] is None
    assert event["evidence"]["reason"] == "provider maintenance"


def test_failed_explicit_rebind_keeps_old_binding_active(tenant) -> None:
    pool, _, user_a, _, account_a, _ = tenant

    class WrongGeoProvider(FakeProvider):
        def allocate_static(
            self,
            geo: Geo,
            *,
            excluded_references: frozenset[str] = frozenset(),
        ) -> ProxyEndpoint:
            with self._lock:
                self.allocate_calls += 1
                return self._endpoints[self.allocate_calls - 1]

    provider = WrongGeoProvider(
        [_endpoint("old"), _endpoint("new", geo=Geo("US", "NY", "New York"))]
    )
    service = _service(pool, provider, _key())
    old = service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))

    with pytest.raises(ProxyBindingError, match="geography"):
        service.rebind(
            user_a,
            account_a,
            Geo("IN", "MH", "Mumbai"),
            reason="bad replacement",
        )

    current = service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))
    assert current.id == old.id
    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM proxy_bindings WHERE released_at IS NULL"
            ).fetchone()["count"]
            == 1
        )


@pytest.mark.parametrize("reason", ["connection_failed", "ip_mismatch"])
def test_unhealthy_rebind_candidate_never_releases_old_binding(
    tenant, reason: str
) -> None:
    pool, _, user_a, _, account_a, _ = tenant
    provider = FakeProvider([_endpoint("old"), _endpoint("new")])
    service = _service(pool, provider, _key())
    old = service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))
    provider.health = ProxyHealth.degraded(reason)

    with pytest.raises(ProxyBindingError, match="health check failed"):
        service.rebind(
            user_a,
            account_a,
            Geo("IN", "MH", "Mumbai"),
            reason="candidate verification",
        )

    provider.health = ProxyHealth.healthy()
    current = service.ensure_binding(user_a, account_a, Geo("IN", "MH", "Mumbai"))
    assert current.id == old.id
    with pool.connection() as connection:
        bindings = connection.execute(
            "SELECT proxy_id, released_at FROM proxy_bindings"
        ).fetchall()
        assert len(bindings) == 1
        assert bindings[0]["proxy_id"] == old.id
        assert bindings[0]["released_at"] is None
        assert (
            connection.execute("SELECT count(*) AS count FROM proxies").fetchone()[
                "count"
            ]
            == 1
        )
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM account_health_events "
                "WHERE event_type = 'proxy_rebound'"
            ).fetchone()["count"]
            == 0
        )


def _settings(**overrides) -> Settings:
    values = {
        "DATABASE_URL": "postgresql://localhost/engagemate",
        "SESSION_ENCRYPTION_KEY": _key(),
        "INTERNAL_API_KEY": "internal-test-key",
        "FINGERPRINT_GEO_MAP_JSON": json.dumps(
            [
                {
                    "country": "IN",
                    "region": "MH",
                    "city": "Mumbai",
                    "timezone": "Asia/Kolkata",
                    "locale": "en-IN",
                },
                {
                    "country": "US",
                    "region": "NY",
                    "city": "New York",
                    "timezone": "America/New_York",
                    "locale": "en-US",
                },
            ]
        ),
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


def _lease(
    reference: str,
    host: str,
    *,
    country: str = "IN",
    region: str = "MH",
    city: str = "Mumbai",
    username: str = "configured-user-secret",
    password: str = "configured-password-secret",
) -> dict:
    return {
        "reference": reference,
        "host": host,
        "port": 12345,
        "username": username,
        "password": password,
        "country": country,
        "region": region,
        "city": city,
    }


def _inventory_settings(leases: list[dict]) -> Settings:
    return _settings(
        IPROYAL_MODE="import",
        IPROYAL_ISP_LEASES_JSON=json.dumps(leases),
        PROXY_HEALTH_URL="https://health.example.invalid/v1/identity",
    )


def test_iproyal_inventory_rejects_geo_outside_fingerprint_registry() -> None:
    """A proxy geo without a browser mapping must fail during provider construction."""

    settings = _inventory_settings(
        [
            _lease(
                "london-1",
                "192.0.2.88",
                country="GB",
                region="ENG",
                city="London",
            )
        ]
    )

    with pytest.raises(ValueError) as raised:
        IPRoyalProvider.from_settings(settings)
    assert str(raised.value) == "IPRoyal ISP lease inventory is invalid"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


class HealthyIPRoyalProvider(IPRoyalProvider):
    def check(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        return ProxyHealth.healthy()


def test_iproyal_is_disabled_without_optional_provider_secrets() -> None:
    settings = _settings()
    assert settings.iproyal_mode == "disabled"
    with pytest.raises(ValueError, match="disabled"):
        IPRoyalProvider.from_settings(settings)


def test_iproyal_import_mode_requires_static_isp_inventory() -> None:
    with pytest.raises(ValidationError) as error:
        _settings(IPROYAL_MODE="import")

    message = str(error.value)
    assert "ISP lease inventory" in message


def test_iproyal_import_mode_rejects_blank_inventory() -> None:
    with pytest.raises(ValidationError, match="ISP lease inventory"):
        _settings(IPROYAL_MODE="import", IPROYAL_ISP_LEASES_JSON=" ")


def test_iproyal_settings_errors_hide_inventory_secrets() -> None:
    with pytest.raises(ValidationError) as error:
        _settings(
            IPROYAL_MODE="import",
            IPROYAL_ISP_LEASES_JSON="inventory-secret",
        )
    assert "inventory-secret" not in str(error.value)


def test_iproyal_managed_mode_requires_geo_catalog_and_https_health_url() -> None:
    with pytest.raises(ValidationError):
        Settings(
            DATABASE_URL="postgresql://localhost/test",
            SESSION_ENCRYPTION_KEY=_key(),
            INTERNAL_API_KEY="internal-test-key",
            IPROYAL_MODE="managed",
            _env_file=None,
        )

    settings = Settings(
        DATABASE_URL="postgresql://localhost/test",
        SESSION_ENCRYPTION_KEY=_key(),
        INTERNAL_API_KEY="internal-test-key",
        IPROYAL_MODE="managed",
        IPROYAL_GEO_CATALOG_JSON=json.dumps(
            [
                {
                    "country": "IN",
                    "region": "MH",
                    "city": "Mumbai",
                    "product_id": 9,
                    "plan_id": 4,
                    "location_id": 51,
                }
            ]
        ),
        PROXY_HEALTH_URL="https://example.test/identity",
        _env_file=None,
    )

    assert settings.iproyal_mode == "managed"
    # Zero while nobody is paying: the buffer is unconditional demand, so a
    # non-zero default buys leases for an empty platform. The authoritative
    # baseline for this lives in tests/deploy/test_billing_gate.py.
    assert settings.iproyal_spare_per_geo == 0
    assert settings.iproyal_max_order_quantity == 5
    assert settings.iproyal_access_token is None
    provider = IPRoyalProvider.from_settings(settings)
    assert "managed" in repr(provider)
    with pytest.raises(ValueError, match="no available"):
        provider.allocate_static(Geo("IN", "MH", "Mumbai"))


def test_iproyal_inventory_mode_uses_only_encrypted_database_leases() -> None:
    settings = _settings(
        IPROYAL_MODE="inventory",
        PROXY_HEALTH_URL="https://ipwho.is/",
    )

    provider = IPRoyalProvider.from_settings(settings)

    assert settings.iproyal_mode == "inventory"
    assert "inventory" in repr(provider)
    with pytest.raises(ValueError, match="no available"):
        provider.allocate_static(Geo("IN", "MH", "Mumbai"))


def test_iproyal_selects_exact_geo_and_excludes_active_references() -> None:
    settings = _inventory_settings(
        [
            _lease("mumbai-1", "203.0.113.10"),
            _lease("mumbai-2", "203.0.113.11"),
            _lease(
                "new-york-1",
                "203.0.113.12",
                country="US",
                region="NY",
                city="New York",
            ),
        ]
    )
    provider = IPRoyalProvider.from_settings(settings)

    first = provider.allocate_static(Geo("IN", "MH", "Mumbai"))
    second = provider.allocate_static(
        Geo("IN", "MH", "Mumbai"),
        excluded_references=frozenset({"mumbai-1"}),
    )
    us = provider.allocate_static(Geo("US", "NY", "New York"))

    assert first.provider_reference == "mumbai-1"
    assert second.provider_reference == "mumbai-2"
    assert us.provider_reference == "new-york-1"
    assert first.username == "configured-user-secret"
    assert "configured-user-secret" not in repr(provider)
    assert "configured-password-secret" not in repr(first)
    with pytest.raises(ValueError, match="no available"):
        provider.allocate_static(
            Geo("IN", "MH", "Mumbai"),
            excluded_references=frozenset({"mumbai-1", "mumbai-2"}),
        )


@pytest.mark.parametrize(
    "leases",
    [
        [{"password": "inventory-secret"}],
        [
            _lease("duplicate", "203.0.113.10"),
            _lease("duplicate", "203.0.113.11"),
        ],
        [
            _lease("first", "203.0.113.10"),
            _lease("second", "203.0.113.10"),
        ],
        [{**_lease("wrong-port", "203.0.113.10"), "port": True}],
        [{**_lease("hostname", "203.0.113.10"), "host": "proxy.example.com"}],
    ],
)
def test_iproyal_inventory_validation_is_strict_and_secret_safe(
    leases: list[dict],
) -> None:
    settings = _inventory_settings(leases)

    with pytest.raises(ValueError) as error:
        IPRoyalProvider.from_settings(settings)

    formatted = "".join(
        traceback.format_exception(
            type(error.value), error.value, error.value.__traceback__
        )
    )
    assert str(error.value) == "IPRoyal ISP lease inventory is invalid"
    assert "inventory-secret" not in formatted
    assert "configured-password-secret" not in formatted
    assert "configured-user-secret" not in repr(settings)


def _insert_accounts(pool, user_id: uuid.UUID, count: int) -> list[uuid.UUID]:
    account_ids = [uuid.uuid4() for _ in range(count)]
    with pool.connection() as connection:
        connection.cursor().executemany(
            """
            INSERT INTO ig_accounts (
                id, user_id, login_country, login_region, login_city
            )
            VALUES (%s, %s, 'IN', 'MH', 'Mumbai')
            """,
            [(account_id, user_id) for account_id in account_ids],
        )
    return account_ids


def _two_mumbai_inventory() -> Settings:
    return _inventory_settings(
        [
            _lease("mumbai-1", "203.0.113.10"),
            _lease("mumbai-2", "203.0.113.11"),
        ]
    )


def test_two_accounts_get_distinct_inventory_leases_and_third_exhausts(
    tenant,
) -> None:
    pool, _, user_a, _, _, _ = tenant
    first_account, second_account, third_account = _insert_accounts(pool, user_a, 3)
    provider = HealthyIPRoyalProvider.from_settings(_two_mumbai_inventory())
    service = ProxyBindingService(ProxyRepository(pool, _key()), provider)
    geo = Geo("IN", "MH", "Mumbai")

    first = service.ensure_binding(user_a, first_account, geo)
    second = service.ensure_binding(user_a, second_account, geo)

    assert first.provider_reference == "mumbai-1"
    assert second.provider_reference == "mumbai-2"
    with pytest.raises(ProxyBindingError, match="inventory unavailable"):
        service.ensure_binding(user_a, third_account, geo)
    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM proxy_bindings WHERE released_at IS NULL"
            ).fetchone()["count"]
            == 2
        )


def test_concurrent_provider_instances_select_distinct_inventory_leases(
    tenant,
) -> None:
    pool_a, pool_b, user_a, _, _, _ = tenant
    first_account, second_account = _insert_accounts(pool_a, user_a, 2)
    key = _key()
    settings = _two_mumbai_inventory()
    services = (
        ProxyBindingService(
            ProxyRepository(pool_a, key),
            HealthyIPRoyalProvider.from_settings(settings),
        ),
        ProxyBindingService(
            ProxyRepository(pool_b, key),
            HealthyIPRoyalProvider.from_settings(settings),
        ),
    )
    barrier = threading.Barrier(2)

    def bind(index: int) -> ProxyEndpoint:
        barrier.wait()
        return services[index].ensure_binding(
            user_a,
            (first_account, second_account)[index],
            Geo("IN", "MH", "Mumbai"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(bind, index) for index in range(2)]
        endpoints = [future.result(timeout=5) for future in futures]

    assert {endpoint.provider_reference for endpoint in endpoints} == {
        "mumbai-1",
        "mumbai-2",
    }


def test_new_provider_instance_respects_database_active_references(tenant) -> None:
    pool, _, user_a, _, _, _ = tenant
    first_account, second_account = _insert_accounts(pool, user_a, 2)
    key = _key()
    settings = _two_mumbai_inventory()
    first_service = ProxyBindingService(
        ProxyRepository(pool, key),
        HealthyIPRoyalProvider.from_settings(settings),
    )
    first = first_service.ensure_binding(
        user_a, first_account, Geo("IN", "MH", "Mumbai")
    )

    restarted_service = ProxyBindingService(
        ProxyRepository(pool, key),
        HealthyIPRoyalProvider.from_settings(settings),
    )
    second = restarted_service.ensure_binding(
        user_a, second_account, Geo("IN", "MH", "Mumbai")
    )

    assert first.provider_reference == "mumbai-1"
    assert second.provider_reference == "mumbai-2"


def test_iproyal_health_check_is_bounded_fail_closed_and_secret_safe(
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: dict = {}

    class FailingClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, _url: str):
            raise httpx.ProxyError("proxy-user-secret:proxy-password-secret rejected")

    provider = IPRoyalProvider(
        (_endpoint(),),
        health_url="https://health.example.invalid/v1/identity",
        connect_timeout=3.0,
        read_timeout=7.0,
        client_factory=FailingClient,
    )

    health = provider.check(_endpoint())

    assert health == ProxyHealth.degraded("connection_failed")
    assert captured["timeout"].connect == 3.0
    assert captured["timeout"].read == 7.0
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False
    assert "proxy-user-secret" not in repr(health)
    assert "proxy-password-secret" not in caplog.text


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "ip": "203.0.113.10",
                "country": "IN",
                "region": "MH",
                "city": "Mumbai",
            },
            ProxyHealth.healthy(),
        ),
        (
            {
                "ip": "203.0.113.10",
                "country_code": "IN",
                "region_code": "MH",
                "region": "Maharashtra",
                "city": "Mumbai",
                "timezone": "Asia/Kolkata",
                "postal": "400001",
            },
            ProxyHealth.healthy(),
        ),
        (
            {
                "ip": "203.0.113.99",
                "country": "IN",
                "region": "MH",
                "city": "Mumbai",
            },
            ProxyHealth.degraded("ip_mismatch"),
        ),
        (
            {
                "ip": "203.0.113.10",
                "country": "IN",
                "region": "KA",
                "city": "Bengaluru",
            },
            ProxyHealth.degraded("geo_mismatch"),
        ),
        (
            {
                "ip": "not-an-ip",
                "country": "IN",
                "region": "MH",
                "city": "Mumbai",
            },
            ProxyHealth.degraded("invalid_response"),
        ),
    ],
)
def test_iproyal_health_requires_observed_ip_and_exact_geo_identity(
    payload: dict, expected: ProxyHealth
) -> None:
    class Response:
        status_code = 200

        def json(self) -> dict:
            return payload

    class Client:
        def __init__(self, **_kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, _url: str) -> Response:
            return Response()

    endpoint = _endpoint(host="203.0.113.10")
    provider = IPRoyalProvider(
        (endpoint,),
        health_url="https://health.example.invalid/v1/identity",
        connect_timeout=3.0,
        read_timeout=7.0,
        client_factory=Client,
    )

    assert provider.check(endpoint) == expected


def test_iproyal_health_proxy_url_uses_one_ipv6_bracket_pair() -> None:
    captured: dict = {}

    class Response:
        status_code = 502

    class Client:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def get(self, _url: str) -> Response:
            return Response()

    endpoint = _endpoint(host="2001:db8::10")
    provider = IPRoyalProvider(
        (endpoint,),
        health_url="https://health.example.invalid/v1/identity",
        connect_timeout=3.0,
        read_timeout=7.0,
        client_factory=Client,
    )

    assert provider.check(endpoint) == ProxyHealth.degraded("unexpected_status")
    assert captured["proxy"].endswith("@[2001:db8::10]:12345")
    assert "[[" not in captured["proxy"]
    assert "]]" not in captured["proxy"]


@pytest.mark.parametrize(
    ("host", "server"),
    [
        ("203.0.113.10", "http://203.0.113.10:12345"),
        ("proxy.example.com", "http://proxy.example.com:12345"),
        ("2001:db8::10", "http://[2001:db8::10]:12345"),
    ],
)
def test_proxy_endpoint_server_formats_ip_and_dns_authorities(
    host: str, server: str
) -> None:
    assert _endpoint(host=host).server == server


def test_health_status_and_geo_values_validate_and_render_safely() -> None:
    assert ProxyHealth.healthy().status is ProxyHealthStatus.HEALTHY
    with pytest.raises(ValueError):
        Geo("india", "MH", "Mumbai")
    with pytest.raises(ValueError):
        ProxyEndpoint(
            provider="iproyal",
            provider_reference="lease",
            proxy_type="isp_static_residential",
            geo=Geo("IN", "MH", "Mumbai"),
            host="host",
            port=70000,
            username="u",
            password="p",
        )


def test_indeterminate_health_refuses_a_new_binding(tenant) -> None:
    """Regression: a third health status used to fall through to success.

    `ensure_binding` tested only for DEGRADED and returned the endpoint
    otherwise, so INDETERMINATE — nobody could confirm where this proxy is —
    bound an account to an unverified IP with nothing logged. Refusing is the
    whole point of the status existing.
    """

    pool, _, user_a, _, account_a, _ = tenant
    indeterminate = ProxyHealth(
        ProxyHealthStatus.INDETERMINATE, "no_geo_readings"
    )
    provider = FakeProvider([_endpoint()], health=indeterminate)

    with pytest.raises(ProxyBindingError):
        _service(pool, provider, _key()).ensure_binding(
            user_a, account_a, Geo("IN", "MH", "Mumbai")
        )


def test_indeterminate_health_still_allows_a_healthy_rebind(tenant) -> None:
    """An oracle outage must not permanently strand an account."""

    pool, _, user_a, _, account_a, _ = tenant
    geo = Geo("IN", "MH", "Mumbai")
    provider = FakeProvider(
        [_endpoint()],
        health=ProxyHealth(ProxyHealthStatus.INDETERMINATE, "no_geo_readings"),
    )
    service = _service(pool, provider, _key())
    with pytest.raises(ProxyBindingError):
        service.ensure_binding(user_a, account_a, geo)

    provider.health = ProxyHealth.healthy()

    assert service.ensure_binding(user_a, account_a, geo).country == "IN"


def test_indeterminate_health_leaves_the_ledger_untouched(tenant) -> None:
    """An oracle outage must not rewrite a proxy's recorded health.

    record_health mapped anything that was not HEALTHY to 'degraded', so a
    provider being unreachable would have marked good inventory bad and taken
    it out of supply, which is the opposite of what INDETERMINATE means.
    """

    pool, _, user_a, _, account_a, _ = tenant
    geo = Geo("IN", "MH", "Mumbai")
    provider = FakeProvider([_endpoint()])
    service = _service(pool, provider, _key())
    endpoint = service.ensure_binding(user_a, account_a, geo)

    ProxyRepository(pool, _key()).record_health(
        user_id=user_a,
        ig_account_id=account_a,
        endpoint=endpoint,
        health=ProxyHealth(ProxyHealthStatus.INDETERMINATE, "no_geo_readings"),
    )

    with pool.connection() as connection:
        row = connection.execute(
            "SELECT health_status FROM proxies WHERE id = %s", (endpoint.id,)
        ).fetchone()
    assert _text(row["health_status"]) == "active"


class _PanelClient:
    """A fake HTTP client that answers each provider URL from a script."""

    responses: dict = {}

    def __init__(self, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def get(self, url: str):
        payload = self.responses.get(url)

        class Response:
            status_code = 200 if payload is not None else 503

            def json(self_inner):
                if payload is None:
                    raise ValueError("no body")
                return payload

        return Response()


def _panel_provider(endpoint, responses, anchors, radius=25.0):
    _PanelClient.responses = responses
    return IPRoyalProvider(
        (endpoint,),
        health_url="https://ipwho.is/",
        connect_timeout=3.0,
        read_timeout=7.0,
        client_factory=_PanelClient,
        geo_anchors=anchors,
        geo_radius_miles=radius,
        geo_providers=("ipwho.is", "ipinfo.io"),
    )


_HEMPSTEAD = Geo("US", "NY", "Hempstead")
_ANCHORS = {_HEMPSTEAD: (40.7062, -73.6187)}


def test_one_provider_placing_the_lease_nearby_keeps_it() -> None:
    """Regression: the production false positive, end to end.

    ipwho.is says Delaware, ipinfo.io says nine miles away. The lease works.
    """

    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = _panel_provider(
        endpoint,
        {
            "https://ipwho.is/": {
                "ip": "203.0.113.10",
                "latitude": 38.7746,
                "longitude": -75.1393,
            },
            "https://ipinfo.io/json": {
                "ip": "203.0.113.10",
                "loc": "40.6895,-73.7510",
            },
        },
        _ANCHORS,
    )

    assert provider.check(endpoint) == ProxyHealth.healthy()


def test_every_provider_placing_the_lease_far_condemns_it() -> None:
    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = _panel_provider(
        endpoint,
        {
            "https://ipwho.is/": {
                "ip": "203.0.113.10",
                "latitude": 38.7746,
                "longitude": -75.1393,
            },
            "https://ipinfo.io/json": {
                "ip": "203.0.113.10",
                "loc": "37.3568,-77.4400",
            },
        },
        _ANCHORS,
    )

    assert provider.check(endpoint) == ProxyHealth.degraded("geo_mismatch")


def test_a_wrong_exit_address_still_fails_regardless_of_geography() -> None:
    """IP identity is not a vote. One provider seeing the wrong exit is fatal."""

    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = _panel_provider(
        endpoint,
        {
            "https://ipwho.is/": {
                "ip": "198.51.100.7",
                "latitude": 40.6895,
                "longitude": -73.7510,
            },
            "https://ipinfo.io/json": {
                "ip": "203.0.113.10",
                "loc": "40.6895,-73.7510",
            },
        },
        _ANCHORS,
    )

    assert provider.check(endpoint) == ProxyHealth.degraded("ip_mismatch")


def test_every_provider_unreachable_is_indeterminate_not_degraded() -> None:
    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = _panel_provider(endpoint, {}, _ANCHORS)

    health = provider.check(endpoint)

    assert health.status is ProxyHealthStatus.INDETERMINATE


@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize(("message", "reason"), [
    ("403 Forbidden proxy-user-secret:proxy-password-secret", "proxy_access_denied"),
    ("403", "proxy_access_denied"),
    ("proxy-user-secret:403 rejected", "connection_failed"),
    ("4030 invalid proxy-user-secret", "connection_failed"),
    ("407 Proxy Authentication Required proxy-user-secret:proxy-password-secret", "proxy_authentication_failed"),
    ("502 Bad Gateway proxy-user-secret:proxy-password-secret", "connection_failed"),
    ("proxy-user-secret:407 rejected", "connection_failed"),
])
def test_rejected_proxy_tunnel_is_not_a_geo_provider_outage(panel, message, reason, caplog, monkeypatch):
    monkeypatch.setattr("engagemate.proxy.iproyal.time.sleep", lambda _: None)
    calls = []

    class RejectedTunnel(_PanelClient):
        def get(self, url):
            calls.append(url)
            raise httpx.ProxyError(message)

    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = IPRoyalProvider(
        (endpoint,), health_url="https://ipwho.is/", connect_timeout=3.0,
        read_timeout=7.0, client_factory=RejectedTunnel,
        geo_anchors=_ANCHORS,
        geo_providers=("ipwho.is", "ipinfo.io") if panel else (),
    )
    health = provider.check(endpoint)
    assert health == ProxyHealth.degraded(reason)
    assert len(calls) == (2 if reason == "proxy_access_denied" else 1)
    assert "proxy-user-secret" not in repr(health) + caplog.text
    assert "proxy-password-secret" not in repr(health) + caplog.text


def test_a_geography_with_no_anchor_is_indeterminate() -> None:
    """An unanchored geography must not read as a mislocated proxy."""

    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = _panel_provider(
        endpoint,
        {"https://ipwho.is/": {"ip": "203.0.113.10", "latitude": 40.68, "longitude": -73.75}},
        {},
    )

    assert provider.check(endpoint).status is ProxyHealthStatus.INDETERMINATE


@pytest.mark.parametrize("panel", [False, True])
def test_origin_403_is_not_a_proxy_connect_denial(panel):
    calls = []

    class OriginForbidden(_PanelClient):
        def get(self, url):
            calls.append(url)
            return httpx.Response(403)

    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = IPRoyalProvider(
        (endpoint,), health_url="https://ipwho.is/", connect_timeout=3.0,
        read_timeout=7.0, client_factory=OriginForbidden,
        geo_anchors=_ANCHORS,
        geo_providers=("ipwho.is", "ipinfo.io") if panel else (),
    )
    health = provider.check(endpoint)
    expected = (ProxyHealth(ProxyHealthStatus.INDETERMINATE, "no_geo_readings")
                if panel else ProxyHealth.degraded("unexpected_status"))
    assert health == expected
    assert len(calls) == (2 if panel else 1)


@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("second", ["healthy", "denied", "auth", "wrong_ip", "wrong_geo", "origin403"])
def test_connect403_rechecks_same_endpoint_with_fresh_client(panel, second, monkeypatch):
    clients, events = [], []
    monkeypatch.setattr("engagemate.proxy.iproyal.time.sleep", lambda delay: events.append(("sleep", delay)))

    class Client(_PanelClient):
        def __init__(self, **kwargs):
            self.index = len(clients)
            clients.append(kwargs)
            events.append(("open", self.index))

        def __exit__(self, *_args):
            events.append(("close", self.index))

        def get(self, url):
            events.append(("get", self.index, url))
            if self.index == 0 or second == "denied":
                raise httpx.ProxyError("403 secret-credentials")
            if second == "auth":
                raise httpx.ProxyError("407 secret-credentials")
            if second == "origin403":
                return httpx.Response(403)
            ip = "198.51.100.7" if second == "wrong_ip" else "203.0.113.10"
            lat, lon = (0, 0) if second == "wrong_geo" else _ANCHORS[_HEMPSTEAD]
            payload = {"ip": ip, "latitude": lat, "longitude": lon,
                       "loc": f"{lat},{lon}", "country_code": "US", "region_code": "NY",
                       "city": "elsewhere" if second == "wrong_geo" else "Hempstead"}
            return httpx.Response(200, json=payload)

    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = IPRoyalProvider(
        (endpoint,), health_url="https://ipwho.is/", connect_timeout=3.0,
        read_timeout=7.0, client_factory=Client, geo_anchors=_ANCHORS,
        geo_providers=("ipwho.is", "ipinfo.io") if panel else (),
    )
    health = provider.check(endpoint)
    expected = {
        "healthy": ProxyHealth.healthy(),
        "denied": ProxyHealth.degraded("proxy_access_denied"),
        "auth": ProxyHealth.degraded("proxy_authentication_failed"),
        "wrong_ip": ProxyHealth.degraded("ip_mismatch"),
        "wrong_geo": ProxyHealth.degraded("geo_mismatch"),
        "origin403": (ProxyHealth(ProxyHealthStatus.INDETERMINATE, "no_geo_readings")
                      if panel else ProxyHealth.degraded("unexpected_status")),
    }[second]
    assert health == expected
    assert len(clients) == 2 and clients[0] == clients[1]
    assert clients[0]["follow_redirects"] is False and clients[0]["trust_env"] is False
    assert events[:5] == [("open", 0), ("get", 0, "https://ipwho.is/"),
                          ("close", 0), ("sleep", 1.0), ("open", 1)]
    assert events[-1] == ("close", 1)
    if panel and second == "healthy":
        assert [event[2] for event in events if event[:2] == ("get", 1)] == [
            "https://ipwho.is/", "https://ipinfo.io/json"]
    assert "secret" not in repr(health)


def test_retry_discards_partial_panel_readings(monkeypatch):
    attempts = []
    monkeypatch.setattr("engagemate.proxy.iproyal.time.sleep", lambda _: None)

    class Client(_PanelClient):
        def __init__(self, **kwargs):
            self.index = len(attempts)
            attempts.append([])

        def get(self, url):
            attempts[self.index].append(url)
            if self.index == 0 and url == "https://ipinfo.io/json":
                raise httpx.ProxyError("403 Forbidden")
            lat, lon = _ANCHORS[_HEMPSTEAD] if self.index == 0 else (0, 0)
            return httpx.Response(200, json={"ip": "203.0.113.10", "latitude": lat,
                                            "longitude": lon, "loc": f"{lat},{lon}"})

    endpoint = _endpoint(host="203.0.113.10", geo=_HEMPSTEAD)
    provider = IPRoyalProvider(
        (endpoint,), health_url="https://ipwho.is/", connect_timeout=3,
        read_timeout=7, client_factory=Client, geo_anchors=_ANCHORS,
        geo_providers=("ipwho.is", "ipinfo.io"),
    )
    assert provider.check(endpoint) == ProxyHealth.degraded("geo_mismatch")
    assert attempts == [["https://ipwho.is/", "https://ipinfo.io/json"]] * 2
