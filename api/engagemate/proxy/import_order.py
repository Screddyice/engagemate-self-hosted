"""One-shot import for an operator-purchased IPRoyal ISP order."""

from __future__ import annotations

import argparse
import json
import os
import stat
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from engagemate.db.pool import create_pool
from engagemate.db.proxy_capacity import PostgresCapacityRepository
from engagemate.proxy.base import Geo, ProxyEndpoint, ProxyHealth, ProxyHealthStatus
from engagemate.proxy.capacity import GeoCatalogEntry
from engagemate.proxy.iproyal import IPRoyalProvider


@dataclass(frozen=True, slots=True)
class ConfirmedOrderSegment:
    """One exact-geography slice of a confirmed mixed-location order."""

    geo: Geo
    entry: GeoCatalogEntry
    paid_cost_cents: int
    endpoints: tuple[ProxyEndpoint, ...]


class ExistingOrderImportError(RuntimeError):
    """A secret-safe operator import failure."""


class ExistingOrderRepository(Protocol):
    def register_existing_order(
        self,
        *,
        provider_order_id: str,
        geo: Geo,
        entry: GeoCatalogEntry,
        quantity: int,
        paid_cost_cents: int,
    ) -> None: ...

    def confirm_order(
        self,
        provider_order_id: str,
        endpoints: tuple[ProxyEndpoint, ...],
        encryption_key: str,
    ) -> None: ...

    def confirm_order_segment(
        self,
        provider_order_id: str,
        geo: Geo,
        endpoints: tuple[object, ...],
        encryption_key: str,
    ) -> None: ...


class ExistingOrderClient(Protocol):
    def read_order(self, order_id: str, geo: Geo) -> object: ...


def import_existing_order(
    *,
    repository: ExistingOrderRepository,
    client: ExistingOrderClient,
    encryption_key: str,
    provider_order_id: str,
    geo: Geo,
    entry: GeoCatalogEntry,
    paid_cost_cents: int,
) -> int:
    """Verify, register, and import one confirmed provider order idempotently."""

    order = client.read_order(provider_order_id, geo)
    status = getattr(order, "status", None)
    quantity = getattr(order, "quantity", None)
    endpoints = getattr(order, "endpoints", None)
    if (
        status != "confirmed"
        or isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or quantity <= 0
        or not isinstance(endpoints, tuple)
        or len(endpoints) != quantity
    ):
        raise ExistingOrderImportError("IPRoyal order is not confirmed")
    repository.register_existing_order(
        provider_order_id=provider_order_id,
        geo=geo,
        entry=entry,
        quantity=quantity,
        paid_cost_cents=paid_cost_cents,
    )
    repository.confirm_order(provider_order_id, endpoints, encryption_key)
    return quantity


def import_confirmed_order_segments(
    *,
    repository: ExistingOrderRepository,
    encryption_key: str,
    provider_order_id: str,
    segments: tuple[ConfirmedOrderSegment, ...],
    health_check: Callable[[ProxyEndpoint], ProxyHealth] | None = None,
) -> int:
    """Import a verified mixed-location order as exact-geography segments."""

    if not provider_order_id.isdecimal() or not segments:
        raise ExistingOrderImportError("confirmed IPRoyal order is invalid")
    for segment in segments:
        if (
            isinstance(segment.paid_cost_cents, bool)
            or not isinstance(segment.paid_cost_cents, int)
            or segment.paid_cost_cents < 0
            or not segment.endpoints
            or any(
                getattr(endpoint, "geo", None) != segment.geo
                for endpoint in segment.endpoints
            )
        ):
            raise ExistingOrderImportError("confirmed IPRoyal order is invalid")
    if health_check is not None:
        for segment in segments:
            for endpoint in segment.endpoints:
                health = health_check(endpoint)
                if health.status is not ProxyHealthStatus.HEALTHY:
                    raise ExistingOrderImportError(
                        "confirmed IPRoyal order health check failed"
                    )
    quantity = 0
    for segment in segments:
        repository.register_existing_order(
            provider_order_id=provider_order_id,
            geo=segment.geo,
            entry=segment.entry,
            quantity=len(segment.endpoints),
            paid_cost_cents=segment.paid_cost_cents,
        )
        repository.confirm_order_segment(
            provider_order_id,
            segment.geo,
            segment.endpoints,
            encryption_key,
        )
        quantity += len(segment.endpoints)
    return quantity


def parse_confirmed_order_payload(
    raw: str,
) -> tuple[str, tuple[ConfirmedOrderSegment, ...]]:
    """Parse a secret operator export and split its cost across exact geos."""

    expected_keys = {
        "order_id",
        "product_id",
        "plan_id",
        "paid_cost_cents",
        "location_ids",
        "proxies",
    }
    try:
        payload: Any = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise ValueError
        order_id = payload["order_id"]
        product_id = payload["product_id"]
        plan_id = payload["plan_id"]
        paid_cost_cents = payload["paid_cost_cents"]
        locations = payload["location_ids"]
        if (
            not isinstance(order_id, str)
            or not order_id.isdecimal()
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in (product_id, plan_id)
            )
            or isinstance(paid_cost_cents, bool)
            or not isinstance(paid_cost_cents, int)
            or paid_cost_cents < 0
            or not isinstance(locations, dict)
        ):
            raise ValueError
        endpoints = IPRoyalProvider._parse_inventory(json.dumps(payload["proxies"]))
        base_cost, remainder = divmod(paid_cost_cents, len(endpoints))
        grouped: dict[Geo, list[tuple[object, int]]] = defaultdict(list)
        for index, endpoint in enumerate(endpoints):
            location_id = locations.get(endpoint.country)
            if (
                isinstance(location_id, bool)
                or not isinstance(location_id, int)
                or location_id <= 0
            ):
                raise ValueError
            grouped[endpoint.geo].append(
                (endpoint, base_cost + (1 if index < remainder else 0))
            )
        segments = tuple(
            ConfirmedOrderSegment(
                geo=geo,
                entry=GeoCatalogEntry(
                    product_id=product_id,
                    plan_id=plan_id,
                    location_id=locations[geo.country],
                ),
                paid_cost_cents=sum(cost for _, cost in items),
                endpoints=tuple(endpoint for endpoint, _ in items),
            )
            for geo, items in sorted(
                grouped.items(),
                key=lambda item: (
                    item[0].country,
                    item[0].region,
                    item[0].city,
                ),
            )
        )
        return order_id, segments
    except (KeyError, TypeError, ValueError):
        raise ExistingOrderImportError("confirmed IPRoyal order is invalid") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import one confirmed IPRoyal ISP order from a mode-600 file."
    )
    parser.add_argument("--confirmed-order-file", required=True)
    return parser


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or value != value.strip():
        raise ExistingOrderImportError(f"{name} is unavailable")
    return value


def main() -> None:  # pragma: no cover - operator entrypoint
    args = _parser().parse_args()
    pool = None
    try:
        order_path = Path(args.confirmed_order_file).resolve(strict=True)
        if stat.S_IMODE(order_path.stat().st_mode) & 0o077:
            raise ExistingOrderImportError("confirmed order file is not private")
        order_id, segments = parse_confirmed_order_payload(
            order_path.read_text(encoding="utf-8")
        )
        endpoints = tuple(
            endpoint for segment in segments for endpoint in segment.endpoints
        )
        provider = IPRoyalProvider(
            endpoints,
            health_url=_required_environment("PROXY_HEALTH_URL"),
            connect_timeout=float(
                _required_environment("PROXY_CONNECT_TIMEOUT_SECONDS")
            ),
            read_timeout=float(_required_environment("PROXY_READ_TIMEOUT_SECONDS")),
        )
        pool = create_pool(_required_environment("DATABASE_URL"), max_size=2)
        quantity = import_confirmed_order_segments(
            repository=PostgresCapacityRepository(pool),
            encryption_key=_required_environment("SESSION_ENCRYPTION_KEY"),
            provider_order_id=order_id,
            segments=segments,
            health_check=provider.check,
        )
    except Exception:  # noqa: BLE001 -- keep database/provider failures secret-safe
        raise SystemExit("existing IPRoyal order import failed") from None
    finally:
        if pool is not None:
            pool.close()
    print(f"Imported IPRoyal order {order_id} with {quantity} proxies.")


if __name__ == "__main__":  # pragma: no cover - operator entrypoint
    main()
