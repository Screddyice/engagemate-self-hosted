"""Fail-closed capacity planning for dedicated ISP proxy inventory."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from engagemate.proxy.base import Geo
from engagemate.proxy.iproyal_reseller import IPRoyalAPIError


@dataclass(frozen=True, slots=True)
class CapacityPolicy:
    """Operator limits applied before any provider-side purchase."""

    spare_per_geo: int
    max_order_quantity: int
    max_order_cost_cents: int
    max_monthly_cost_cents: int
    # Concurrent leases that may exist purely to serve trials. A count, not a
    # monthly spend: a lease bills every month, so the recurring commitment is
    # what needs bounding. Zero means trials are served from spares only.
    max_trial_leases: int = 0

    def __post_init__(self) -> None:
        values = (
            self.spare_per_geo,
            self.max_order_quantity,
            self.max_order_cost_cents,
            self.max_monthly_cost_cents,
        )
        if isinstance(self.max_trial_leases, bool) or not isinstance(
            self.max_trial_leases, int
        ):
            raise ValueError("capacity policy values must be integers")
        if self.max_trial_leases < 0:
            raise ValueError("trial lease ceiling cannot be negative")
        if any(
            isinstance(value, bool) or not isinstance(value, int) for value in values
        ):
            raise ValueError("capacity policy values must be integers")
        if self.spare_per_geo < 0:
            raise ValueError("spare proxy count cannot be negative")
        if any(value <= 0 for value in values[1:]):
            raise ValueError("capacity purchase limits must be positive")


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    """One exact geography's current demand, supply, and spend."""

    connected_accounts: int
    confirmed_proxies: int
    pending_proxies: int
    monthly_spend_cents: int
    # Accounts in this geography whose owner is still on trial. They create
    # demand only up to the trial ceiling, and only after spares run out.
    trial_accounts: int = 0
    # Leases already serving trials, across every geography. The ceiling is
    # global because the money is.
    trial_leases_in_use: int = 0

    def __post_init__(self) -> None:
        values = (
            self.connected_accounts,
            self.confirmed_proxies,
            self.pending_proxies,
            self.monthly_spend_cents,
            self.trial_accounts,
            self.trial_leases_in_use,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError("capacity snapshot values must be non-negative integers")


@dataclass(frozen=True, slots=True)
class CapacityPlan:
    quantity: int


def plan_capacity_order(
    snapshot: CapacitySnapshot, policy: CapacityPolicy
) -> CapacityPlan | None:
    """Return the next bounded order required to cover demand and its buffer."""

    # Paying accounts and their spares are unconditional demand. Trials
    # consume the spare buffer rather than adding to it: counting them as
    # demand would restock the buffer on every signup, which is the same as
    # buying a lease for each trial by another route. Only trials beyond the
    # buffer create demand, and only up to the global ceiling, so a burst of
    # signups cannot commit us to a lease a month for people who never convert.
    trial_allowance = max(
        0, policy.max_trial_leases - snapshot.trial_leases_in_use
    )
    unabsorbed_trials = max(0, snapshot.trial_accounts - policy.spare_per_geo)
    trial_demand = min(unabsorbed_trials, trial_allowance)
    required = snapshot.connected_accounts + policy.spare_per_geo + trial_demand
    covered = snapshot.confirmed_proxies + snapshot.pending_proxies
    shortfall = required - covered
    if shortfall <= 0:
        return None
    return CapacityPlan(quantity=min(shortfall, policy.max_order_quantity))


@dataclass(frozen=True, slots=True)
class GeoCatalogEntry:
    """Dynamic reseller catalog IDs approved for one exact login geography."""

    product_id: int
    plan_id: int
    location_id: int

    def __post_init__(self) -> None:
        values = (self.product_id, self.plan_id, self.location_id)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("catalog IDs must be positive integers")


@dataclass(frozen=True, slots=True)
class CapacityIntent:
    """A durable at-most-once purchase intent recorded before the API write."""

    intent_id: str
    geo: Geo
    catalog: GeoCatalogEntry
    quantity: int
    quoted_cost_cents: int


@dataclass(frozen=True, slots=True)
class SubmittedCapacityOrder:
    intent_id: str
    provider_order_id: str
    geo: Geo


class CapacityRepository(Protocol):
    def get_snapshot(self, geo: Geo) -> CapacitySnapshot: ...

    def reserve_submission(
        self,
        geo: Geo,
        entry: GeoCatalogEntry,
        policy: CapacityPolicy,
        quantity: int,
        quoted_cost_cents: int,
    ) -> CapacityIntent | None: ...

    def mark_submitted(
        self, intent_id: str, provider_order_id: str, provider_status: str
    ) -> None: ...

    def mark_unknown(self, intent_id: str) -> None: ...

    def list_submitted_orders(self) -> tuple[SubmittedCapacityOrder, ...]: ...

    def confirm_order(
        self,
        provider_order_id: str,
        endpoints: tuple[object, ...],
        encryption_key: str,
    ) -> None: ...

    def mark_provider_failed(self, provider_order_id: str, status: str) -> None: ...


class CapacityClient(Protocol):
    def calculate_pricing(self, **values: int) -> object: ...

    def create_balance_order(self, **values: int) -> object: ...

    def read_order(self, order_id: str, geo: Geo) -> object: ...


class CapacityReconciler:
    """Purchase one bounded shortfall per configured geography and tick."""

    def __init__(
        self,
        *,
        repository: CapacityRepository,
        client: CapacityClient,
        catalog: Mapping[Geo, GeoCatalogEntry],
        policy: CapacityPolicy,
    ) -> None:
        self._repository = repository
        self._client = client
        self._catalog = dict(catalog)
        self._policy = policy

    def reconcile(self) -> int:
        submitted = 0
        for geo, entry in self._catalog.items():
            snapshot = self._repository.get_snapshot(geo)
            plan = plan_capacity_order(snapshot, self._policy)
            if plan is None:
                continue
            quote = self._client.calculate_pricing(
                product_id=entry.product_id,
                plan_id=entry.plan_id,
                location_id=entry.location_id,
                quantity=plan.quantity,
            )
            total_cents = getattr(quote, "total_cents", None)
            if (
                isinstance(total_cents, bool)
                or not isinstance(total_cents, int)
                or total_cents < 0
            ):
                continue
            intent = self._repository.reserve_submission(
                geo,
                entry,
                self._policy,
                plan.quantity,
                total_cents,
            )
            if intent is None:
                continue
            try:
                order = self._client.create_balance_order(
                    product_id=entry.product_id,
                    plan_id=entry.plan_id,
                    location_id=entry.location_id,
                    quantity=intent.quantity,
                )
            except IPRoyalAPIError:
                self._repository.mark_unknown(intent.intent_id)
                continue
            order_id = getattr(order, "order_id", None)
            provider_status = getattr(order, "status", None)
            if not isinstance(order_id, str) or not isinstance(provider_status, str):
                self._repository.mark_unknown(intent.intent_id)
                continue
            self._repository.mark_submitted(intent.intent_id, order_id, provider_status)
            submitted += 1
        return submitted

    def refresh_submitted(self, encryption_key: str) -> int:
        """Import confirmed orders and close terminal provider failures."""

        refreshed = 0
        for pending in self._repository.list_submitted_orders():
            try:
                order = self._client.read_order(pending.provider_order_id, pending.geo)
            except IPRoyalAPIError:
                continue
            status = getattr(order, "status", None)
            if status == "confirmed":
                endpoints = getattr(order, "endpoints", None)
                if not isinstance(endpoints, tuple) or not endpoints:
                    continue
                self._repository.confirm_order(
                    pending.provider_order_id, endpoints, encryption_key
                )
                refreshed += 1
            elif status in {"expired", "refunded"}:
                self._repository.mark_provider_failed(pending.provider_order_id, status)
                refreshed += 1
        return refreshed


def parse_geo_catalog(raw: str) -> dict[Geo, GeoCatalogEntry]:
    """Parse an exact-geo reseller catalog without echoing its input."""

    expected = {
        "country",
        "region",
        "city",
        "product_id",
        "plan_id",
        "location_id",
    }
    try:
        payload: Any = json.loads(raw)
        if not isinstance(payload, list) or not payload:
            raise ValueError
        catalog: dict[Geo, GeoCatalogEntry] = {}
        for item in payload:
            if not isinstance(item, dict) or set(item) != expected:
                raise ValueError
            geo = Geo(item["country"], item["region"], item["city"])
            if geo in catalog:
                raise ValueError
            catalog[geo] = GeoCatalogEntry(
                product_id=item["product_id"],
                plan_id=item["plan_id"],
                location_id=item["location_id"],
            )
        return catalog
    except (KeyError, TypeError, ValueError):
        raise ValueError("IPRoyal geo catalog is invalid") from None
