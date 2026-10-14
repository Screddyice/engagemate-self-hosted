"""Secret-safe IPRoyal reseller API boundary.

The write path intentionally performs one HTTP attempt. IPRoyal does not
document an idempotency key for ISP order creation, so callers must treat a
timeout or transport failure as an unknown outcome and reconcile it manually.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from engagemate.proxy.base import Geo, ProxyEndpoint

_BASE_URL = "https://apid.iproyal.com/v1/reseller"


class IPRoyalAPIError(RuntimeError):
    """A stable provider error that never includes response or credential data."""


@dataclass(frozen=True, slots=True)
class PricingQuote:
    total_cents: int
    unit_cents: int


@dataclass(frozen=True, slots=True)
class CreatedOrder:
    order_id: str
    status: str
    quantity: int


@dataclass(frozen=True, slots=True)
class ProviderOrder:
    order_id: str
    status: str
    quantity: int
    endpoints: tuple[ProxyEndpoint, ...]


class IPRoyalResellerClient:
    """Bounded client for pricing, purchasing, and reading ISP orders."""

    def __init__(
        self,
        access_token: str,
        *,
        timeout_seconds: float = 15.0,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
    ) -> None:
        if (
            not isinstance(access_token, str)
            or not access_token.strip()
            or access_token != access_token.strip()
        ):
            raise ValueError("IPRoyal API token is invalid")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("IPRoyal API timeout must be positive")
        self._access_token = access_token
        self._timeout = httpx.Timeout(float(timeout_seconds))
        self._client_factory = client_factory

    def __repr__(self) -> str:
        return "IPRoyalResellerClient(access_token=<redacted>)"

    def calculate_pricing(
        self,
        *,
        product_id: int,
        plan_id: int,
        location_id: int,
        quantity: int,
    ) -> PricingQuote:
        payload = self._request_json(
            "GET",
            "/orders/calculate-pricing",
            params={
                "product_id": product_id,
                "product_plan_id": plan_id,
                "product_location_id": location_id,
                "quantity": quantity,
                "order_billing_type": "regular",
            },
            unknown_write_outcome=False,
        )
        try:
            return PricingQuote(
                total_cents=_money_to_cents(payload["price"]),
                unit_cents=_money_to_cents(payload["price_per_item"]),
            )
        except (KeyError, TypeError, ValueError):
            raise IPRoyalAPIError("IPRoyal pricing response is invalid") from None

    def create_balance_order(
        self,
        *,
        product_id: int,
        plan_id: int,
        location_id: int,
        quantity: int,
    ) -> CreatedOrder:
        payload = self._request_json(
            "POST",
            "/orders",
            json_body={
                "product_id": product_id,
                "product_plan_id": plan_id,
                "product_location_id": location_id,
                "quantity": quantity,
                "auto_extend": False,
            },
            unknown_write_outcome=True,
        )
        try:
            order_id = str(payload["id"])
            status = payload["status"]
            returned_quantity = payload["quantity"]
            if (
                not order_id.isdecimal()
                or not isinstance(status, str)
                or not status
                or isinstance(returned_quantity, bool)
                or not isinstance(returned_quantity, int)
                or returned_quantity != quantity
            ):
                raise ValueError
            return CreatedOrder(order_id, status, returned_quantity)
        except (KeyError, TypeError, ValueError):
            raise IPRoyalAPIError("IPRoyal order response is invalid") from None

    def get_order(self, order_id: str) -> Mapping[str, Any]:
        if not isinstance(order_id, str) or not order_id.isdecimal():
            raise ValueError("IPRoyal order ID is invalid")
        return self._request_json(
            "GET",
            f"/orders/{order_id}",
            unknown_write_outcome=False,
        )

    def get_order_endpoints(self, order_id: str, geo: Geo) -> tuple[ProxyEndpoint, ...]:
        """Read a confirmed order and convert its HTTP inventory strictly."""

        return self.read_order(order_id, geo).endpoints

    def read_order(self, order_id: str, geo: Geo) -> ProviderOrder:
        """Return one order with endpoints only after provider confirmation."""

        payload = self.get_order(order_id)
        try:
            status = payload["status"]
            quantity = payload["quantity"]
            if (
                not isinstance(status, str)
                or not status
                or isinstance(quantity, bool)
                or not isinstance(quantity, int)
                or quantity <= 0
            ):
                raise ValueError
            if status != "confirmed":
                return ProviderOrder(order_id, status, quantity, ())
            proxy_data = payload["proxy_data"]
            port = proxy_data["ports"]["http|https"]
            proxies = proxy_data["proxies"]
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not isinstance(proxies, list)
            ):
                raise ValueError
            endpoints: list[ProxyEndpoint] = []
            for item in proxies:
                if not isinstance(item, dict):
                    raise ValueError
                host = str(ipaddress.ip_address(item["ip"]))
                username = item["username"]
                password = item["password"]
                endpoints.append(
                    ProxyEndpoint(
                        provider="iproyal",
                        provider_reference=f"order:{order_id}:{host}",
                        proxy_type="isp_static_residential",
                        geo=geo,
                        host=host,
                        port=port,
                        username=username,
                        password=password,
                    )
                )
            if len(endpoints) != quantity:
                raise ValueError
            return ProviderOrder(order_id, status, quantity, tuple(endpoints))
        except (KeyError, TypeError, ValueError):
            raise IPRoyalAPIError(
                "IPRoyal proxy inventory response is invalid"
            ) from None

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
        unknown_write_outcome: bool,
    ) -> Mapping[str, Any]:
        headers = {
            "X-Access-Token": self._access_token,
            "Content-Type": "application/json",
        }
        try:
            with self._client_factory(
                base_url=_BASE_URL,
                timeout=self._timeout,
                trust_env=False,
                follow_redirects=False,
                headers=headers,
            ) as client:
                response = client.request(method, path, params=params, json=json_body)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, OSError, ValueError):
            message = (
                "IPRoyal order outcome is unknown"
                if unknown_write_outcome
                else "IPRoyal API request failed"
            )
            raise IPRoyalAPIError(message) from None
        if not isinstance(payload, dict):
            raise IPRoyalAPIError("IPRoyal API response is invalid")
        return payload


def _money_to_cents(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError from None
    if not decimal.is_finite() or decimal < 0:
        raise ValueError
    cents = (decimal * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return int(cents)
