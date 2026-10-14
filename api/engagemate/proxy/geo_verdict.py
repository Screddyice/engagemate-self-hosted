"""Decide whether a proxy is where it was sold to be, from several opinions.

The rule is deliberately asymmetric: **condemn only when every provider that
answered agrees the lease is far away.** One provider placing it nearby keeps
it.

That polarity comes from what actually went wrong. On 2026-08-17 a single
provider put `203.0.113.221` in Delaware while another put it nine miles
from the Hempstead it was sold as. The lease was fine. A rule needing
agreement to accept would have binned a working proxy, bought a replacement,
and left the geography short. Wrongly condemning costs real money; keeping a
slightly-off lease costs little, because location is one control among several
in the anti-ban model rather than the only one.

Providers that do not know must not vote. Some answer an unknown IP with a
country centroid rather than an error: geojs returns latitude 37.751,
longitude -97.822 with `accuracy: 1000` for a New York address, which is the
middle of Kansas. Counted as a reading it condemns from 1,200 miles away, so
anything vaguer than the configured accuracy is discarded before the vote.

This module is pure. Fetching readings belongs to the provider; deciding what
they mean belongs here, so the rule can be tested as a table without a network.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from engagemate.proxy.base import ProxyHealthStatus

_EARTH_RADIUS_MILES = 3958.8


@dataclass(frozen=True, slots=True)
class GeoAnchor:
    """Where a purchased geography actually is."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude <= 90.0:
            raise ValueError("anchor latitude must be between -90 and 90")
        if not -180.0 <= self.longitude <= 180.0:
            raise ValueError("anchor longitude must be between -180 and 180")


@dataclass(frozen=True, slots=True)
class Reading:
    """One provider's opinion of where an address is.

    `accuracy_km` is the radius the provider itself claims. None means it did
    not say, which is treated as usable rather than vague: most providers
    offer no such field and refusing them all would leave nothing to vote.
    """

    provider: str
    latitude: float
    longitude: float
    accuracy_km: float | None = None


@dataclass(frozen=True, slots=True)
class GeoVerdict:
    """A decision plus the evidence behind it, safe to persist and to log."""

    status: ProxyHealthStatus
    reason: str
    counted: tuple[Reading, ...] = field(default_factory=tuple)
    discarded: tuple[Reading, ...] = field(default_factory=tuple)
    distances: tuple[tuple[Reading, float], ...] = field(default_factory=tuple)


def haversine_miles(
    latitude: float, longitude: float, other_latitude: float, other_longitude: float
) -> float:
    """Great-circle distance in miles. A sphere is close enough at this scale."""

    lat1, lat2 = math.radians(latitude), math.radians(other_latitude)
    delta_lat = math.radians(other_latitude - latitude)
    delta_lon = math.radians(other_longitude - longitude)
    inner = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_MILES * math.asin(math.sqrt(inner))


def evaluate(
    anchor: GeoAnchor,
    readings: tuple[Reading, ...],
    radius_miles: float,
    max_accuracy_km: float,
) -> GeoVerdict:
    """Return the verdict for one proxy against the geography it was sold as."""

    if type(radius_miles) is not float or radius_miles <= 0:
        raise ValueError("radius_miles must be positive")
    if type(max_accuracy_km) is not float or max_accuracy_km <= 0:
        raise ValueError("max_accuracy_km must be positive")

    counted: list[Reading] = []
    discarded: list[Reading] = []
    for reading in readings:
        if reading.accuracy_km is not None and reading.accuracy_km > max_accuracy_km:
            discarded.append(reading)
        else:
            counted.append(reading)

    if not counted:
        # Nobody usable answered. Health is left alone: an outage at the
        # provider must never read as a fault in the proxy.
        return GeoVerdict(
            status=ProxyHealthStatus.INDETERMINATE,
            reason="no_geo_readings",
            discarded=tuple(discarded),
        )

    distances = tuple(
        (
            reading,
            haversine_miles(
                anchor.latitude, anchor.longitude, reading.latitude, reading.longitude
            ),
        )
        for reading in counted
    )

    if any(distance <= radius_miles for _, distance in distances):
        return GeoVerdict(
            status=ProxyHealthStatus.HEALTHY,
            reason="ok",
            counted=tuple(counted),
            discarded=tuple(discarded),
            distances=distances,
        )
    return GeoVerdict(
        status=ProxyHealthStatus.DEGRADED,
        reason="geo_mismatch",
        counted=tuple(counted),
        discarded=tuple(discarded),
        distances=distances,
    )
