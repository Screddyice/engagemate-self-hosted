"""Secret-safe proxy values and provider contracts."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID


_COUNTRY = re.compile(r"^[A-Z]{2}$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_PROXY_TYPE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _required_text(value: str, field_name: str, *, max_length: int = 255) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"invalid {field_name}")
    return value


@dataclass(frozen=True, slots=True)
class Geo:
    """An exact country, region, and city binding."""

    country: str
    region: str
    city: str

    def __post_init__(self) -> None:
        if not isinstance(self.country, str) or _COUNTRY.fullmatch(self.country) is None:
            raise ValueError("country must be a two-letter uppercase ISO code")
        _required_text(self.region, "region", max_length=128)
        _required_text(self.city, "city", max_length=128)


class ProxyHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    # Nobody usable answered. Distinct from DEGRADED on purpose: a geolocation
    # provider having a bad hour must not condemn a working lease. Every
    # consumer has to handle this explicitly rather than falling through to
    # success, which is why the bind path tests all three states.
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class ProxyHealth:
    """A bounded health result containing only a stable, non-secret reason code."""

    status: ProxyHealthStatus
    reason: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.reason):
            raise ValueError("invalid proxy health reason")

    @classmethod
    def healthy(cls) -> "ProxyHealth":
        return cls(ProxyHealthStatus.HEALTHY, "ok")

    @classmethod
    def degraded(cls, reason: str) -> "ProxyHealth":
        return cls(ProxyHealthStatus.DEGRADED, reason)


@dataclass(frozen=True, slots=True)
class ProxyEndpoint:
    """A runtime proxy endpoint whose credentials never appear in representations."""

    provider: str
    provider_reference: str
    proxy_type: str
    geo: Geo
    host: str
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    id: UUID | None = None

    def __post_init__(self) -> None:
        if _PROVIDER.fullmatch(self.provider) is None:
            raise ValueError("invalid proxy provider")
        _required_text(self.provider_reference, "provider reference")
        if _PROXY_TYPE.fullmatch(self.proxy_type) is None:
            raise ValueError("invalid proxy type")
        _required_text(self.host, "proxy host")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 1 <= self.port <= 65535
        ):
            raise ValueError("proxy port must be between 1 and 65535")
        _required_text(self.username, "proxy username", max_length=1024)
        _required_text(self.password, "proxy password", max_length=4096)

    @property
    def country(self) -> str:
        return self.geo.country

    @property
    def region(self) -> str:
        return self.geo.region

    @property
    def city(self) -> str:
        return self.geo.city

    @property
    def server(self) -> str:
        try:
            host = (
                f"[{self.host}]"
                if ipaddress.ip_address(self.host).version == 6
                else self.host
            )
        except ValueError:
            host = self.host
        return f"http://{host}:{self.port}"


class ProxyProvider(Protocol):
    """A provider that returns dedicated static leases and checks connectivity."""

    name: str

    def allocate_static(
        self,
        geo: Geo,
        *,
        excluded_references: frozenset[str] = frozenset(),
    ) -> ProxyEndpoint:
        """Acquire or import one dedicated static endpoint for an exact geography."""

    def check(self, endpoint: ProxyEndpoint) -> ProxyHealth:
        """Return a fail-closed, secret-safe connectivity result."""
