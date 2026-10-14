"""Choose an account's login geography from the inventory we actually hold.

Onboarding used to make the operator type a country, region, and city, and the
worker then demanded an exact free lease in that triple or failed the run. That
put the burden of knowing our inventory on the person least able to know it.

This picks for them: given roughly where the account lives, return the nearest
city we hold a free lease in. The rule is asymmetric on purpose. Nearer is
better, but "nearest" can still be far, and a lease two time zones from where an
account has always logged in is a worse anti-ban signal than the honest city.
So the recommendation carries the distance and a guard flag; the caller decides
whether to bind it, warn, or buy a lease in the real metro instead.

Pure by design. Fetching inventory and geolocating the account belong to the
caller; the rule is a table here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from zoneinfo import TZPATH

from engagemate.proxy.base import Geo
from engagemate.proxy.geo_verdict import haversine_miles

# Beyond this the nearest lease is far enough that it is worth surfacing rather
# than binding silently. Roughly one time zone of driving.
DEFAULT_MAX_DISTANCE_MILES = 300.0

# An IANA time zone is the one location signal a browser gives up without a
# permission prompt (`Intl.DateTimeFormat().resolvedOptions().timeZone`), so it
# is what the extension sends to seed the pick. A zone is a rough point, not a
# city, which is exactly enough to rank our handful of anchor cities and let the
# distance guard catch the far cases. Unknown zones resolve to nothing rather
# than a guess -- a wrong point would silently bind the wrong metro.
_CURATED_TIMEZONE_POINTS: dict[str, tuple[float, float]] = {
    "America/New_York": (40.7128, -74.0060),
    "America/Detroit": (42.3314, -83.0458),
    "America/Chicago": (41.8781, -87.6298),
    "America/Denver": (39.7392, -104.9903),
    "America/Phoenix": (33.4484, -112.0740),
    "America/Los_Angeles": (34.0522, -118.2437),
    "America/Anchorage": (61.2181, -149.9003),
    "Pacific/Honolulu": (21.3069, -157.8583),
    "America/Toronto": (43.6532, -79.3832),
    "America/Vancouver": (49.2827, -123.1207),
    "Europe/London": (51.5074, -0.1278),
    "Europe/Copenhagen": (55.6761, 12.5683),
    "Europe/Paris": (48.8566, 2.3522),
    "Europe/Berlin": (52.5200, 13.4050),
    "Europe/Madrid": (40.4168, -3.7038),
    "Asia/Kolkata": (19.0760, 72.8777),
    "Asia/Dubai": (25.2048, 55.2708),
    "Asia/Singapore": (1.3521, 103.8198),
    "Asia/Tokyo": (35.6762, 139.6503),
    "Australia/Sydney": (-33.8688, 151.2093),
}


def _parse_compact_coordinate(value: str, degree_digits: int) -> float:
    """Parse one signed ISO 6709 coordinate from tzdata's zone table."""

    if not value or value[0] not in "+-":
        raise ValueError("invalid compact coordinate")
    digits = value[1:]
    if len(digits) not in {degree_digits + 2, degree_digits + 4}:
        raise ValueError("invalid compact coordinate")
    degrees = int(digits[:degree_digits])
    minutes = int(digits[degree_digits : degree_digits + 2])
    seconds = (
        int(digits[degree_digits + 2 :])
        if len(digits) > degree_digits + 2
        else 0
    )
    if minutes >= 60 or seconds >= 60:
        raise ValueError("invalid compact coordinate")
    coordinate = degrees + minutes / 60 + seconds / 3600
    return -coordinate if value[0] == "-" else coordinate


def _parse_zone_coordinates(value: str) -> tuple[float, float]:
    """Split and parse tzdata's concatenated latitude/longitude value."""

    separator = next(
        (
            index
            for index, character in enumerate(value[1:], start=1)
            if character in "+-"
        ),
        -1,
    )
    if separator < 0:
        raise ValueError("invalid zone coordinates")
    latitude = _parse_compact_coordinate(value[:separator], 2)
    longitude = _parse_compact_coordinate(value[separator:], 3)
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise ValueError("invalid zone coordinates")
    return latitude, longitude


def _load_timezone_points() -> dict[str, tuple[float, float]]:
    """Load canonical IANA zone coordinates with a small portable fallback."""

    points = dict(_CURATED_TIMEZONE_POINTS)
    for root in TZPATH:
        table = Path(root) / "zone.tab"
        try:
            rows = table.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for row in rows:
            if not row or row.startswith("#"):
                continue
            fields = row.split("\t")
            if len(fields) < 3:
                continue
            try:
                points.setdefault(fields[2], _parse_zone_coordinates(fields[1]))
            except (TypeError, ValueError):
                continue
        break
    return points


_TIMEZONE_POINTS = _load_timezone_points()


def resolve_timezone_point(timezone: str) -> tuple[float, float] | None:
    """Map an IANA time-zone name to an approximate point, or None if unknown.

    A miss returns None on purpose: the caller then shows a picker rather than
    binding a metro the account never logs in from.
    """

    if not isinstance(timezone, str):
        return None
    return _TIMEZONE_POINTS.get(timezone.strip())


@dataclass(frozen=True, slots=True)
class GeoOption:
    """One supported geography, its anchor point, and its free lease count."""

    geo: Geo
    latitude: float
    longitude: float
    free_leases: int


@dataclass(frozen=True, slots=True)
class GeoRecommendation:
    """The nearest usable geography and how far it is from the account."""

    geo: Geo
    distance_miles: float
    free_leases: int
    within_guard: bool


def recommend_login_geo(
    latitude: float,
    longitude: float,
    options: list[GeoOption],
    max_distance_miles: float = DEFAULT_MAX_DISTANCE_MILES,
) -> GeoRecommendation | None:
    """Return the nearest geography with a free lease, or None if none exist.

    Ties break on the geography itself, never on list order, so the same board
    always yields the same pick.
    """

    usable = [option for option in options if option.free_leases > 0]
    if not usable:
        return None

    def sort_key(option: GeoOption) -> tuple[float, str, str, str]:
        distance = haversine_miles(
            latitude, longitude, option.latitude, option.longitude
        )
        return (distance, option.geo.country, option.geo.region, option.geo.city)

    best = min(usable, key=sort_key)
    distance = haversine_miles(latitude, longitude, best.latitude, best.longitude)
    return GeoRecommendation(
        geo=best.geo,
        distance_miles=distance,
        free_leases=best.free_leases,
        within_guard=distance <= max_distance_miles,
    )
