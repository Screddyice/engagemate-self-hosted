from __future__ import annotations

import json
import uuid

import httpx
import pytest
from engagemate.db.proxies import ProxyBindingError
from engagemate.db.proxy_capacity import UnboundAccount
from engagemate.proxy.base import Geo, ProxyEndpoint, ProxyHealth
from engagemate.proxy.capacity import (
    CapacityIntent,
    CapacityPolicy,
    CapacityReconciler,
    CapacitySnapshot,
    GeoCatalogEntry,
    SubmittedCapacityOrder,
    parse_geo_catalog,
    plan_capacity_order,
)
from engagemate.proxy.import_order import (
    ConfirmedOrderSegment,
    ExistingOrderImportError,
    import_confirmed_order_segments,
    import_existing_order,
    parse_confirmed_order_payload,
)
from engagemate.proxy.iproyal_reseller import (
    IPRoyalAPIError,
    IPRoyalResellerClient,
    ProviderOrder,
)
from engagemate.proxy.managed_capacity import ManagedCapacityController
from pydantic import SecretStr


def test_capacity_plan_orders_only_the_shortfall_with_buffer() -> None:
    policy = CapacityPolicy(
        spare_per_geo=2,
        max_order_quantity=10,
        max_order_cost_cents=5_000,
        max_monthly_cost_cents=20_000,
    )

    plan = plan_capacity_order(
        CapacitySnapshot(
            connected_accounts=7,
            confirmed_proxies=5,
            pending_proxies=1,
            monthly_spend_cents=3_000,
        ),
        policy,
    )

    assert plan is not None
    assert plan.quantity == 3


def test_capacity_plan_caps_one_purchase_without_overbuying() -> None:
    policy = CapacityPolicy(
        spare_per_geo=2,
        max_order_quantity=5,
        max_order_cost_cents=5_000,
        max_monthly_cost_cents=20_000,
    )

    plan = plan_capacity_order(
        CapacitySnapshot(
            connected_accounts=40,
            confirmed_proxies=4,
            pending_proxies=0,
            monthly_spend_cents=0,
        ),
        policy,
    )

    assert plan is not None
    assert plan.quantity == 5


def test_capacity_plan_does_nothing_when_demand_and_buffer_are_covered() -> None:
    policy = CapacityPolicy(
        spare_per_geo=2,
        max_order_quantity=10,
        max_order_cost_cents=5_000,
        max_monthly_cost_cents=20_000,
    )

    assert (
        plan_capacity_order(
            CapacitySnapshot(
                connected_accounts=7,
                confirmed_proxies=8,
                pending_proxies=1,
                monthly_spend_cents=3_000,
            ),
            policy,
        )
        is None
    )


def test_geo_catalog_requires_unique_exact_geographies_and_positive_ids() -> None:
    raw = json.dumps(
        [
            {
                "country": "US",
                "region": "CA",
                "city": "San Francisco",
                "product_id": 9,
                "plan_id": 4,
                "location_id": 51,
            }
        ]
    )

    catalog = parse_geo_catalog(raw)

    assert catalog == {
        Geo("US", "CA", "San Francisco"): GeoCatalogEntry(
            product_id=9,
            plan_id=4,
            location_id=51,
        )
    }

    duplicate = json.dumps([json.loads(raw)[0], json.loads(raw)[0]])
    with pytest.raises(ValueError, match="IPRoyal geo catalog is invalid"):
        parse_geo_catalog(duplicate)


def test_iproyal_client_prices_and_creates_balance_order_without_retries() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "price": 12.5,
                    "price_per_item": 2.5,
                    "pre_discount_price": 15,
                    "price_with_vat": 12.5,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 420,
                "status": "in-progress",
                "quantity": 5,
                "proxy_data": {
                    "ports": {"http|https": 12323, "socks5": 12324},
                    "proxies": [],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = IPRoyalResellerClient(
        "provider-secret-token",
        client_factory=lambda **kwargs: httpx.Client(transport=transport, **kwargs),
    )

    quote = client.calculate_pricing(
        product_id=9,
        plan_id=4,
        location_id=51,
        quantity=5,
    )
    order = client.create_balance_order(
        product_id=9,
        plan_id=4,
        location_id=51,
        quantity=5,
    )

    assert quote.total_cents == 1_250
    assert quote.unit_cents == 250
    assert order.order_id == "420"
    assert order.status == "in-progress"
    assert len(requests) == 2
    assert requests[0].headers["X-Access-Token"] == "provider-secret-token"
    assert requests[1].method == "POST"
    payload = json.loads(requests[1].content)
    assert payload == {
        "product_id": 9,
        "product_plan_id": 4,
        "product_location_id": 51,
        "quantity": 5,
        "auto_extend": False,
    }
    assert "card_id" not in payload


def test_iproyal_purchase_failure_is_secret_safe_and_not_retried() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout(
            "upstream contained provider-secret-token and proxy credentials"
        )

    client = IPRoyalResellerClient(
        "provider-secret-token",
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )

    with pytest.raises(IPRoyalAPIError, match="order outcome is unknown") as caught:
        client.create_balance_order(
            product_id=9,
            plan_id=4,
            location_id=51,
            quantity=1,
        )

    assert calls == 1
    assert "provider-secret-token" not in str(caught.value)
    assert "credentials" not in str(caught.value)


def test_confirmed_order_parses_documented_http_proxy_inventory() -> None:
    response = {
        "id": 420,
        "status": "confirmed",
        "quantity": 2,
        "proxy_data": {
            "ports": {"http|https": 12323, "socks5": 12324},
            "proxies": [
                {
                    "ip": "198.51.100.248",
                    "username": "account-user",
                    "password": "account-password",
                },
                {
                    "ip": "198.51.100.249",
                    "username": "account-user",
                    "password": "account-password",
                },
            ],
        },
    }
    client = IPRoyalResellerClient(
        "provider-secret-token",
        client_factory=lambda **kwargs: httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=response)
            ),
            **kwargs,
        ),
    )

    endpoints = client.get_order_endpoints("420", Geo("US", "CA", "San Francisco"))

    assert [endpoint.host for endpoint in endpoints] == [
        "198.51.100.248",
        "198.51.100.249",
    ]
    assert all(endpoint.port == 12323 for endpoint in endpoints)
    assert all(endpoint.provider == "iproyal" for endpoint in endpoints)
    assert all(
        endpoint.geo == Geo("US", "CA", "San Francisco") for endpoint in endpoints
    )
    assert endpoints[0].provider_reference == "order:420:198.51.100.248"


class _CapacityRepository:
    def __init__(self, snapshot: CapacitySnapshot) -> None:
        self.snapshot = snapshot
        self.submitted: list[tuple[str, str, str]] = []
        self.unknown: list[str] = []
        self.awaiting: list[SubmittedCapacityOrder] = []
        self.confirmed: list[tuple[str, tuple[object, ...], str]] = []
        self.confirmed_segments: list[tuple[str, Geo, tuple[object, ...], str]] = []
        self.failed: list[tuple[str, str]] = []
        self.registered: list[tuple[str, Geo, GeoCatalogEntry, int, int]] = []

    def get_snapshot(self, _geo: Geo) -> CapacitySnapshot:
        return self.snapshot

    def reserve_submission(
        self,
        geo: Geo,
        entry: GeoCatalogEntry,
        policy: CapacityPolicy,
        quantity: int,
        quoted_cost_cents: int,
    ) -> CapacityIntent | None:
        plan = plan_capacity_order(self.snapshot, policy)
        if plan is None or plan.quantity != quantity:
            return None
        if quoted_cost_cents > policy.max_order_cost_cents:
            return None
        if (
            self.snapshot.monthly_spend_cents + quoted_cost_cents
            > policy.max_monthly_cost_cents
        ):
            return None
        return CapacityIntent(
            intent_id="intent-1",
            geo=geo,
            catalog=entry,
            quantity=quantity,
            quoted_cost_cents=quoted_cost_cents,
        )

    def mark_submitted(
        self, intent_id: str, provider_order_id: str, provider_status: str
    ) -> None:
        self.submitted.append((intent_id, provider_order_id, provider_status))

    def mark_unknown(self, intent_id: str) -> None:
        self.unknown.append(intent_id)

    def list_submitted_orders(self) -> tuple[SubmittedCapacityOrder, ...]:
        return tuple(self.awaiting)

    def confirm_order(self, provider_order_id, endpoints, encryption_key) -> None:
        self.confirmed.append((provider_order_id, tuple(endpoints), encryption_key))

    def confirm_order_segment(
        self, provider_order_id, geo, endpoints, encryption_key
    ) -> None:
        self.confirmed_segments.append(
            (provider_order_id, geo, tuple(endpoints), encryption_key)
        )

    def mark_provider_failed(self, provider_order_id: str, status: str) -> None:
        self.failed.append((provider_order_id, status))

    def register_existing_order(
        self,
        *,
        provider_order_id: str,
        geo: Geo,
        entry: GeoCatalogEntry,
        quantity: int,
        paid_cost_cents: int,
    ) -> None:
        self.registered.append(
            (provider_order_id, geo, entry, quantity, paid_cost_cents)
        )


class _CapacityClient:
    def __init__(self, *, fail_purchase: bool = False) -> None:
        self.fail_purchase = fail_purchase
        self.priced: list[int] = []
        self.purchased: list[int] = []
        self.provider_orders: dict[str, ProviderOrder] = {}

    def calculate_pricing(self, **values) -> object:
        self.priced.append(values["quantity"])
        return type("Quote", (), {"total_cents": values["quantity"] * 300})()

    def create_balance_order(self, **values) -> object:
        self.purchased.append(values["quantity"])
        if self.fail_purchase:
            raise IPRoyalAPIError("IPRoyal order outcome is unknown")
        return type(
            "Order",
            (),
            {"order_id": "420", "status": "in-progress"},
        )()

    def read_order(self, order_id: str, _geo: Geo) -> ProviderOrder:
        return self.provider_orders[order_id]


def _policy() -> CapacityPolicy:
    return CapacityPolicy(
        spare_per_geo=2,
        max_order_quantity=5,
        max_order_cost_cents=2_000,
        max_monthly_cost_cents=5_000,
    )


def _catalog_entry() -> GeoCatalogEntry:
    return GeoCatalogEntry(product_id=9, plan_id=4, location_id=51)


def test_existing_order_import_registers_then_encrypts_confirmed_inventory() -> None:
    geo = Geo("US", "CA", "San Francisco")
    repository = _CapacityRepository(CapacitySnapshot(0, 0, 0, 0))
    client = _CapacityClient()
    endpoint = ProxyEndpoint(
        provider="iproyal",
        provider_reference="order:420:198.51.100.248",
        proxy_type="isp_static_residential",
        geo=geo,
        host="198.51.100.248",
        port=12323,
        username="account-user",
        password="account-password",
    )
    client.provider_orders["420"] = ProviderOrder(
        order_id="420",
        status="confirmed",
        quantity=1,
        endpoints=(endpoint,),
    )

    quantity = import_existing_order(
        repository=repository,
        client=client,
        encryption_key="encryption-key",
        provider_order_id="420",
        geo=geo,
        entry=_catalog_entry(),
        paid_cost_cents=300,
    )

    assert quantity == 1
    assert repository.registered == [("420", geo, _catalog_entry(), 1, 300)]
    assert repository.confirmed == [("420", (endpoint,), "encryption-key")]


def test_existing_order_import_rejects_unconfirmed_order_without_registration() -> None:
    geo = Geo("US", "CA", "San Francisco")
    repository = _CapacityRepository(CapacitySnapshot(0, 0, 0, 0))
    client = _CapacityClient()
    client.provider_orders["420"] = ProviderOrder(
        order_id="420",
        status="in-progress",
        quantity=1,
        endpoints=(),
    )

    with pytest.raises(ExistingOrderImportError, match="not confirmed"):
        import_existing_order(
            repository=repository,
            client=client,
            encryption_key="encryption-key",
            provider_order_id="420",
            geo=geo,
            entry=_catalog_entry(),
            paid_cost_cents=300,
        )

    assert repository.registered == []


def test_confirmed_mixed_country_order_imports_each_exact_geo_segment() -> None:
    repository = _CapacityRepository(CapacitySnapshot(0, 0, 0, 0))
    us_geo = Geo("US", "NY", "New York")
    dk_geo = Geo("DK", "84", "Copenhagen")
    us_entry = GeoCatalogEntry(product_id=9, plan_id=22, location_id=16)
    dk_entry = GeoCatalogEntry(product_id=9, plan_id=22, location_id=26)
    endpoints = (
        ProxyEndpoint(
            provider="iproyal",
            provider_reference="order:10000001:192.0.2.10",
            proxy_type="isp_static_residential",
            geo=us_geo,
            host="192.0.2.10",
            port=12323,
            username="us-user",
            password="us-password",
        ),
        ProxyEndpoint(
            provider="iproyal",
            provider_reference="order:10000001:192.0.2.20",
            proxy_type="isp_static_residential",
            geo=dk_geo,
            host="192.0.2.20",
            port=12323,
            username="dk-user",
            password="dk-password",
        ),
    )

    quantity = import_confirmed_order_segments(
        repository=repository,
        encryption_key="encryption-key",
        provider_order_id="10000001",
        segments=(
            ConfirmedOrderSegment(us_geo, us_entry, 1_615, (endpoints[0],)),
            ConfirmedOrderSegment(dk_geo, dk_entry, 4_845, (endpoints[1],)),
        ),
    )

    assert quantity == 2
    assert repository.registered == [
        ("10000001", us_geo, us_entry, 1, 1_615),
        ("10000001", dk_geo, dk_entry, 1, 4_845),
    ]
    assert repository.confirmed_segments == [
        ("10000001", us_geo, (endpoints[0],), "encryption-key"),
        ("10000001", dk_geo, (endpoints[1],), "encryption-key"),
    ]


def test_confirmed_order_payload_groups_exact_geos_without_duplicating_cost() -> None:
    proxies = []
    for offset in range(5):
        proxies.append(
            {
                "reference": f"order:10000001:192.0.2.{offset + 1}",
                "host": f"192.0.2.{offset + 1}",
                "port": 12323,
                "username": "us-user",
                "password": "us-password",
                "country": "US",
                "region": "NY",
                "city": "New York",
            }
        )
    for offset in range(15):
        proxies.append(
            {
                "reference": f"order:10000001:198.51.100.{offset + 1}",
                "host": f"198.51.100.{offset + 1}",
                "port": 12323,
                "username": "dk-user",
                "password": "dk-password",
                "country": "DK",
                "region": "84",
                "city": "Copenhagen",
            }
        )

    order_id, segments = parse_confirmed_order_payload(
        json.dumps(
            {
                "order_id": "10000001",
                "product_id": 9,
                "plan_id": 22,
                "paid_cost_cents": 6_460,
                "location_ids": {"US": 16, "DK": 26},
                "proxies": proxies,
            }
        )
    )

    assert order_id == "10000001"
    assert [
        (segment.geo, len(segment.endpoints), segment.paid_cost_cents)
        for segment in segments
    ] == [
        (Geo("DK", "84", "Copenhagen"), 15, 4_845),
        (Geo("US", "NY", "New York"), 5, 1_615),
    ]
    assert sum(segment.paid_cost_cents for segment in segments) == 6_460
    assert [segment.entry.location_id for segment in segments] == [26, 16]


def test_confirmed_order_health_failure_prevents_partial_inventory_import() -> None:
    repository = _CapacityRepository(CapacitySnapshot(0, 0, 0, 0))
    geo = Geo("DK", "84", "Copenhagen")
    endpoint = ProxyEndpoint(
        provider="iproyal",
        provider_reference="order:10000001:192.0.2.20",
        proxy_type="isp_static_residential",
        geo=geo,
        host="192.0.2.20",
        port=12323,
        username="dk-user",
        password="dk-password",
    )

    with pytest.raises(ExistingOrderImportError, match="health check failed"):
        import_confirmed_order_segments(
            repository=repository,
            encryption_key="encryption-key",
            provider_order_id="10000001",
            segments=(
                ConfirmedOrderSegment(
                    geo,
                    GeoCatalogEntry(product_id=9, plan_id=22, location_id=26),
                    323,
                    (endpoint,),
                ),
            ),
            health_check=lambda _endpoint: ProxyHealth.degraded("geo_mismatch"),
        )

    assert repository.registered == []
    assert repository.confirmed_segments == []


def test_managed_controller_rebinds_an_account_with_a_degraded_proxy() -> None:
    account = UnboundAccount(
        user_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        geo=Geo("US", "CA", "San Francisco"),
    )

    class Repository:
        def list_accounts_needing_binding(self):
            return (account,)

    class Reconciler:
        def refresh_submitted(self, _key):
            return 0

        def reconcile(self):
            return 0

    class Bindings:
        def __init__(self):
            self.rebound = []

        def ensure_binding(self, *_args):
            raise ProxyBindingError("proxy health check failed")

        def rebind(self, *args, **kwargs):
            self.rebound.append((args, kwargs))

    bindings = Bindings()
    settings = type(
        "Settings",
        (),
        {"session_encryption_key": SecretStr("encryption-key")},
    )()
    controller = ManagedCapacityController(
        settings=settings,
        repository=Repository(),
        reconciler=Reconciler(),
        bindings=bindings,
    )

    assert controller.tick() == 1
    assert bindings.rebound == [
        (
            (account.user_id, account.account_id, account.geo),
            {"reason": "managed_proxy_health_recovery"},
        )
    ]


def test_capacity_reconciler_quotes_reserves_then_submits_one_order() -> None:
    geo = Geo("US", "CA", "San Francisco")
    repository = _CapacityRepository(
        CapacitySnapshot(
            connected_accounts=7,
            confirmed_proxies=4,
            pending_proxies=0,
            monthly_spend_cents=0,
        )
    )
    client = _CapacityClient()

    submitted = CapacityReconciler(
        repository=repository,
        client=client,
        catalog={geo: _catalog_entry()},
        policy=_policy(),
    ).reconcile()

    assert submitted == 1
    assert client.priced == [5]
    assert client.purchased == [5]
    assert repository.submitted == [("intent-1", "420", "in-progress")]
    assert repository.unknown == []


def test_capacity_reconciler_refuses_orders_over_the_spend_guard() -> None:
    geo = Geo("US", "CA", "San Francisco")
    repository = _CapacityRepository(
        CapacitySnapshot(
            connected_accounts=7,
            confirmed_proxies=4,
            pending_proxies=0,
            monthly_spend_cents=4_000,
        )
    )
    client = _CapacityClient()

    submitted = CapacityReconciler(
        repository=repository,
        client=client,
        catalog={geo: _catalog_entry()},
        policy=_policy(),
    ).reconcile()

    assert submitted == 0
    assert client.priced == [5]
    assert client.purchased == []


def test_capacity_reconciler_marks_uncertain_purchase_without_retrying() -> None:
    geo = Geo("US", "CA", "San Francisco")
    repository = _CapacityRepository(
        CapacitySnapshot(
            connected_accounts=7,
            confirmed_proxies=4,
            pending_proxies=0,
            monthly_spend_cents=0,
        )
    )
    client = _CapacityClient(fail_purchase=True)

    submitted = CapacityReconciler(
        repository=repository,
        client=client,
        catalog={geo: _catalog_entry()},
        policy=_policy(),
    ).reconcile()

    assert submitted == 0
    assert client.purchased == [5]
    assert repository.submitted == []
    assert repository.unknown == ["intent-1"]


def test_capacity_reconciler_imports_confirmed_orders_before_new_purchases() -> None:
    geo = Geo("US", "CA", "San Francisco")
    repository = _CapacityRepository(
        CapacitySnapshot(
            connected_accounts=1,
            confirmed_proxies=1,
            pending_proxies=0,
            monthly_spend_cents=300,
        )
    )
    repository.awaiting = [
        SubmittedCapacityOrder(intent_id="intent-1", provider_order_id="420", geo=geo)
    ]
    endpoint = ProxyEndpoint(
        provider="iproyal",
        provider_reference="order:420:198.51.100.248",
        proxy_type="isp_static_residential",
        geo=geo,
        host="198.51.100.248",
        port=12323,
        username="proxy-user",
        password="proxy-password",
    )
    client = _CapacityClient()
    client.provider_orders["420"] = ProviderOrder(
        order_id="420", status="confirmed", quantity=1, endpoints=(endpoint,)
    )
    reconciler = CapacityReconciler(
        repository=repository,
        client=client,
        catalog={geo: _catalog_entry()},
        policy=CapacityPolicy(
            spare_per_geo=0,
            max_order_quantity=5,
            max_order_cost_cents=2_000,
            max_monthly_cost_cents=5_000,
        ),
    )

    refreshed = reconciler.refresh_submitted("encryption-key")

    assert refreshed == 1
    assert repository.confirmed == [("420", (endpoint,), "encryption-key")]
    assert repository.failed == []


def _snapshot(**overrides):
    values = {
        "connected_accounts": 0,
        "confirmed_proxies": 0,
        "pending_proxies": 0,
        "monthly_spend_cents": 0,
        "trial_accounts": 0,
        "trial_leases_in_use": 0,
    }
    values.update(overrides)
    return CapacitySnapshot(**values)


class TestTrialDemand:
    """Trials are served from spares, and buy only under a bounded ceiling.

    A lease bills every month, so buying one the moment a seven-day trial
    starts pays for people who may never convert. But a trial that can never
    get a proxy is a trial that does not run, so the ceiling exists rather
    than a flat refusal.
    """

    def test_a_trial_alone_buys_nothing_while_a_spare_is_free(self):
        policy = CapacityPolicy(
            spare_per_geo=2,
            max_order_quantity=5,
            max_order_cost_cents=5_000,
            max_monthly_cost_cents=20_000,
            max_trial_leases=5,
        )

        plan = plan_capacity_order(
            _snapshot(trial_accounts=1, confirmed_proxies=2), policy
        )

        assert plan is None

    def test_an_empty_geography_still_stocks_its_spare_buffer(self):
        """The trial is served by the buffer, not by a lease of its own."""

        policy = CapacityPolicy(2, 5, 5_000, 20_000, max_trial_leases=5)

        plan = plan_capacity_order(
            _snapshot(trial_accounts=1, confirmed_proxies=0), policy
        )

        assert plan is not None
        assert plan.quantity == 2

    def test_trials_beyond_the_spare_buffer_do_buy(self):
        policy = CapacityPolicy(2, 10, 5_000, 20_000, max_trial_leases=5)

        plan = plan_capacity_order(
            _snapshot(trial_accounts=5, confirmed_proxies=2), policy
        )

        assert plan is not None
        # Two spares already cover two trials; the other three need leases.
        assert plan.quantity == 3

    def test_trial_demand_stops_at_the_ceiling(self):
        policy = CapacityPolicy(0, 10, 5_000, 20_000, max_trial_leases=2)

        plan = plan_capacity_order(
            _snapshot(trial_accounts=9, trial_leases_in_use=2), policy
        )

        assert plan is None

    def test_the_ceiling_counts_leases_already_serving_trials(self):
        policy = CapacityPolicy(0, 10, 5_000, 20_000, max_trial_leases=5)

        plan = plan_capacity_order(
            _snapshot(trial_accounts=4, trial_leases_in_use=3), policy
        )

        assert plan is not None
        assert plan.quantity == 2

    def test_converted_accounts_are_never_capped(self):
        """The ceiling bounds speculation, not customers who pay."""

        policy = CapacityPolicy(0, 10, 5_000, 20_000, max_trial_leases=0)

        plan = plan_capacity_order(_snapshot(connected_accounts=6), policy)

        assert plan is not None
        assert plan.quantity == 6
