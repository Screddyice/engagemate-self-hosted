"""Pre-flight health checking for unbound proxy inventory."""

import base64
import os
import uuid

import pytest
from engagemate.db.proxy_capacity import InventoryProxy
from engagemate.proxy.base import Geo, ProxyEndpoint, ProxyHealth, ProxyHealthStatus
from engagemate.proxy.healthcheck_inventory import check_inventory
from engagemate.security.session_cipher import SessionCipher


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")


USERNAME = "lease-user-9f3c1d"
PASSWORD = "lease-pass-4b7e20"


def _proxy(key, *, host="203.0.113.10", geo=None, health="active", broken=False):
    proxy_id = uuid.uuid4()
    cipher = SessionCipher(key, proxy_id)
    return InventoryProxy(
        id=proxy_id,
        provider="iproyal",
        provider_reference=f"order:1:{host}",
        proxy_type="isp_static_residential",
        geo=geo or Geo("US", "AZ", "Phoenix"),
        host=host,
        port=12323,
        encrypted_username=(
            "v1.not-a-real-envelope"
            if broken
            else cipher.encrypt({"value": USERNAME})
        ),
        encrypted_password=cipher.encrypt({"value": PASSWORD}),
        health_status=health,
    )


class FakeRepository:
    def __init__(self, proxies):
        self._proxies = tuple(proxies)
        self.marked = []

    def list_unbound_inventory(self):
        return self._proxies

    def mark_inventory_health(self, proxy_id, healthy):
        self.marked.append((proxy_id, healthy))
        return True


class FakeProvider:
    def __init__(self, results):
        self._results = dict(results)
        self.checked = []

    def check(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        self.checked.append(endpoint.host)
        return self._results[endpoint.host]


def _healthy() -> ProxyHealth:
    return ProxyHealth(status=ProxyHealthStatus.HEALTHY, reason="ok")


def test_a_healthy_lease_still_records_that_it_was_checked():
    """last_checked_at is the whole signal.

    All twenty production leases read active with last_checked_at NULL, so
    "passed" and "never dialled" looked identical. Writing only on a status
    change would have preserved exactly that ambiguity.
    """

    key = _key()
    proxy = _proxy(key)
    repository = FakeRepository([proxy])
    provider = FakeProvider({proxy.host: _healthy()})

    healthy, degraded, recorded = check_inventory(
        repository, provider, key, update=True
    )

    assert (healthy, degraded, recorded) == (1, 0, 1)
    assert repository.marked == [(proxy.id, True)]


def test_a_geo_mismatched_lease_is_marked_degraded():
    key = _key()
    proxy = _proxy(key, host="203.0.113.94")
    repository = FakeRepository([proxy])
    provider = FakeProvider({proxy.host: ProxyHealth.degraded("geo_mismatch")})

    healthy, degraded, recorded = check_inventory(
        repository, provider, key, update=True
    )

    assert (healthy, degraded, recorded) == (0, 1, 1)
    assert repository.marked == [(proxy.id, False)]


def test_reporting_alone_never_writes():
    key = _key()
    proxy = _proxy(key, host="203.0.113.94")
    repository = FakeRepository([proxy])
    provider = FakeProvider({proxy.host: ProxyHealth.degraded("geo_mismatch")})

    healthy, degraded, recorded = check_inventory(repository, provider, key)

    assert (healthy, degraded, recorded) == (0, 1, 0)
    assert repository.marked == []


def test_a_recovered_lease_is_marked_active_again():
    key = _key()
    proxy = _proxy(key, health="degraded")
    repository = FakeRepository([proxy])
    provider = FakeProvider({proxy.host: _healthy()})

    healthy, degraded, recorded = check_inventory(
        repository, provider, key, update=True
    )

    assert (healthy, degraded, recorded) == (1, 0, 1)
    assert repository.marked == [(proxy.id, True)]


def test_an_undecryptable_lease_is_degraded_without_being_checked():
    key = _key()
    proxy = _proxy(key, broken=True)
    repository = FakeRepository([proxy])
    provider = FakeProvider({})

    healthy, degraded, recorded = check_inventory(
        repository, provider, key, update=True
    )

    assert (healthy, degraded, recorded) == (0, 1, 1)
    # Never dialled: a lease we cannot authenticate has nothing to dial with.
    assert provider.checked == []


@pytest.mark.parametrize("broken", [False, True])
def test_no_credential_reaches_the_log(broken, caplog):
    """Every artifact here is derived from a paid lease, so nothing leaks."""

    key = _key()
    proxy = _proxy(key, host="203.0.113.94", broken=broken)
    repository = FakeRepository([proxy])
    provider = FakeProvider({proxy.host: ProxyHealth.degraded("geo_mismatch")})

    with caplog.at_level("DEBUG"):
        check_inventory(repository, provider, key, update=True)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert USERNAME not in logged
    assert PASSWORD not in logged
    assert proxy.encrypted_username not in logged
    assert proxy.encrypted_password not in logged
    # The host and reason are what an operator needs, so they must be present.
    assert proxy.host in logged


def test_every_lease_is_checked_not_just_the_first():
    key = _key()
    first = _proxy(key, host="203.0.113.10")
    second = _proxy(key, host="203.0.113.11", geo=Geo("US", "AL", "Centre"))
    repository = FakeRepository([first, second])
    provider = FakeProvider(
        {first.host: _healthy(), second.host: ProxyHealth.degraded("ip_mismatch")}
    )

    healthy, degraded, _ = check_inventory(repository, provider, key)

    assert (healthy, degraded) == (1, 1)
    assert provider.checked == [first.host, second.host]


@pytest.mark.parametrize("update", [True, False])
def test_empty_inventory_is_not_an_error(update):
    key = _key()

    assert check_inventory(FakeRepository([]), FakeProvider({}), key,
                           update=update) == (0, 0, 0)
