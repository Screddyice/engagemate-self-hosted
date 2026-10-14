"""The suggest-login-geo endpoint: timezone in, nearest free lease out.

Its two moving parts are unit-tested elsewhere (`free_leases_by_geo` in the
proxy-repository suite, `recommend_login_geo`/`resolve_timezone_point` as pure
tables). This proves the wiring: a real request against a real Postgres, with
anchors configured, returns the geography the parts imply.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[2]
API_DIR = ROOT_DIR / "api"
sys.path.insert(0, str(API_DIR))

from engagemate.config import Settings
from engagemate.control.dependencies import current_user_id
from engagemate.db.pool import create_pool
from main import create_app

_ANCHORS = json.dumps(
    [
        {"country": "US", "region": "NC", "city": "Raleigh", "latitude": 35.7796, "longitude": -78.6382},
        {"country": "US", "region": "AZ", "city": "Phoenix", "latitude": 33.4484, "longitude": -112.0740},
    ]
)
_GEO_MAP = json.dumps(
    [
        {"country": "US", "region": "NC", "city": "Raleigh", "timezone": "America/New_York", "locale": "en-US"},
        {"country": "US", "region": "AZ", "city": "Phoenix", "timezone": "America/Phoenix", "locale": "en-US"},
    ]
)

_SCHEMA = """
CREATE TABLE users (id uuid PRIMARY KEY, plan text NOT NULL DEFAULT 'starter');
CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id)
);
CREATE TABLE proxies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider text NOT NULL, provider_reference text NOT NULL,
    proxy_type text NOT NULL, country text NOT NULL, region text, city text,
    host text NOT NULL, port integer NOT NULL,
    encrypted_username text NOT NULL, encrypted_password text NOT NULL,
    credential_scope text NOT NULL, health_status text NOT NULL DEFAULT 'active'
);
CREATE TABLE proxy_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    proxy_id uuid NOT NULL REFERENCES proxies(id), released_at timestamptz
);
"""


def _proxy(
    connection,
    host,
    region,
    city,
    scope="inventory",
    health="active",
    reference=None,
):
    return connection.execute(
        "INSERT INTO proxies (provider, provider_reference, proxy_type, country, "
        "region, city, host, port, encrypted_username, encrypted_password, "
        "credential_scope, health_status) VALUES ('iproyal', %s, "
        "'isp_static_residential', 'US', %s, %s, %s, 12323, 'u', 'p', %s, %s) "
        "RETURNING id",
        (reference or f"order:1:{host}", region, city, host, scope, health),
    ).fetchone()["id"]


@pytest.fixture()
def client(request):
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode:
        pytest.skip("local PostgreSQL is unavailable")
    db = f"em_suggest_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    subprocess.run(["createdb", db], capture_output=True, check=True)
    url = f"postgresql://localhost/{db}"
    try:
        with psycopg.connect(url, autocommit=True) as c:
            c.execute(_SCHEMA)
        pool = create_pool(url, min_size=1, max_size=4)
        if getattr(request, "param", None) != "import":
            with pool.connection() as c:
                _proxy(c, "203.0.113.1", "NC", "Raleigh")
                _proxy(c, "203.0.113.2", "NC", "Raleigh")
                _proxy(c, "203.0.113.3", "AZ", "Phoenix")
        settings_values = {
            "DATABASE_URL": url,
            "SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(
                os.urandom(32)
            ).decode().rstrip("="),
            "INTERNAL_API_KEY": "internal-test-key",
            "FINGERPRINT_GEO_MAP_JSON": _GEO_MAP,
            "PROXY_GEO_ANCHORS_JSON": _ANCHORS,
            "_env_file": None,
        }
        if getattr(request, "param", None) == "import":
            settings_values.update(
                {
                    "IPROYAL_MODE": "import",
                    "IPROYAL_ISP_LEASES_JSON": json.dumps(
                        [
                            {
                                "reference": "configured-raleigh-1",
                                "host": "203.0.113.10",
                                "port": 12345,
                                "username": "configured-user-secret",
                                "password": "configured-password-secret",
                                "country": "US",
                                "region": "NC",
                                "city": "Raleigh",
                            }
                        ]
                    ),
                    "PROXY_HEALTH_URL": "https://health.example.invalid/v1/identity",
                }
            )
        settings = Settings(
            **settings_values,
        )
        app = create_app(settings=settings, pool=pool)
        app.dependency_overrides[current_user_id] = lambda: uuid.uuid4()
        try:
            with TestClient(app) as tc:
                yield tc, pool
        finally:
            pool.close()
    finally:
        subprocess.run(["dropdb", "--if-exists", "--force", db], capture_output=True, check=False)


def test_east_coast_timezone_recommends_raleigh(client):
    tc, _ = client
    r = tc.post("/v1/accounts/suggest-login-geo", json={"timezone": "America/New_York"})
    assert r.status_code == 200
    body = r.json()
    assert body["recommended"]["city"] == "Raleigh"
    assert body["recommended"]["free_leases"] == 2
    # New York -> Raleigh is ~425 mi, past the 300-mi guard: Raleigh is still
    # the nearest lease we hold, but far enough that the operator should see the
    # distance and weigh buying one closer. Recommend, but flag.
    assert body["within_guard"] is False
    assert body["distance_miles"] > 400
    assert {o["city"] for o in body["options"]} == {"Raleigh", "Phoenix"}


def test_explicit_point_near_phoenix_recommends_phoenix(client):
    tc, _ = client
    r = tc.post("/v1/accounts/suggest-login-geo", json={"latitude": 33.5, "longitude": -112.0})
    assert r.json()["recommended"]["city"] == "Phoenix"


def test_unknown_timezone_returns_options_without_a_recommendation(client):
    tc, _ = client
    r = tc.post("/v1/accounts/suggest-login-geo", json={"timezone": "Mars/Olympus"})
    body = r.json()
    assert body["recommended"] is None
    assert len(body["options"]) == 2  # still lists what is buyable for a picker


def test_no_free_inventory_recommends_nothing(client):
    tc, pool = client
    with pool.connection() as c:
        c.execute("UPDATE proxies SET health_status = 'degraded'")
    r = tc.post("/v1/accounts/suggest-login-geo", json={"timezone": "America/New_York"})
    body = r.json()
    assert body["recommended"] is None
    assert body["options"] == []


@pytest.mark.parametrize("client", ["import"], indirect=True)
def test_import_mode_recommends_configured_inventory_when_database_is_empty(client):
    """The onboarding recommendation must see leases the binder can allocate."""

    tc, _ = client
    response = tc.post(
        "/v1/accounts/suggest-login-geo",
        json={"timezone": "America/New_York"},
    )

    assert response.status_code == 200
    assert response.json()["recommended"] == {
        "country": "US",
        "region": "NC",
        "city": "Raleigh",
        "free_leases": 1,
    }


@pytest.mark.parametrize("client", ["import"], indirect=True)
def test_import_mode_excludes_configured_lease_held_by_active_binding(client):
    tc, pool = client
    with pool.connection() as connection:
        proxy_id = _proxy(
            connection,
            "203.0.113.10",
            "NC",
            "Raleigh",
            scope="account",
            reference="configured-raleigh-1",
        )
        connection.execute(
            "INSERT INTO proxy_bindings (proxy_id) VALUES (%s)",
            (proxy_id,),
        )

    response = tc.post(
        "/v1/accounts/suggest-login-geo",
        json={"timezone": "America/New_York"},
    )

    assert response.status_code == 200
    assert response.json()["recommended"] is None
    assert response.json()["options"] == []
