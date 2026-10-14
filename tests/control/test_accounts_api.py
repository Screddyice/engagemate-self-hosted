from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient


ROOT_DIR = Path(__file__).resolve().parents[2]
API_DIR = ROOT_DIR / "api"
sys.path.insert(0, str(API_DIR))

from engagemate.config import Settings
from engagemate.control.dependencies import current_user_id
from engagemate.db.accounts import AccountRepository
from engagemate.db.jobs import JobRepository
from engagemate.db.pool import create_pool
from engagemate.security.session_cipher import SessionCipher
from main import create_app


FINGERPRINT_GEO_MAP_JSON = json.dumps(
    [
        {
            "country": "IN",
            "region": "MH",
            "city": "Mumbai",
            "timezone": "Asia/Kolkata",
            "locale": "en-IN",
        }
    ]
)


SCHEMA_SQL = """
CREATE TABLE users (
    id uuid PRIMARY KEY,
    plan text NOT NULL DEFAULT 'starter'
);
CREATE TABLE ig_accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    username text NOT NULL,
    ig_user_id text,
    login_country text,
    login_region text,
    login_city text,
    follower_count integer,
    warmup_started_at timestamptz,
    health_status text NOT NULL DEFAULT 'active',
    last_healthy_at timestamptz,
    paused_at timestamptz,
    pause_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, username)
);

CREATE TABLE agent_configs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL UNIQUE,
    enabled boolean NOT NULL DEFAULT false,
    daily_like_limit integer NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ig_accounts_user_ig_user_id_idx
    ON ig_accounts (user_id, ig_user_id)
    WHERE ig_user_id IS NOT NULL;
CREATE TABLE ig_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    encrypted_cookie_blob text NOT NULL,
    encryption_version integer NOT NULL,
    captured_user_agent text NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(),
    last_validated_at timestamptz,
    expires_at timestamptz
);
CREATE TABLE account_health_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    event_type text NOT NULL,
    severity text NOT NULL,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE proxies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
);
CREATE TABLE proxy_bindings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    proxy_id uuid NOT NULL REFERENCES proxies(id),
    bound_at timestamptz NOT NULL DEFAULT now(),
    released_at timestamptz
);
CREATE TABLE jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    ig_account_id uuid NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    schedule_key text NOT NULL UNIQUE,
    scheduled_for timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'queued',
    claimed_by text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""


@pytest.fixture(scope="module")
def database_url() -> str:
    if subprocess.run(["pg_isready"], capture_output=True, check=False).returncode:
        pytest.skip("local PostgreSQL is unavailable")
    database = f"engagemate_control_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    created = subprocess.run(["createdb", database], capture_output=True, text=True)
    if created.returncode:
        pytest.fail(f"could not create disposable PostgreSQL database: {created.stderr}")
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
def control(database_url: str):
    pool = create_pool(database_url, min_size=1, max_size=8)
    user_a, user_b = uuid.uuid4(), uuid.uuid4()
    with pool.connection() as connection:
        connection.execute(
            "TRUNCATE jobs, account_health_events, proxy_bindings, proxies, ig_sessions, ig_accounts, users CASCADE"
        )
        connection.execute(
            # Agency seats, so identity and health tests are not
            # constrained by a plan limit they do not exercise.
            "INSERT INTO users (id, plan) VALUES (%s, 'agency'), (%s, 'agency')",
            (user_a, user_b),
        )

    settings = Settings(
        DATABASE_URL=database_url,
        SESSION_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32))
        .decode()
        .rstrip("="),
        INTERNAL_API_KEY="internal-test-key",
        FINGERPRINT_GEO_MAP_JSON=FINGERPRINT_GEO_MAP_JSON,
        _env_file=None,
    )
    app = create_app(settings=settings, pool=pool)
    identity = {"value": user_a}
    app.dependency_overrides[current_user_id] = lambda: identity["value"]
    try:
        with TestClient(app) as client:
            yield client, pool, identity, user_a, user_b
    finally:
        pool.close()


def _session_payload(username: str = "target_owner") -> dict:
    return {
        "username": username,
        "ig_user_id": "123456",
        "cookies": [
            {
                "name": "sessionid",
                "value": "captured-cookie-secret",
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "None",
                "expirationDate": 1780000000,
            },
            {
                "name": "ds_user_id",
                "value": "123456",
                "domain": ".instagram.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "sameSite": "Lax",
            },
        ],
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "login_geo": {"country": "IN", "region": "MH", "city": "Mumbai"},
    }


def _set_session_identity(payload: dict, ig_user_id: str) -> None:
    payload["ig_user_id"] = ig_user_id
    cookie_identity = next(
        cookie for cookie in payload["cookies"] if cookie["name"] == "ds_user_id"
    )
    cookie_identity["value"] = ig_user_id.strip()


def test_session_upsert_returns_safe_projection_and_reuses_account(
    control, caplog: pytest.LogCaptureFixture
) -> None:
    client, pool, _, user_a, _ = control
    first = client.post("/v1/accounts/session", json=_session_payload())
    second = client.post("/v1/accounts/session", json=_session_payload())

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert set(first.json()) == {
        "id",
        "username",
        "health",
        "captured_at",
        "proxy_bound",
    }
    serialized = first.text + second.text
    assert "captured-cookie-secret" not in serialized
    assert "encrypted_cookie_blob" not in serialized
    assert "captured-cookie-secret" not in caplog.text

    with pool.connection() as connection:
        session = connection.execute(
            "SELECT user_id, encrypted_cookie_blob FROM ig_sessions"
        ).fetchall()
    assert len(session) == 1
    assert session[0]["user_id"] == user_a
    encrypted_blob = session[0]["encrypted_cookie_blob"]
    if isinstance(encrypted_blob, bytes):
        encrypted_blob = encrypted_blob.decode()
    assert "captured-cookie-secret" not in encrypted_blob


def test_session_capture_rejects_mismatched_ds_user_id(control) -> None:
    client, pool, _, _, _ = control
    payload = _session_payload("identity_cookie_mismatch")
    next(cookie for cookie in payload["cookies"] if cookie["name"] == "ds_user_id")[
        "value"
    ] = "654321"

    response = client.post("/v1/accounts/session", json=payload)

    assert response.status_code == 422
    assert "654321" not in response.text
    with pool.connection() as connection:
        account_count = connection.execute(
            "SELECT count(*) AS count FROM ig_accounts"
        ).fetchone()["count"]
    assert account_count == 0


def test_capture_does_not_start_warmup_and_reconnect_preserves_progress(control):
    client, pool, _, user_a, _ = control
    payload = _session_payload()
    response = client.post("/v1/accounts/session", json=payload)
    assert response.status_code == 201
    account_id = response.json()["id"]
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT warmup_started_at FROM ig_accounts WHERE id = %s", (account_id,)
        ).fetchone()
        assert row["warmup_started_at"] is None
        connection.execute(
            "UPDATE ig_accounts SET warmup_started_at = '2026-01-01T00:00:00Z' WHERE id = %s",
            (account_id,),
        )
    assert client.post("/v1/accounts/session", json=payload).status_code == 200
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT warmup_started_at FROM ig_accounts WHERE id = %s", (account_id,)
        ).fetchone()
    assert row["warmup_started_at"] == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_session_capture_rejects_geo_outside_configured_fingerprint_registry(
    control,
) -> None:
    """Persisting an unsupported geo would defer a permanent worker failure."""

    client, pool, _, _, _ = control
    payload = _session_payload("unsupported_geo_owner")
    payload["login_geo"] = {
        "country": "GB",
        "region": "ENG",
        "city": "London",
    }

    response = client.post("/v1/accounts/session", json=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "unsupported login geography"}
    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM ig_accounts"
            ).fetchone()["count"]
            == 0
        )


def test_api_starts_without_worker_geo_config_but_capture_fails_closed(
    control,
) -> None:
    """Worker-only config must not stop API health while session capture stays closed."""

    client, pool, _, _, _ = control
    settings = client.app.state.settings
    configured_registry = settings.fingerprint_geo_map_json
    settings.fingerprint_geo_map_json = None
    try:
        response = client.post(
            "/v1/accounts/session",
            json=_session_payload("missing_geo_config_owner"),
        )
    finally:
        settings.fingerprint_geo_map_json = configured_registry

    assert response.status_code == 503
    assert response.json() == {
        "detail": "fingerprint geography configuration unavailable"
    }
    with pool.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) AS count FROM ig_accounts"
            ).fetchone()["count"]
            == 0
        )


def test_concurrent_first_capture_is_idempotent_across_repeated_runs(control) -> None:
    client, pool, _, user_a, _ = control

    for run in range(5):
        barrier = Barrier(4)
        payload = _session_payload(f"concurrent_{run}")
        _set_session_identity(payload, str(8_000 + run))

        def capture() -> tuple[int, str]:
            barrier.wait()
            response = client.post("/v1/accounts/session", json=payload)
            return response.status_code, response.json().get("id", "")

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: capture(), range(4)))

        assert all(status in (200, 201) for status, _ in results), results
        assert len({account_id for _, account_id in results}) == 1

    with pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT account.ig_user_id,
                   count(DISTINCT account.id) AS account_count,
                   count(session.id) AS session_count
            FROM ig_accounts account
            LEFT JOIN ig_sessions session
              ON session.ig_account_id = account.id
             AND session.user_id = account.user_id
            WHERE account.user_id = %(user_id)s
              AND account.ig_user_id IN ('8000', '8001', '8002', '8003', '8004')
            GROUP BY account.ig_user_id
            ORDER BY account.ig_user_id
            """,
            {"user_id": user_a},
        ).fetchall()
    assert len(rows) == 5
    assert all(row["account_count"] == 1 for row in rows)
    assert all(row["session_count"] == 1 for row in rows)


def test_stable_identity_survives_rename_and_rejects_username_rebinding(control) -> None:
    client, pool, _, user_a, _ = control
    payload = _session_payload("original_name")
    _set_session_identity(payload, "7001")
    first = client.post("/v1/accounts/session", json=payload)
    account_id = first.json()["id"]

    renamed = client.patch(
        f"/v1/accounts/{account_id}", json={"username": "dashboard_rename"}
    )
    assert renamed.status_code == 200
    payload["username"] = "instagram_rename"
    resynced = client.post("/v1/accounts/session", json=payload)
    assert resynced.status_code == 200
    assert resynced.json()["id"] == account_id
    assert resynced.json()["username"] == "instagram_rename"

    other = _session_payload("instagram_rename")
    _set_session_identity(other, "7002")
    conflict = client.post("/v1/accounts/session", json=other)
    assert conflict.status_code == 409
    assert "captured-cookie-secret" not in conflict.text

    with pool.connection() as connection:
        account_count = connection.execute(
            """
            SELECT count(*) FROM ig_accounts
            WHERE user_id = %s AND ig_user_id = '7001'
            """,
            (user_a,),
        ).fetchone()["count"]
    assert account_count == 1


def test_identity_canonicalization_reuses_account_and_lowercases_username(control) -> None:
    client, pool, _, user_a, _ = control
    first = _session_payload("target")
    _set_session_identity(first, "123")
    created = client.post("/v1/accounts/session", json=first)

    second = _session_payload("Target")
    _set_session_identity(second, " 123 ")
    resynced = client.post("/v1/accounts/session", json=second)

    assert created.status_code == 201
    assert resynced.status_code == 200
    assert resynced.json()["id"] == created.json()["id"]
    assert resynced.json()["username"] == "target"
    with pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT username, ig_user_id
            FROM ig_accounts
            WHERE user_id = %s
            """,
            (user_a,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["username"] in ("target", b"target")
    assert rows[0]["ig_user_id"] in ("123", b"123")


@pytest.mark.parametrize(
    "ig_user_id",
    ["abc", "12.3", "１２３", "١٢٣", "0", "000", "0123", "+123", "-123"],
)
def test_noncanonical_instagram_ids_are_safe_422s(control, ig_user_id: str) -> None:
    client, _, _, _, _ = control
    payload = _session_payload("invalid_identity")
    _set_session_identity(payload, ig_user_id)
    secret = payload["cookies"][0]["value"]

    response = client.post("/v1/accounts/session", json=payload)

    assert response.status_code == 422
    assert secret not in response.text
    assert ig_user_id not in response.text


def test_case_only_username_collision_for_another_identity_is_409(control) -> None:
    client, pool, _, _, _ = control
    first = _session_payload("case_owner")
    _set_session_identity(first, "9001")
    assert client.post("/v1/accounts/session", json=first).status_code == 201

    second = _session_payload("Case_Owner")
    _set_session_identity(second, "9002")
    response = client.post("/v1/accounts/session", json=second)

    assert response.status_code == 409
    with pool.connection() as connection:
        count = connection.execute(
            "SELECT count(*) FROM ig_accounts WHERE username = 'case_owner'"
        ).fetchone()["count"]
    assert count == 1


def test_repository_defensively_canonicalizes_direct_identity_calls(control) -> None:
    _, pool, _, user_a, _ = control
    repository = AccountRepository(
        pool,
        base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="),
    )
    account, created = repository.upsert_session(
        user_id=user_a,
        username="Direct_User",
        ig_user_id=" 555 ",
        cookies=[{"name": "sessionid", "value": "not-logged"}],
        user_agent="test-agent",
        login_country="IN",
        login_region=None,
        login_city=None,
        expires_at=None,
    )
    assert created is True
    assert account.username == "direct_user"

    with pytest.raises(ValueError, match="invalid Instagram user ID"):
        repository.upsert_session(
            user_id=user_a,
            username="another_user",
            ig_user_id="٠",
            cookies=[],
            user_agent="test-agent",
            login_country="IN",
            login_region=None,
            login_city=None,
            expires_at=None,
        )


def test_verified_identity_repair_requires_same_username_and_unclaimed_id(control) -> None:
    client, pool, _, user_id, _ = control
    account = client.post(
        "/v1/accounts/session", json=_session_payload("repair_owner")
    ).json()
    other = _session_payload("other_owner")
    _set_session_identity(other, "654321")
    client.post("/v1/accounts/session", json=other)
    account_uuid = uuid.UUID(account["id"])
    repository = AccountRepository(
        pool, client.app.state.settings.session_encryption_key.get_secret_value()
    )

    assert not repository.repair_verified_identity(
        user_id,
        account_uuid,
        expected_ig_user_id="123456",
        verified_ig_user_id="654321",
        verified_username="repair_owner",
    )
    assert not repository.repair_verified_identity(
        user_id,
        account_uuid,
        expected_ig_user_id="123456",
        verified_ig_user_id="789012",
        verified_username="different_owner",
    )
    assert repository.repair_verified_identity(
        user_id,
        account_uuid,
        expected_ig_user_id="123456",
        verified_ig_user_id="789012",
        verified_username="REPAIR_OWNER",
    )
    with pool.connection() as connection:
        repaired = connection.execute(
            "SELECT ig_user_id FROM ig_accounts WHERE id=%s", (account_uuid,)
        ).fetchone()
        events = connection.execute(
            """
            SELECT count(*) AS count FROM account_health_events
            WHERE ig_account_id=%s AND event_type='identity_repaired'
            """,
            (account_uuid,),
        ).fetchone()["count"]
    assert repaired["ig_user_id"] in ("789012", b"789012")
    assert events == 1


def test_all_account_routes_are_tenant_scoped_and_pause_resume(control) -> None:
    client, pool, identity, user_a, user_b = control
    created = client.post("/v1/accounts/session", json=_session_payload()).json()
    account_id = created["id"]

    assert client.get(f"/v1/accounts/{account_id}").status_code == 200
    renamed = client.patch(
        f"/v1/accounts/{account_id}", json={"username": "renamed_owner"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["username"] == "renamed_owner"
    case_renamed = client.patch(
        f"/v1/accounts/{account_id}", json={"username": "Renamed_Owner"}
    )
    assert case_renamed.status_code == 200
    assert case_renamed.json()["username"] == "renamed_owner"

    paused = client.post(
        f"/v1/accounts/{account_id}/pause", json={"reason": "customer requested"}
    )
    assert paused.status_code == 200
    assert paused.json()["health"] == "paused"
    assert paused.json()["paused_at"] is not None
    paused_again = client.post(
        f"/v1/accounts/{account_id}/pause", json={"reason": "login_required"}
    )
    assert paused_again.json()["paused_at"] == paused.json()["paused_at"]
    resumed = client.post(f"/v1/accounts/{account_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["health"] == "active"
    assert resumed.json()["paused_at"] is None
    with pool.connection() as connection:
        resets = connection.execute(
            "SELECT count(*) AS count FROM account_health_events "
            "WHERE ig_account_id=%s AND event_type='warmup_restart'",
            (account_id,),
        ).fetchone()["count"]
    assert resets == 0

    identity["value"] = user_b
    for method, url, body in [
        ("get", f"/v1/accounts/{account_id}", None),
        ("patch", f"/v1/accounts/{account_id}", {"username": "stolen"}),
        ("post", f"/v1/accounts/{account_id}/pause", {"reason": "stolen"}),
        ("post", f"/v1/accounts/{account_id}/resume", None),
        ("post", f"/v1/accounts/{account_id}/jobs", {}),
    ]:
        response = getattr(client, method)(url, json=body) if body is not None else getattr(client, method)(url)
        assert response.status_code == 404

    identity["value"] = user_a


def test_resume_after_thirty_days_restarts_warmup_once(control) -> None:
    client, pool, _, _, _ = control
    account_id = client.post(
        "/v1/accounts/session", json=_session_payload("long_paused_owner")
    ).json()["id"]
    assert client.post(
        f"/v1/accounts/{account_id}/pause", json={"reason": "login_required"}
    ).status_code == 200
    with pool.connection() as connection:
        connection.execute(
            "UPDATE ig_accounts SET paused_at=now()-interval '30 days' WHERE id=%s",
            (account_id,),
        )

    assert client.post(f"/v1/accounts/{account_id}/resume").status_code == 200
    assert client.post(f"/v1/accounts/{account_id}/resume").status_code == 404
    with pool.connection() as connection:
        events = connection.execute(
            "SELECT event_type, evidence FROM account_health_events "
            "WHERE ig_account_id=%s AND event_type='warmup_restart'",
            (account_id,),
        ).fetchall()
    assert len(events) == 1
    assert events[0]["evidence"]["disconnected_at"]


def test_job_create_and_read_enforce_ownership(control) -> None:
    client, _, identity, user_a, user_b = control
    account_id = client.post(
        "/v1/accounts/session", json=_session_payload("job_owner")
    ).json()["id"]
    scheduled_for = datetime.now(timezone.utc).isoformat()
    created = client.post(
        f"/v1/accounts/{account_id}/jobs",
        json={"scheduled_for": scheduled_for, "schedule_key": "manual-test-job"},
    )

    assert created.status_code == 201
    assert created.json()["status"] == "queued"
    job_id = created.json()["id"]
    assert client.get(f"/v1/jobs/{job_id}").status_code == 200

    identity["value"] = user_b
    assert client.get(f"/v1/jobs/{job_id}").status_code == 404
    identity["value"] = user_a


def test_pause_cancels_queued_jobs_and_unsafe_accounts_reject_new_jobs(control) -> None:
    client, pool, _, _, _ = control
    account_id = client.post(
        "/v1/accounts/session", json=_session_payload("pause_job_owner")
    ).json()["id"]
    created = client.post(
        f"/v1/accounts/{account_id}/jobs",
        json={"schedule_key": "pause-cancel-job"},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]

    paused = client.post(
        f"/v1/accounts/{account_id}/pause", json={"reason": "operator stop"}
    )
    assert paused.status_code == 200
    with pool.connection() as connection:
        status_value = connection.execute(
            "SELECT status FROM jobs WHERE id = %s", (job_id,)
        ).fetchone()["status"]
    if isinstance(status_value, bytes):
        status_value = status_value.decode()
    assert status_value == "cancelled"
    assert JobRepository(pool).claim_next("test-worker") is None

    for health in ("paused", "challenged", "banned"):
        with pool.connection() as connection:
            connection.execute(
                "UPDATE ig_accounts SET health_status = %s WHERE id = %s",
                (health, account_id),
            )
        blocked = client.post(
            f"/v1/accounts/{account_id}/jobs",
            json={"schedule_key": f"blocked-{health}"},
        )
        assert blocked.status_code == 409


def test_validation_rejects_empty_cookies_bad_geo_and_invalid_patch(control) -> None:
    client, _, _, _, _ = control
    payload = _session_payload()
    payload["cookies"] = []
    assert client.post("/v1/accounts/session", json=payload).status_code == 422

    payload = _session_payload()
    payload["login_geo"]["country"] = "IND"
    assert client.post("/v1/accounts/session", json=payload).status_code == 422

    account_id = client.post(
        "/v1/accounts/session", json=_session_payload("validate_owner")
    ).json()["id"]
    assert client.patch(
        f"/v1/accounts/{account_id}", json={"username": ""}
    ).status_code == 422
    assert client.post(
        f"/v1/accounts/{account_id}/jobs",
        json={"scheduled_for": "2026-07-31T10:00:00"},
    ).status_code == 422


def test_session_expiry_uses_only_sessionid_cookie(control) -> None:
    client, pool, _, _, _ = control
    payload = _session_payload("expiry_owner")
    session_expiry = payload["cookies"][0]["expirationDate"]
    payload["cookies"].append(
        {
            "name": "csrftoken",
            "value": "aux-secret",
            "domain": "www.instagram.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "sameSite": "Lax",
            "expirationDate": session_expiry - 1_000_000,
        }
    )
    account_id = client.post("/v1/accounts/session", json=payload).json()["id"]

    with pool.connection() as connection:
        expires_at = connection.execute(
            "SELECT expires_at FROM ig_sessions WHERE ig_account_id = %s",
            (account_id,),
        ).fetchone()["expires_at"]
    assert expires_at == datetime.fromtimestamp(session_expiry, tz=timezone.utc)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("domain", "evil.example"),
        ("domain", "instagram.com.evil.example"),
        ("domain", "..instagram.com"),
        ("path", "relative"),
        ("secure", False),
        ("httpOnly", False),
    ],
)
def test_cookie_validation_rejects_non_instagram_or_unsafe_sessionid(
    control, field: str, value
) -> None:
    client, _, _, _, _ = control
    payload = _session_payload(f"unsafe_{field}")
    secret = payload["cookies"][0]["value"]
    payload["cookies"][0][field] = value
    response = client.post("/v1/accounts/session", json=payload)
    assert response.status_code == 422
    assert secret not in response.text


@pytest.mark.parametrize("expiration", ["Infinity", "1e999", "253402300800"])
def test_invalid_session_expiration_is_a_safe_422(control, expiration: str) -> None:
    client, _, _, _, _ = control
    payload = _session_payload("bad_expiry")
    raw = (
        __import__("json")
        .dumps(payload)
        .replace("1780000000", expiration, 1)
    )
    response = client.post(
        "/v1/accounts/session",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert "captured-cookie-secret" not in response.text


def test_challenged_account_cannot_be_manually_resumed(control) -> None:
    client, pool, _, _, _ = control
    account_id = client.post(
        "/v1/accounts/session", json=_session_payload("challenged_owner")
    ).json()["id"]
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE ig_accounts
            SET health_status = 'challenged',
                paused_at = now(),
                pause_reason = 'checkpoint'
            WHERE id = %s
            """,
            (account_id,),
        )

    assert client.post(f"/v1/accounts/{account_id}/resume").status_code == 404
    account = client.get(f"/v1/accounts/{account_id}")
    assert account.json()["health"] == "challenged"


def test_identity_mismatch_requires_a_fresh_matching_session_before_resume(control) -> None:
    client, pool, _, _, _ = control
    payload = _session_payload("identity_owner")
    account_id = client.post("/v1/accounts/session", json=payload).json()["id"]
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE ig_accounts
            SET health_status='paused', paused_at=(
                    SELECT captured_at + interval '1 microsecond'
                    FROM ig_sessions WHERE ig_account_id=%s
                ),
                pause_reason='relationship_identity_mismatch'
            WHERE id=%s
            """,
            (account_id, account_id),
        )

    stale = client.post(f"/v1/accounts/{account_id}/resume")
    assert stale.status_code == 409
    assert client.get(f"/v1/accounts/{account_id}").json()["health"] == "paused"

    wrong_identity = dict(payload)
    wrong_identity["cookies"] = [dict(cookie) for cookie in payload["cookies"]]
    wrong_identity["cookies"][-1]["value"] = "654321"
    assert client.post("/v1/accounts/session", json=wrong_identity).status_code == 422
    account_uuid = uuid.UUID(account_id)
    encrypted = SessionCipher(
        client.app.state.settings.session_encryption_key.get_secret_value(),
        account_uuid,
    ).encrypt({"cookies": wrong_identity["cookies"]})
    with pool.connection() as connection:
        connection.execute(
            """
            UPDATE ig_sessions
            SET encrypted_cookie_blob=%s, captured_at=now()
            WHERE ig_account_id=%s
            """,
            (encrypted, account_uuid),
        )
    assert client.post(f"/v1/accounts/{account_id}/resume").status_code == 409

    assert client.post("/v1/accounts/session", json=payload).status_code == 200
    resumed = client.post(f"/v1/accounts/{account_id}/resume")
    assert resumed.status_code == 200
    assert resumed.json()["health"] == "active"


def test_validation_errors_and_auth_failures_do_not_echo_secrets(control) -> None:
    client, _, _, _, _ = control
    payload = _session_payload("invalid_cookie_owner")
    secret = "invalid-secret-" + ("x" * 16_384)
    payload["cookies"][0]["value"] = secret
    response = client.post("/v1/accounts/session", json=payload)
    assert response.status_code == 422
    assert secret not in response.text

    client.app.dependency_overrides.pop(current_user_id)
    account_id = uuid.uuid4()
    assert client.get(f"/v1/accounts/{account_id}").status_code == 401
    assert (
        client.get(
            f"/v1/accounts/{account_id}",
            headers={
                "X-Internal-API-Key": "wrong-key",
                "X-User-ID": str(uuid.uuid4()),
            },
        ).status_code
        == 401
    )
    assert (
        client.get(
            f"/v1/accounts/{account_id}",
            headers={
                "X-Internal-API-Key": "internal-test-key",
                "X-User-ID": "not-a-uuid",
            },
        ).status_code
        == 401
    )


def test_invalid_session_key_fails_before_app_startup(database_url: str) -> None:
    pool = create_pool(database_url, min_size=1, max_size=1)
    settings = Settings(
        DATABASE_URL=database_url,
        SESSION_ENCRYPTION_KEY="invalid$key",
        INTERNAL_API_KEY="internal-test-key",
        _env_file=None,
    )
    invalid_app = create_app(settings=settings, pool=pool)
    try:
        with pytest.raises(ValueError, match="32 bytes"):
            with TestClient(invalid_app):
                pytest.fail("invalid key allowed application startup")
    finally:
        pool.close()


def test_capture_seeds_an_agent_config_so_the_account_can_be_scheduled(
    control,
) -> None:
    """The scheduler and worker both inner-join agent_configs.

    Captured without one, an account is silently never scheduled and a manual
    job dies with config_missing, so the row has to exist from the start.
    """

    client, pool, _, user_a, _ = control
    payload = _session_payload("config_seeded")
    _set_session_identity(payload, "9100")

    assert client.post("/v1/accounts/session", json=payload).status_code == 201

    with pool.connection() as connection:
        row = connection.execute(
            """
            SELECT config.enabled
            FROM agent_configs config
            JOIN ig_accounts account ON account.id = config.ig_account_id
            WHERE account.user_id = %(user_id)s AND account.ig_user_id = '9100'
            """,
            {"user_id": user_a},
        ).fetchone()

    assert row is not None, "capture must seed an agent_configs row"
    assert row["enabled"] is False


def test_capture_refuses_to_exceed_the_workspace_account_limit(control, monkeypatch) -> None:
    """The capture path bypassed the seat check the web path enforces."""

    client, pool, _, user_a, _ = control
    with pool.connection() as connection:
        connection.execute(
            "UPDATE users SET plan = 'starter' WHERE id = %s", (user_a,)
        )

    monkeypatch.setattr("engagemate.db.accounts.account_seats", lambda plan: 1)

    first = _session_payload("seat_one")
    _set_session_identity(first, "9200")
    assert client.post("/v1/accounts/session", json=first).status_code == 201

    second = _session_payload("seat_two")
    _set_session_identity(second, "9201")
    refused = client.post("/v1/accounts/session", json=second)
    assert refused.status_code == 402, refused.text

    # Recapturing an account they already own still works.
    assert client.post("/v1/accounts/session", json=first).status_code == 200

    with pool.connection() as connection:
        owned = connection.execute(
            "SELECT count(*) AS owned FROM ig_accounts WHERE user_id = %s AND ig_user_id IN ('9200','9201')",
            (user_a,),
        ).fetchone()
    assert owned["owned"] == 1
