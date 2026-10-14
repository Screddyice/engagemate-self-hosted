"""Adapters for the "where am I" services the proxy check asks.

One function per provider rather than configurable field paths. The shapes
differ enough that configuration would become a small unreadable language:
ipinfo packs both coordinates into one comma-joined string, freeipapi renames
the address field, and geojs returns numbers as strings.

Every provider here must serve HTTPS. `IPRoyalProvider` refuses a plaintext
health URL, so a provider that cannot do TLS cannot be asked. ip-api.com is
absent for that reason: its free tier answers `SSL unavailable for this
endpoint, order a key`.

A provider that cannot answer returns None. It must never return a guess: the
verdict counts every reading it is given, so a shrug shaped like data
condemns a working lease.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderReading:
    """One provider's answer about the address the request came from."""

    ip: str
    latitude: float
    longitude: float
    accuracy_km: float | None = None


@dataclass(frozen=True, slots=True)
class GeoProvider:
    name: str
    url: str
    parse: Callable[[Any], ProviderReading | None]


def _number(value: Any) -> float | None:
    # geojs sends coordinates as strings; the others send them as numbers.
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _reading(
    payload: Any,
    *,
    ip_field: str,
    latitude: Any,
    longitude: Any,
    accuracy: Any = None,
) -> ProviderReading | None:
    if not isinstance(payload, Mapping):
        return None
    ip = payload.get(ip_field)
    lat, lon = _number(latitude), _number(longitude)
    if not isinstance(ip, str) or not ip or lat is None or lon is None:
        return None
    return ProviderReading(
        ip=ip, latitude=lat, longitude=lon, accuracy_km=_number(accuracy)
    )


def _parse_ipwho(payload: Any) -> ProviderReading | None:
    if not isinstance(payload, Mapping) or payload.get("success") is False:
        return None
    return _reading(
        payload,
        ip_field="ip",
        latitude=payload.get("latitude"),
        longitude=payload.get("longitude"),
    )


def _parse_ipinfo(payload: Any) -> ProviderReading | None:
    if not isinstance(payload, Mapping):
        return None
    location = payload.get("loc")
    if not isinstance(location, str) or location.count(",") != 1:
        return None
    latitude, _, longitude = location.partition(",")
    return _reading(
        payload, ip_field="ip", latitude=latitude, longitude=longitude
    )


def _parse_freeipapi(payload: Any) -> ProviderReading | None:
    if not isinstance(payload, Mapping):
        return None
    return _reading(
        payload,
        ip_field="ipAddress",
        latitude=payload.get("latitude"),
        longitude=payload.get("longitude"),
    )


def _parse_geojs(payload: Any) -> ProviderReading | None:
    if not isinstance(payload, Mapping):
        return None
    return _reading(
        payload,
        ip_field="ip",
        latitude=payload.get("latitude"),
        longitude=payload.get("longitude"),
        # geojs answers an unknown address with the country centroid and a
        # 1000km accuracy instead of an error. Carrying the number forward is
        # what lets the verdict discard it.
        accuracy=payload.get("accuracy"),
    )


PROVIDERS: tuple[GeoProvider, ...] = (
    GeoProvider("ipwho.is", "https://ipwho.is/", _parse_ipwho),
    GeoProvider("ipinfo.io", "https://ipinfo.io/json", _parse_ipinfo),
    GeoProvider("freeipapi", "https://free.freeipapi.com/api/v1/json/", _parse_freeipapi),
    GeoProvider("geojs.io", "https://get.geojs.io/v1/ip/geo.json", _parse_geojs),
)

_BY_NAME = {provider.name: provider for provider in PROVIDERS}


def provider_by_name(name: str) -> GeoProvider:
    """Return one provider, refusing anything not in the built-in set."""

    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError("unknown geolocation provider") from None
