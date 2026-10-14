"""Profile capture persistence and browser exclusion against disposable Postgres."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import psycopg
import pytest
from engagemate.browser.lease import BrowserBusy, LeasedBrowserFactory, browser_lease
from engagemate.db.pool import create_pool
from engagemate.onboarding.profile_capture import (
    ProfileCaptureError,
    ProfileCaptureService,
)
from engagemate.worker.follower_browser import FollowerBrowserError

SCHEMA = """
CREATE TABLE ig_accounts (
 id uuid PRIMARY KEY, user_id uuid NOT NULL, health_status text NOT NULL DEFAULT 'active',
 paused_at timestamptz, pause_reason text, updated_at timestamptz DEFAULT now(),
 UNIQUE(id,user_id)
);
CREATE TABLE account_health_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL, ig_account_id uuid NOT NULL,
 event_type text NOT NULL, severity text NOT NULL, evidence jsonb NOT NULL, created_at timestamptz DEFAULT now()
);
CREATE TABLE brand_profiles (
 id uuid PRIMARY KEY, user_id uuid NOT NULL, ig_account_id uuid NOT NULL,
 FOREIGN KEY(ig_account_id,user_id) REFERENCES ig_accounts(id,user_id) ON DELETE CASCADE
);
CREATE TABLE brand_reference_accounts (
 id uuid PRIMARY KEY, brand_profile_id uuid NOT NULL REFERENCES brand_profiles(id) ON DELETE CASCADE,
 username text NOT NULL
);
"""
NAMES = ["sourceone", "sourcetwo", "sourcethree"]


def public_profile(username):
    return {
        "username": username,
        "fullName": username,
        "biography": "A public profile",
        "followersCount": 100,
        "followingCount": 10,
        "postsCount": 5,
        "isPrivate": False,
        "isVerified": False,
        "latestPosts": [],
    }


@pytest.fixture(scope="module")
def database_url():
    if (
        not shutil.which("pg_isready")
        or subprocess.run(["pg_isready"], capture_output=True, check=False).returncode
    ):
        pytest.skip("local PostgreSQL is unavailable")
    database = f"engagemate_profile_capture_{os.getpid()}_{uuid4().hex[:8]}"
    subprocess.run(
        ["createdb", "--template=template0", "--encoding=UTF8", database],
        check=True,
        capture_output=True,
    )
    url = f"postgresql://localhost/{database}"
    try:
        with psycopg.connect(url, autocommit=True) as connection:
            connection.execute(SCHEMA)
            migration = (
                Path(__file__).resolve().parents[2]
                / "app/src/db/migrations/0019_profile_capture.sql"
            )
            connection.execute(migration.read_text())
        yield url
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", "--force", database],
            check=True,
            capture_output=True,
        )


@pytest.fixture
def world(database_url):
    pool = create_pool(database_url, min_size=1, max_size=6)
    user, account, other_user, other_account = (uuid4() for _ in range(4))
    with pool.connection() as connection:
        for uid, aid in [(user, account), (other_user, other_account)]:
            profile = uuid4()
            connection.execute(
                "INSERT INTO ig_accounts(id,user_id) VALUES (%s,%s)", (aid, uid)
            )
            connection.execute(
                "INSERT INTO brand_profiles VALUES (%s,%s,%s)", (profile, uid, aid)
            )
            for name in NAMES:
                connection.execute(
                    "INSERT INTO brand_reference_accounts VALUES (%s,%s,%s)",
                    (uuid4(), profile, name),
                )
    yield SimpleNamespace(
        pool=pool,
        user=user,
        account=account,
        other_user=other_user,
        other_account=other_account,
        service=ProfileCaptureService(pool, object()),
    )
    with pool.connection() as connection:
        connection.execute(
            "DELETE FROM ig_accounts WHERE id=ANY(%s)", ([account, other_account],)
        )
    pool.close()


def state(world):
    with world.pool.connection() as connection:
        return connection.execute(
            "SELECT * FROM profile_check_state WHERE user_id=%s AND ig_account_id=%s",
            (world.user, world.account),
        ).fetchone()


def account_row(world):
    with world.pool.connection() as connection:
        return connection.execute(
            "SELECT * FROM ig_accounts WHERE id=%s", (world.account,)
        ).fetchone()


def test_saved_cache_survives_restart_preserves_scope_and_expires(world):
    service = world.service
    service._save(world.user, world.account, NAMES[0], public_profile(NAMES[0]))
    restarted = ProfileCaptureService(world.pool, object())
    assert restarted._cached(world.user, world.account, NAMES) == {
        NAMES[0]: public_profile(NAMES[0])
    }
    assert restarted._cached(world.other_user, world.other_account, NAMES) == {}
    with world.pool.connection() as connection:
        connection.execute(
            "UPDATE onboarding_profile_cache SET captured_at=now()-interval '7 hours' WHERE ig_account_id=%s",
            (world.account,),
        )
    assert restarted._cached(world.user, world.account, NAMES) == {}
    updated = {**public_profile(NAMES[0]), "followersCount": 222}
    restarted._save(world.user, world.account, NAMES[0], updated)
    assert (
        restarted._cached(world.user, world.account, NAMES)[NAMES[0]]["followersCount"]
        == 222
    )


@pytest.mark.parametrize(
    "names", [["unapproved", "sourcetwo", "sourcethree"], NAMES[:2]]
)
def test_cache_rejects_changed_source_list(world, names):
    with pytest.raises(ProfileCaptureError, match="source_accounts_changed"):
        world.service._cached(world.user, world.account, names)


def test_other_tenant_cannot_read_or_reserve_owned_account(world):
    with pytest.raises(ProfileCaptureError, match="source_accounts_changed"):
        world.service._cached(world.other_user, world.account, NAMES)
    with pytest.raises(psycopg.IntegrityError):
        world.service._reserve(world.other_user, world.account)


def test_reservation_cooldown_is_durable_and_refreshes_only_after_expiry(world):
    world.service._reserve(world.user, world.account)
    initial = state(world)
    assert datetime.now(UTC) + timedelta(seconds=115) < initial["next_allowed_at"]
    with pytest.raises(ProfileCaptureError, match="profile_check_cooldown") as failure:
        ProfileCaptureService(world.pool, object())._reserve(world.user, world.account)
    assert failure.value.retry_after == 120
    assert state(world)["next_allowed_at"] == initial["next_allowed_at"]
    with world.pool.connection() as connection:
        connection.execute(
            "UPDATE profile_check_state SET next_allowed_at=now()-interval '1 second', failure_reason='old' WHERE ig_account_id=%s",
            (world.account,),
        )
    world.service._reserve(world.user, world.account)
    assert state(world)["failure_reason"] is None


@pytest.mark.parametrize(
    "restricted,seconds,health", [(True, 1800, "paused"), (False, 120, "active")]
)
def test_failures_persist_cooldown_and_pause_only_restricted_account(
    world, restricted, seconds, health
):
    world.service._reserve(world.user, world.account)
    world.service._failure(
        world.user,
        world.account,
        "rate_limited" if restricted else "profile_unreadable",
        restricted,
    )
    assert (
        datetime.now(UTC) + timedelta(seconds=seconds - 5)
        < state(world)["next_allowed_at"]
    )
    row = account_row(world)
    assert row["health_status"] == health
    if restricted:
        assert row["pause_reason"] == "rate_limit"
        assert row["paused_at"] is not None
        with world.pool.connection() as connection:
            event = connection.execute(
                "SELECT * FROM account_health_events WHERE user_id=%s AND ig_account_id=%s",
                (world.user, world.account),
            ).fetchone()
        assert event["event_type"] == "rate_limit"
        assert event["created_at"] is not None
    with world.pool.connection() as connection:
        other = connection.execute(
            "SELECT health_status FROM ig_accounts WHERE id=%s", (world.other_account,)
        ).fetchone()
    assert other["health_status"] == "active"


def test_browser_lease_is_shared_across_pools_and_profile_checks_globally(
    world, database_url
):
    second = create_pool(database_url, min_size=1, max_size=3)
    try:
        with browser_lease(world.pool, world.account):
            with pytest.raises(BrowserBusy), browser_lease(second, world.account):
                pytest.fail("same-account browser must be excluded")
            with browser_lease(second, world.other_account):
                pass
        with (
            browser_lease(world.pool, world.account, profile_capture=True),
            pytest.raises(BrowserBusy),
            browser_lease(second, world.other_account, profile_capture=True),
        ):
            pytest.fail("onboarding browser cap must be shared")
        with browser_lease(second, world.account, profile_capture=True):
            pass
    finally:
        second.close()


@pytest.mark.asyncio
async def test_leased_factories_exclude_same_account_and_release_on_exception(world):
    context = SimpleNamespace(ig_account_id=world.account)
    events = []

    class Factory:
        @asynccontextmanager
        async def open(self, context):
            events.append("opened")
            try:
                yield object()
            finally:
                events.append("closed")

    first = LeasedBrowserFactory(world.pool, Factory())
    second = LeasedBrowserFactory(world.pool, Factory())
    with pytest.raises(ValueError, match="test failure"):
        async with first.open(context):
            with pytest.raises(BrowserBusy):
                async with second.open(context):
                    pytest.fail("second factory opened the same session")
            raise ValueError("test failure")
    async with second.open(context):
        pass
    assert events == ["opened", "closed", "opened", "closed"]


def fake_browser_service(world, *, fail_at=None, failure=None, hang=False):
    events = []
    page = object()
    playwright = object()
    context = SimpleNamespace(
        ig_account_id=world.account, endpoint=object(), cookies=object()
    )

    @asynccontextmanager
    async def playwright_factory():
        events.append("playwright_open")
        try:
            yield playwright
        finally:
            events.append("playwright_close")

    class BrowserFactory:
        def __init__(self, **kwargs):
            assert kwargs == {"playwright": playwright}

        @asynccontextmanager
        async def open(self, supplied):
            assert supplied is context
            events.append("browser_open")
            try:
                yield SimpleNamespace(
                    context=SimpleNamespace(new_page=AsyncMock(return_value=page))
                )
            finally:
                events.append("browser_close")

    class Adapter:
        def __init__(self, supplied):
            assert supplied is page

        async def public_profile(self, name):
            events.append(("profile", name))
            if hang:
                await asyncio.Event().wait()
            if name == fail_at:
                raise failure
            return public_profile(name)

    async def sleep(seconds):
        events.append(("sleep", seconds))

    service = ProfileCaptureService(
        world.pool,
        object(),
        playwright_factory=playwright_factory,
        browser_factory=BrowserFactory,
        adapter_factory=Adapter,
        sleep=sleep,
    )
    service._load = Mock(return_value=context)
    return service, events


@pytest.mark.asyncio
async def test_capture_uses_bound_context_paces_calls_and_cache_avoids_browser(world):
    service, events = fake_browser_service(world)
    assert await service.capture(world.user, world.account, NAMES) == [
        public_profile(name) for name in NAMES
    ]
    service._load.assert_called_once_with(world.user, world.account)
    assert events == [
        "playwright_open",
        "browser_open",
        ("profile", NAMES[0]),
        ("sleep", 5),
        ("profile", NAMES[1]),
        ("sleep", 5),
        ("profile", NAMES[2]),
        "browser_close",
        "playwright_close",
    ]
    events.clear()
    assert await service.capture(world.user, world.account, list(reversed(NAMES))) == [
        public_profile(name) for name in reversed(NAMES)
    ]
    assert events == []
    assert service._load.call_count == 1


@pytest.mark.asyncio
async def test_wrong_tenant_cannot_open_browser(world):
    service, events = fake_browser_service(world)
    with pytest.raises(ProfileCaptureError, match="source_accounts_changed"):
        await service.capture(world.other_user, world.account, NAMES)
    assert events == []
    service._load.assert_not_called()


@pytest.mark.asyncio
async def test_restriction_stops_remaining_reads_keeps_success_and_releases_context(
    world,
):
    service, events = fake_browser_service(
        world, fail_at=NAMES[1], failure=FollowerBrowserError("rate_limited")
    )
    with pytest.raises(ProfileCaptureError, match="rate_limited") as failure:
        await service.capture(world.user, world.account, NAMES)
    assert failure.value.retry_after == 1800
    assert ("profile", NAMES[2]) not in events
    assert events[-2:] == ["browser_close", "playwright_close"]
    assert service._cached(world.user, world.account, NAMES) == {
        NAMES[0]: public_profile(NAMES[0])
    }
    assert state(world)["next_allowed_at"] > datetime.now(UTC) + timedelta(minutes=29)
    assert account_row(world)["health_status"] == "paused"
    with browser_lease(world.pool, world.account, profile_capture=True):
        pass


@pytest.mark.asyncio
async def test_timeout_closes_resources_releases_lease_and_persists_retry_window(
    world, monkeypatch
):
    service, events = fake_browser_service(world, hang=True)
    original_timeout = asyncio.timeout
    monkeypatch.setattr(
        "engagemate.onboarding.profile_capture.asyncio.timeout",
        lambda _: original_timeout(0.01),
    )
    with pytest.raises(ProfileCaptureError, match="profile_check_timeout") as failure:
        await service.capture(world.user, world.account, NAMES)
    assert failure.value.retry_after == 120
    assert events[-2:] == ["browser_close", "playwright_close"]
    assert state(world)["failure_reason"] == "profile_check_timeout"
    assert state(world)["next_allowed_at"] > datetime.now(UTC) + timedelta(seconds=115)
    assert account_row(world)["health_status"] == "active"
    with browser_lease(world.pool, world.account, profile_capture=True):
        pass


def load_service(
    monkeypatch, *, provider="iproyal", different_geo=False, binding_health="active"
):
    from contextlib import contextmanager

    import engagemate.onboarding.profile_capture as capture_module
    from engagemate.proxy.base import Geo, ProxyHealth
    from pydantic import SecretStr

    user, account = uuid4(), uuid4()
    geo = Geo("US", "California", "Los Angeles")
    endpoint_geo = Geo("US", "New York", "New York") if different_geo else geo
    endpoint = SimpleNamespace(provider=provider, geo=endpoint_geo, proxy_type="isp")
    session = {
        "health_status": "active",
        "login_country": geo.country,
        "login_region": geo.region,
        "login_city": geo.city,
        "encrypted_cookie_blob": "synthetic-encrypted-payload",
        "captured_user_agent": "captured-agent",
        "captured_at": datetime.now(UTC),
        "expires_at": None,
    }
    binding = {"health_status": binding_health}
    connection = Mock()
    connection.execute.side_effect = [
        SimpleNamespace(fetchone=lambda: session),
        SimpleNamespace(
            fetchone=lambda: binding if binding_health == "active" else None
        ),
    ]

    @contextmanager
    def connect():
        yield connection

    pool = SimpleNamespace(connection=connect)
    settings = SimpleNamespace(session_encryption_key=SecretStr("synthetic-key"))
    proxy_repository = Mock()
    proxy_repository._endpoint_from_row.return_value = endpoint
    monkeypatch.setattr(
        capture_module, "ProxyRepository", Mock(return_value=proxy_repository)
    )
    provider_client = Mock()
    provider_client.check.return_value = ProxyHealth.healthy()
    monkeypatch.setattr(
        capture_module.IPRoyalProvider,
        "from_settings",
        Mock(return_value=provider_client),
    )
    fingerprint_factory = Mock()
    fingerprint_factory.for_account.return_value = object()
    monkeypatch.setattr(
        capture_module.FingerprintFactory,
        "from_settings",
        Mock(return_value=fingerprint_factory),
    )
    cipher = Mock()
    cipher.decrypt.return_value = {"cookies": []}
    cipher_factory = Mock(return_value=cipher)
    monkeypatch.setattr(capture_module, "SessionCipher", cipher_factory)
    service = ProfileCaptureService(pool, settings)
    return SimpleNamespace(
        service=service,
        user=user,
        account=account,
        endpoint=endpoint,
        session=session,
        provider=provider_client,
        connection=connection,
        fingerprint=fingerprint_factory,
        repository=proxy_repository,
        cipher=cipher,
        cipher_factory=cipher_factory,
    )


def test_load_reuses_owned_proxy_and_captured_identity_without_allocating(monkeypatch):
    fake = load_service(monkeypatch)
    context = fake.service._load(fake.user, fake.account)
    assert context.ig_account_id == fake.account
    assert context.endpoint is fake.endpoint
    fake.provider.check.assert_called_once_with(fake.endpoint)
    fake.repository._endpoint_from_row.assert_called_once_with(
        {"health_status": "active"}, fake.account
    )
    assert fake.connection.execute.call_args_list[0].args[1] == (
        fake.user,
        fake.account,
    )
    assert fake.connection.execute.call_args_list[1].args[1] == {
        "user_id": fake.user,
        "account_id": fake.account,
    }
    fingerprint_account, captured_session = fake.fingerprint.for_account.call_args.args
    assert fingerprint_account.geo == fake.endpoint.geo
    assert captured_session.captured_user_agent == "captured-agent"
    fake.cipher_factory.assert_called_once_with("synthetic-key", fake.account)
    fake.cipher.decrypt.assert_called_once_with("synthetic-encrypted-payload")
    assert context.cookies.consume() == []
    fake.repository.ensure_binding.assert_not_called()


@pytest.mark.parametrize("reason", ["proxy_access_denied", "connection_failed"])
def test_load_retries_transient_proxy_health_once(monkeypatch, reason):
    import engagemate.onboarding.profile_capture as capture_module
    from engagemate.proxy.base import ProxyHealth

    fake = load_service(monkeypatch)
    monkeypatch.setattr(capture_module.time, "sleep", Mock())
    fake.provider.check.side_effect = [
        ProxyHealth.degraded(reason),
        ProxyHealth.healthy(),
    ]
    context = fake.service._load(fake.user, fake.account)
    assert context.ig_account_id == fake.account
    assert fake.provider.check.call_args_list == [
        ((fake.endpoint,),),
        ((fake.endpoint,),),
    ]
    capture_module.time.sleep.assert_called_once_with(1.0)


@pytest.mark.parametrize("mutation", ["paused", "expired", "old"])
def test_load_rejects_unhealthy_or_stale_session_before_proxy_checks(
    monkeypatch, mutation
):
    fake = load_service(monkeypatch)
    if mutation == "paused":
        fake.session["health_status"] = "paused"
    elif mutation == "expired":
        fake.session["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    else:
        fake.session["captured_at"] = datetime.now(UTC) - timedelta(days=365)
    with pytest.raises(ProfileCaptureError, match="reconnect_required"):
        fake.service._load(fake.user, fake.account)
    fake.provider.check.assert_not_called()


@pytest.mark.parametrize(
    "options",
    [{"provider": "other"}, {"different_geo": True}, {"binding_health": "degraded"}],
)
def test_load_rejects_mismatched_or_unhealthy_bound_proxy_before_health_check(
    monkeypatch, options
):
    fake = load_service(monkeypatch, **options)
    with pytest.raises(ProfileCaptureError):
        fake.service._load(fake.user, fake.account)
    fake.provider.check.assert_not_called()
    fake.fingerprint.for_account.assert_not_called()
    fake.cipher.decrypt.assert_not_called()
    if options.get("binding_health"):
        assert (
            "proxy.health_status = 'active'"
            in fake.connection.execute.call_args_list[1].args[0]
        )
        fake.repository._endpoint_from_row.assert_not_called()
