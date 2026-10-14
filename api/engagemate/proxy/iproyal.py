"""IPRoyal dedicated ISP/static lease import and health checks."""

from __future__ import annotations

import ipaddress
import json
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx

from engagemate.browser.fingerprint import FingerprintGeoRegistry
from engagemate.config import Settings
from engagemate.proxy.base import (
    Geo,
    ProxyEndpoint,
    ProxyHealth,
    ProxyHealthStatus,
)
from engagemate.proxy.geo_anchors import GeoAnchorRegistry
from engagemate.proxy.geo_providers import provider_by_name
from engagemate.proxy.geo_verdict import GeoAnchor, Reading, evaluate


def _proxy_error_health(error: httpx.ProxyError) -> ProxyHealth:
    # HTTPX exposes CONNECT failures as text, not a response. Only recognize
    # the leading status; never return or log the credential-bearing message.
    status = str(error).split(maxsplit=1)[:1]
    if status == ["403"]:
        return ProxyHealth.degraded("proxy_access_denied")
    if status == ["407"]:
        return ProxyHealth.degraded("proxy_authentication_failed")
    return ProxyHealth.degraded("connection_failed")


class IPRoyalProvider:
    """Import a pre-provisioned IPRoyal ISP lease without provider-side writes.

    IPRoyal's residential bearer API does not provision dedicated static IPs.
    Automated ISP order creation is deliberately absent because the reseller
    endpoint requires admin-gated access and explicit dynamic catalog IDs.
    """

    name = "iproyal"

    def __init__(
        self,
        endpoints: tuple[ProxyEndpoint, ...],
        *,
        health_url: str,
        connect_timeout: float,
        read_timeout: float,
        mode: str = "import",
        allow_empty: bool = False,
        client_factory: Callable[..., httpx.Client] = httpx.Client,
        geo_anchors: Mapping[Geo, tuple[float, float]] | None = None,
        geo_radius_miles: float = 25.0,
        geo_providers: tuple[str, ...] = (),
        geo_max_accuracy_km: float = 100.0,
    ) -> None:
        if not health_url.startswith("https://"):
            raise ValueError("proxy health URL must use HTTPS")
        if not endpoints and not allow_empty:
            raise ValueError("IPRoyal ISP lease inventory is invalid")
        self._endpoints = endpoints
        self._mode = mode
        self._health_url = health_url
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._client_factory = client_factory
        self._geo_anchors = dict(geo_anchors or {})
        self._geo_radius_miles = geo_radius_miles
        self._geo_max_accuracy_km = geo_max_accuracy_km
        self._geo_panel = tuple(provider_by_name(name) for name in geo_providers)

    @classmethod
    def from_settings(cls, settings: Settings) -> IPRoyalProvider:
        """Build an imported provider or managed database-inventory fallback."""

        panel = cls._panel_from_settings(settings)
        anchors = GeoAnchorRegistry.from_settings(settings).as_mapping()
        if settings.iproyal_mode in {"inventory", "managed"}:
            return cls(
                (),
                health_url=settings.proxy_health_url or "",
                connect_timeout=settings.proxy_connect_timeout_seconds,
                read_timeout=settings.proxy_read_timeout_seconds,
                mode=settings.iproyal_mode,
                allow_empty=True,
                geo_anchors=anchors,
                geo_radius_miles=settings.proxy_geo_radius_miles,
                geo_providers=panel,
                geo_max_accuracy_km=settings.proxy_geo_max_accuracy_km,
            )
        if settings.iproyal_mode != "import":
            raise ValueError("IPRoyal provider is disabled")

        inventory = settings.iproyal_isp_leases_json
        if inventory is None:
            raise ValueError("IPRoyal ISP lease inventory is invalid")
        endpoints = cls._parse_inventory(inventory.get_secret_value())
        invalid_geo = False
        try:
            registry = FingerprintGeoRegistry.from_settings(settings)
            for endpoint in endpoints:
                registry.require(endpoint.geo)
        except ValueError:
            invalid_geo = True
        if invalid_geo:
            raise ValueError("IPRoyal ISP lease inventory is invalid")
        return cls(
            endpoints,
            health_url=settings.proxy_health_url or "",
            connect_timeout=settings.proxy_connect_timeout_seconds,
            read_timeout=settings.proxy_read_timeout_seconds,
            geo_anchors=anchors,
            geo_radius_miles=settings.proxy_geo_radius_miles,
            geo_providers=panel,
            geo_max_accuracy_km=settings.proxy_geo_max_accuracy_km,
        )

    @staticmethod
    def _panel_from_settings(settings: Settings) -> tuple[str, ...]:
        """Read the comma-separated provider list, refusing unknown names.

        Failing at startup rather than at check time means a typo is a refused
        boot, not a proxy that silently never gets verified.
        """

        raw = (settings.proxy_geo_providers or "").strip()
        if not raw:
            return ()
        return tuple(name.strip() for name in raw.split(",") if name.strip())

    def __repr__(self) -> str:
        return (
            f"IPRoyalProvider(mode='{self._mode}', inventory=<redacted>, "
            "credentials=<redacted>)"
        )

    def free_leases_by_geo(
        self,
        *,
        excluded_references: frozenset[str] = frozenset(),
    ) -> dict[Geo, int]:
        """Count configured leases the import-mode allocator can still return."""

        counts: dict[Geo, int] = {}
        for endpoint in self._endpoints:
            if endpoint.provider_reference in excluded_references:
                continue
            counts[endpoint.geo] = counts.get(endpoint.geo, 0) + 1
        return counts

    def allocate_static(
        self,
        geo: Geo,
        *,
        excluded_references: frozenset[str] = frozenset(),
    ) -> ProxyEndpoint:
        """Return an unused configured dedicated ISP lease for its exact geo."""

        for endpoint in self._endpoints:
            if (
                endpoint.geo == geo
                and endpoint.provider_reference not in excluded_references
            ):
                return endpoint
        raise ValueError("no available IPRoyal lease for geography")

    def check(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        """Check the endpoint through bounded, fail-closed HTTPS requests.

        With a geo panel configured this asks several providers and applies
        the consensus rule in `geo_verdict`. Without one it falls back to the
        original single-oracle exact-city check, so existing deployments keep
        working until they are given anchors.
        """

        check = self._check_with_panel if self._geo_panel else self._check_single
        health = check(endpoint)
        if health == ProxyHealth.degraded("proxy_access_denied"):
            # Reopen the same proxy once and recompute all evidence. A transient
            # CONNECT rejection must never trigger rotation or an action replay.
            time.sleep(1.0)
            return check(endpoint)
        return health

    def _proxy_client(self, endpoint: ProxyEndpoint):
        username = quote(endpoint.username, safe="")
        password = quote(endpoint.password, safe="")
        authority = endpoint.server.removeprefix("http://")
        proxy_url = f"http://{username}:{password}@{authority}"
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        return self._client_factory(
            proxy=proxy_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def _check_with_panel(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        anchor_point = self._geo_anchors.get(endpoint.geo)
        if anchor_point is None:
            # An unanchored geography is a configuration gap, not a bad lease.
            # Condemning here would take good inventory out of supply because
            # somebody forgot a row.
            return ProxyHealth(ProxyHealthStatus.INDETERMINATE, "no_geo_anchor")
        anchor = GeoAnchor(latitude=anchor_point[0], longitude=anchor_point[1])

        readings: list[Reading] = []
        try:
            expected_ip = ipaddress.ip_address(endpoint.host)
        except ValueError:
            return ProxyHealth.degraded("invalid_response")

        try:
            with self._proxy_client(endpoint) as client:
                for provider in self._geo_panel:
                    try:
                        response = client.get(provider.url)
                        if response.status_code != 200:
                            continue
                        answer = provider.parse(response.json())
                    except httpx.ProxyError as error:
                        # A rejected CONNECT tunnel is evidence about this
                        # proxy, not a geolocation provider outage.
                        return _proxy_error_health(error)
                    except (httpx.HTTPError, OSError, ValueError):
                        # One provider being unreachable is not evidence about
                        # the proxy. Ask the rest.
                        continue
                    if answer is None:
                        continue
                    try:
                        observed_ip = ipaddress.ip_address(answer.ip)
                    except ValueError:
                        continue
                    # Exit identity is not a vote. Any provider seeing a
                    # different address means traffic is not leaving where we
                    # think, which no amount of agreement can excuse.
                    if observed_ip != expected_ip:
                        return ProxyHealth.degraded("ip_mismatch")
                    readings.append(
                        Reading(
                            provider=provider.name,
                            latitude=answer.latitude,
                            longitude=answer.longitude,
                            accuracy_km=answer.accuracy_km,
                        )
                    )
        except httpx.ProxyError as error:
            return _proxy_error_health(error)
        except (httpx.HTTPError, OSError, ValueError):
            # Provider exceptions may embed proxy URLs. Never propagate them.
            return ProxyHealth.degraded("connection_failed")

        verdict = evaluate(
            anchor,
            tuple(readings),
            self._geo_radius_miles,
            self._geo_max_accuracy_km,
        )
        return ProxyHealth(verdict.status, verdict.reason)

    def _check_single(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        """The original single-oracle check, kept for unconfigured deployments."""

        username = quote(endpoint.username, safe="")
        password = quote(endpoint.password, safe="")
        authority = endpoint.server.removeprefix("http://")
        proxy_url = f"http://{username}:{password}@{authority}"
        timeout = httpx.Timeout(
            connect=self._connect_timeout,
            read=self._read_timeout,
            write=self._read_timeout,
            pool=self._connect_timeout,
        )
        try:
            with self._client_factory(
                proxy=proxy_url,
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = client.get(self._health_url)
                if response.status_code != 200:
                    return ProxyHealth.degraded("unexpected_status")
                try:
                    payload = response.json()
                except ValueError:
                    return ProxyHealth.degraded("invalid_response")
                identity = self._normalize_health_payload(payload)
                if identity is None:
                    return ProxyHealth.degraded("invalid_response")
                try:
                    observed_ip = ipaddress.ip_address(identity["ip"])
                    expected_ip = ipaddress.ip_address(endpoint.host)
                except ValueError:
                    return ProxyHealth.degraded("invalid_response")
                if observed_ip != expected_ip:
                    return ProxyHealth.degraded("ip_mismatch")
                try:
                    observed_geo = Geo(
                        identity["country"],
                        identity["region"],
                        identity["city"],
                    )
                except ValueError:
                    return ProxyHealth.degraded("invalid_response")
                if observed_geo != endpoint.geo:
                    return ProxyHealth.degraded("geo_mismatch")
                return ProxyHealth.healthy()
        except httpx.ProxyError as error:
            return _proxy_error_health(error)
        except (httpx.HTTPError, OSError, ValueError):
            # Provider exceptions may embed proxy URLs. Never propagate them.
            return ProxyHealth.degraded("connection_failed")

    @classmethod
    def _parse_inventory(cls, raw: str) -> tuple[ProxyEndpoint, ...]:
        """Parse a fixed, secret-safe inventory without echoing bad values."""

        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            raise ValueError("IPRoyal ISP lease inventory is invalid") from None
        if not isinstance(payload, list) or not payload:
            raise ValueError("IPRoyal ISP lease inventory is invalid")

        expected_keys = {
            "reference",
            "host",
            "port",
            "username",
            "password",
            "country",
            "region",
            "city",
        }
        endpoints: list[ProxyEndpoint] = []
        references: set[str] = set()
        addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
        try:
            for item in payload:
                if not isinstance(item, dict) or set(item) != expected_keys:
                    raise ValueError
                if isinstance(item["port"], bool) or not isinstance(item["port"], int):
                    raise ValueError
                for key in expected_keys - {"port"}:
                    value = item[key]
                    if (
                        not isinstance(value, str)
                        or not value
                        or value != value.strip()
                        or any(ord(character) < 32 for character in value)
                    ):
                        raise ValueError
                address = ipaddress.ip_address(item["host"])
                if item["reference"] in references or address in addresses:
                    raise ValueError
                references.add(item["reference"])
                addresses.add(address)
                endpoints.append(
                    ProxyEndpoint(
                        provider=cls.name,
                        provider_reference=item["reference"],
                        proxy_type="isp_static_residential",
                        geo=Geo(item["country"], item["region"], item["city"]),
                        host=str(address),
                        port=item["port"],
                        username=item["username"],
                        password=item["password"],
                    )
                )
        except (KeyError, TypeError, ValueError):
            raise ValueError("IPRoyal ISP lease inventory is invalid") from None
        return tuple(endpoints)

    @staticmethod
    def _normalize_health_payload(payload: Any) -> dict[str, str] | None:
        if not isinstance(payload, dict):
            return None
        if set(payload) == {"ip", "country", "region", "city"}:
            identity = payload
        elif {"ip", "country_code", "region_code", "city"} <= set(payload):
            identity = {
                "ip": payload["ip"],
                "country": payload["country_code"],
                "region": payload["region_code"],
                "city": payload["city"],
            }
        else:
            return None
        if not all(
            isinstance(identity[key], str) and bool(identity[key])
            for key in ("ip", "country", "region", "city")
        ):
            return None
        return identity
