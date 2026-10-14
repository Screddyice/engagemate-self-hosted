"""Database transactions for hosted login, using disposable PostgreSQL."""
from __future__ import annotations

import base64
import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from engagemate.config import Settings
from engagemate.control.routes.accounts import SessionCapture
from engagemate.db.hosted_login import HostedLoginError, HostedLoginRepository
from engagemate.db.pool import create_pool
from engagemate.db.text import text
from engagemate.db.proxies import ProxyBindingError
from engagemate.proxy.base import Geo, ProxyHealth
from engagemate.security.session_cipher import SessionCipher
from test_accounts_api import SCHEMA_SQL, _session_payload

ROOT = Path(__file__).resolve().parents[2]
GEO = Geo('US', 'NY', 'New York')
PROXY_SCHEMA = """
CREATE TABLE proxies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), provider text NOT NULL,
    provider_reference text NOT NULL, proxy_type text NOT NULL,
    country text NOT NULL, region text, city text, host text NOT NULL, port integer NOT NULL,
    encrypted_username text NOT NULL, encrypted_password text NOT NULL,
    credential_scope text NOT NULL DEFAULT 'account', health_status text NOT NULL DEFAULT 'active',
    last_checked_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
"""


@pytest.fixture()
def repo():
    if subprocess.run(['pg_isready'], capture_output=True).returncode:
        pytest.skip('PostgreSQL unavailable')
    name = f'hosted_login_{os.getpid()}_{uuid4().hex[:8]}'
    subprocess.run(['createdb', name], check=True, capture_output=True)
    url = f'postgresql://localhost/{name}'
    schema = SCHEMA_SQL.replace('CREATE TABLE proxies (\n    id uuid PRIMARY KEY DEFAULT gen_random_uuid()\n);', PROXY_SCHEMA)
    with psycopg.connect(url, autocommit=True) as c:
        c.execute(schema)
        c.execute((ROOT / 'app/src/db/migrations/0025_hosted_login.sql').read_text())
        c.execute('CREATE UNIQUE INDEX active_proxy ON proxy_bindings(proxy_id) WHERE released_at IS NULL; CREATE UNIQUE INDEX active_account ON proxy_bindings(ig_account_id) WHERE released_at IS NULL;')
    pool = create_pool(url, min_size=1, max_size=8)
    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip('=')
    settings = Settings(DATABASE_URL=url, SESSION_ENCRYPTION_KEY=key, INTERNAL_API_KEY='test',
        IPROYAL_MODE='inventory', PROXY_HEALTH_URL='https://health.invalid',
        FINGERPRINT_GEO_MAP_JSON=json.dumps([dict(country='US', region='NY', city='New York', timezone='America/New_York', locale='en-US')]),
        PROXY_GEO_ANCHORS_JSON=json.dumps([dict(country='US', region='NY', city='New York', latitude=40.7128, longitude=-74.0060)]), _env_file=None)
    repository = HostedLoginRepository(pool, settings)
    repository.provider.check = lambda endpoint: ProxyHealth.healthy()
    users = [uuid4(), uuid4()]
    with pool.connection() as c:
        for user in users:
            c.execute('INSERT INTO users(id) VALUES (%s)', (user,))
        proxy = uuid4()
        c.execute("""INSERT INTO proxies(id, provider, provider_reference, proxy_type, country, region, city, host, port, encrypted_username, encrypted_password, credential_scope)
            VALUES (%s, 'iproyal', 'paid-1', 'isp_static_residential', 'US','NY','New York','203.0.113.1',12345,%s,%s,'inventory')""", (proxy, repository.proxies._encrypt('user', proxy), repository.proxies._encrypt('secret', proxy)))
    try:
        yield repository, users, proxy
    finally:
        pool.close()
        subprocess.run(['dropdb', '--force', name], capture_output=True)


def capture(username='target_owner', identity='123456'):
    data = _session_payload(username)
    data['ig_user_id'] = identity
    data['login_geo'] = dict(country='US', region='NY', city='New York')
    data['cookies'] = [c for c in data['cookies'] if c['name'] != 'ds_user_id']
    data['cookies'].append(dict(name='ds_user_id', value=identity, domain='.instagram.com', path='/', secure=True, httpOnly=False, sameSite='Lax'))
    return SessionCapture(**data)


def reserve(repo, user):
    return repo.reserve(user, 'target_owner', 'America/New_York')


def test_reserve_holds_inventory_and_release_restores_it(repo):
    repository, users, proxy = repo
    reservation = reserve(repository, users[0])
    assert reservation.endpoint.id == proxy
    assert 'secret' not in repr(reservation)
    assert repository.proxies.free_leases_by_geo() == {}
    assert repository.proxies.active_provider_references('iproyal') == {'paid-1'}
    repository.release(reservation.id, users[1])
    assert repository.proxies.free_leases_by_geo() == {}
    repository.release(reservation.id, users[0])
    assert repository.proxies.free_leases_by_geo() == {GEO: 1}


def test_expiry_restores_inventory_without_reencrypting(repo):
    repository, users, _ = repo
    reservation = reserve(repository, users[0])
    with repository.pool.connection() as c:
        c.execute("UPDATE hosted_login_reservations SET created_at=now()-interval '15 minutes', expires_at=now()-interval '1 second'")
    assert repository.proxies.free_leases_by_geo() == {GEO: 1}
    next_reservation = reserve(repository, users[1])
    assert next_reservation.endpoint.password == 'secret'
    with pytest.raises(HostedLoginError, match='session_unavailable'):
        repository.complete(reservation.id, users[0], capture())


def test_two_tenants_cannot_reserve_one_lease(repo):
    repository, users, _ = repo
    def attempt(user):
        try:
            return reserve(repository, user)
        except HostedLoginError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, users))
    assert sum(not isinstance(result, str) for result in results) == 1


def test_completion_encrypts_and_binds_reserved_endpoint_atomically(repo):
    repository, users, proxy = repo
    reservation = reserve(repository, users[0])
    result = repository.complete(reservation.id, users[0], capture())
    assert result.id == reservation.account_id
    assert result.proxy_bound
    with repository.pool.connection() as c:
        row = c.execute('SELECT encrypted_cookie_blob FROM ig_sessions').fetchone()
        assert SessionCipher(repository.settings.session_encryption_key.get_secret_value(), result.id).decrypt(text(row['encrypted_cookie_blob']))['cookies']
        assert c.execute('SELECT proxy_id FROM proxy_bindings').fetchone()['proxy_id'] == proxy
        assert c.execute('SELECT enabled FROM agent_configs').fetchone()['enabled'] is False
    again = reserve(repository, users[0])
    assert again.account_id == result.id
    assert again.expected_ig_user_id == '123456'
    assert again.endpoint.password == 'secret'
    repository.complete(again.id, users[0], capture())


@pytest.mark.parametrize('mismatch', ['username', 'identity_cookie', 'tenant'])
def test_mismatch_does_not_write_account_or_session(repo, mismatch):
    repository, users, _ = repo
    reservation = reserve(repository, users[0])
    payload = capture('other' if mismatch == 'username' else 'target_owner')
    if mismatch == 'identity_cookie':
        payload.cookies[-1].value = '9999'
    with pytest.raises(HostedLoginError):
        repository.complete(reservation.id, users[1] if mismatch == 'tenant' else users[0], payload)
    with repository.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM ig_accounts').fetchone()['n'] == 0
        assert c.execute('SELECT count(*) AS n FROM ig_sessions').fetchone()['n'] == 0


def test_failure_after_session_write_rolls_back_everything(repo, monkeypatch):
    repository, users, _ = repo
    reservation = reserve(repository, users[0])
    monkeypatch.setattr(repository.proxies, '_encrypt', lambda *args: (_ for _ in ()).throw(ValueError('secret')))
    with pytest.raises(HostedLoginError, match='connection_unavailable'):
        repository.complete(reservation.id, users[0], capture())
    with repository.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM ig_accounts').fetchone()['n'] == 0
        assert c.execute('SELECT count(*) AS n FROM ig_sessions').fetchone()['n'] == 0
        assert c.execute('SELECT released_at FROM hosted_login_reservations').fetchone()['released_at'] is None


def test_proxy_health_and_geography_fail_closed(repo):
    repository, users, _ = repo
    with pytest.raises(HostedLoginError, match='geography_unavailable'):
        repository.reserve(users[0], 'target_owner', 'Asia/Tokyo')
    repository.provider.check = lambda endpoint: ProxyHealth.degraded('connection_failed')
    with pytest.raises(HostedLoginError, match='proxy_unavailable'):
        reserve(repository, users[0])
    assert repository.proxies.free_leases_by_geo() == {GEO: 1}


def test_transient_proxy_health_gets_one_bounded_retry(repo):
    repository, users, _ = repo
    calls = 0

    def check(_endpoint):
        nonlocal calls
        calls += 1
        return (
            ProxyHealth.degraded("connection_failed")
            if calls == 1
            else ProxyHealth.healthy()
        )

    repository.provider.check = check
    reservation = reserve(repository, users[0])
    assert reservation.endpoint.id is not None
    assert calls == 2


def test_proxy_authentication_failure_is_not_retried(repo):
    repository, users, _ = repo
    calls = 0

    def check(_endpoint):
        nonlocal calls
        calls += 1
        return ProxyHealth.degraded("proxy_authentication_failed")

    repository.provider.check = check
    with pytest.raises(HostedLoginError, match="proxy_unavailable"):
        reserve(repository, users[0])
    assert calls == 1



def test_existing_allocator_cannot_claim_reserved_inventory(repo):
    repository, users, _ = repo
    reserve(repository, users[0])
    account, _ = repository.accounts.upsert_session(user_id=users[1], username='different', ig_user_id='555', cookies=[], user_agent='test', login_country=GEO.country, login_region=GEO.region, login_city=GEO.city, expires_at=None)
    with pytest.raises(ProxyBindingError, match='inventory unavailable'):
        repository.proxies.get_or_create(user_id=users[1], ig_account_id=account.id, geo=GEO, allocate=repository.provider.allocate_static, provider_name='iproyal')


def test_import_lease_held_from_configured_allocator(repo):
    from pydantic import SecretStr
    from engagemate.proxy.iproyal import IPRoyalProvider
    repository, users, _ = repo
    with repository.pool.connection() as c:
        c.execute('DELETE FROM proxies')
    repository.settings.iproyal_mode = 'import'
    repository.settings.iproyal_isp_leases_json = SecretStr(json.dumps([dict(reference='import-paid', host='203.0.113.9', port=12345, username='user', password='secret', country='US', region='NY', city='New York')]))
    repository.provider = IPRoyalProvider.from_settings(repository.settings)
    repository.provider.check = lambda endpoint: ProxyHealth.healthy()
    reservation = reserve(repository, users[0])
    assert repository.provider.free_leases_by_geo(excluded_references=repository.proxies.active_provider_references('iproyal')) == {}
    repository.release(reservation.id, users[0])
    again = reserve(repository, users[1])
    assert again.endpoint.id == reservation.endpoint.id
    assert again.endpoint.password == 'secret'


@pytest.mark.parametrize('health,paused,expected', [('challenged', False, 'active'), ('degraded', False, 'active'), ('paused', True, 'paused'), ('banned', False, 'banned')])
def test_verified_reconnect_preserves_manual_holds(repo, health, paused, expected):
    repository, users, _ = repo
    reservation = reserve(repository, users[0])
    result = repository.complete(reservation.id, users[0], capture())
    with repository.pool.connection() as c:
        c.execute('UPDATE ig_accounts SET health_status = %s, paused_at = CASE WHEN %s THEN now() ELSE NULL END', (health, paused))
    reconnect = reserve(repository, users[0])
    assert repository.complete(reconnect.id, users[0], capture()).health == expected


def test_reconnect_identity_cannot_change(repo):
    repository, users, _ = repo
    first = reserve(repository, users[0])
    repository.complete(first.id, users[0], capture())
    reconnect = reserve(repository, users[0])
    with pytest.raises(HostedLoginError, match='identity_mismatch'):
        repository.complete(reconnect.id, users[0], capture(identity='9999'))


def test_reconnect_can_pin_the_existing_account_id(repo):
    repository, users, _ = repo
    first = reserve(repository, users[0])
    account = repository.complete(first.id, users[0], capture())
    reconnect = repository.reserve(
        users[0], account.username.upper(), 'America/New_York', account_id=account.id,
    )
    assert repository.complete(reconnect.id, users[0], capture()).id == account.id


def test_seat_limit_rechecked_on_complete(repo, monkeypatch):
    monkeypatch.setattr("engagemate.db.hosted_login.account_seats", lambda plan: 1)
    repository, users, _ = repo
    reservation = reserve(repository, users[0])
    repository.accounts.upsert_session(user_id=users[0], username='different', ig_user_id='555', cookies=[], user_agent='test', login_country=GEO.country, login_region=GEO.region, login_city=GEO.city, expires_at=None)
    with pytest.raises(HostedLoginError, match='account_limit'):
        repository.complete(reservation.id, users[0], capture())


def test_cancelled_reservation_cannot_finish(repo):
    repository, users, _ = repo
    reservation = reserve(repository, users[0])
    repository.release(reservation.id, users[0])
    with pytest.raises(HostedLoginError, match='session_unavailable'):
        repository.complete(reservation.id, users[0], capture())


def test_reservation_races_existing_allocator_without_double_lease(repo):
    repository, users, _ = repo
    account, _ = repository.accounts.upsert_session(user_id=users[1], username='different', ig_user_id='555', cookies=[], user_agent='test', login_country=GEO.country, login_region=GEO.region, login_city=GEO.city, expires_at=None)
    def bind():
        try:
            return repository.proxies.get_or_create(user_id=users[1], ig_account_id=account.id, geo=GEO, allocate=repository.provider.allocate_static, provider_name='iproyal')
        except ProxyBindingError:
            return None
    def start():
        try:
            return reserve(repository, users[0])
        except HostedLoginError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(fn) for fn in (bind, start)]
        assert sum(f.result() is not None for f in results) == 1


def test_reconnect_prevents_operator_rebind_during_login(repo):
    repository, users, _ = repo
    reservation = reserve(repository, users[0])
    account = repository.complete(reservation.id, users[0], capture())
    reserve(repository, users[0])
    with pytest.raises(ProxyBindingError, match='login is in progress'):
        repository.proxies.rebind(user_id=users[0], ig_account_id=account.id, geo=GEO, reason='test', allocate=repository.provider.allocate_static, check=repository.provider.check, provider_name='iproyal')


def test_health_oracle_runs_after_reservation_commits_without_inventory_locks(repo):
    repository, users, _ = repo
    def check(endpoint):
        with repository.pool.connection() as c:
            c.execute("SET LOCAL lock_timeout = '200ms'")
            # A separate transaction must see the claim and acquire these locks.
            row = c.execute("SELECT id FROM hosted_login_reservations WHERE released_at IS NULL").fetchone()
            assert row is not None
            repository._lock_user(c, users[0])
            repository.proxies._lock_provider_inventory(c, 'iproyal')
            assert repository.proxies._reserved_proxy_ids(c) == [endpoint.id]
        return ProxyHealth.healthy()
    repository.provider.check = check
    assert reserve(repository, users[0]).endpoint.id is not None


@pytest.mark.parametrize('raises', [False, True])
def test_failed_health_releases_committed_reservation(repo, raises):
    repository, users, _ = repo
    def check(endpoint):
        if raises:
            raise RuntimeError('private provider failure')
        return ProxyHealth.degraded('connection_failed')
    repository.provider.check = check
    with pytest.raises(HostedLoginError):
        reserve(repository, users[0])
    with repository.pool.connection() as c:
        assert c.execute('SELECT released_at FROM hosted_login_reservations').fetchone()['released_at'] is not None
    assert repository.proxies.free_leases_by_geo() == {GEO: 1}


def test_atomic_completion_receipt_is_owned_and_survives_restart(repo):
    repository, users, _ = repo
    reservation = reserve(repository, users[0])
    assert repository.completed(reservation.id, users[0]) is None
    result = repository.complete(reservation.id, users[0], capture())
    receipt = repository.completed(reservation.id, users[0])
    assert receipt['state'] == 'connected'
    assert receipt['account']['id'] == str(result.id)
    assert repository.completed(reservation.id, users[1]) is None


def test_cancelled_reconnect_cannot_reuse_an_older_completion(repo):
    repository, users, _ = repo
    original = reserve(repository, users[0])
    repository.complete(original.id, users[0], capture())
    cancelled = reserve(repository, users[0])
    repository.release(cancelled.id, users[0])
    assert repository.completed(cancelled.id, users[0]) is None
