"""Pick the account's login geography from inventory, not from a text box.

`recommend_login_geo` is pure so the rule is a table. It answers one question:
given roughly where an account lives, which city that we actually hold a free
lease in should it call home? The distance guard is the safety half -- a
lease two time zones away is a worse anti-ban signal than the honest city, so
the recommendation says when the nearest option is too far to trust.
"""

from __future__ import annotations

from engagemate.proxy.base import Geo
from engagemate.proxy.geo_selection import GeoOption, recommend_login_geo


def _opt(country, region, city, lat, lon, free) -> GeoOption:
    return GeoOption(
        geo=Geo(country, region, city), latitude=lat, longitude=lon, free_leases=free
    )


# A small board around one query point (Durham NC, next to Raleigh).
_RALEIGH = _opt("US", "NC", "Raleigh", 35.7796, -78.6382, 2)
_DC = _opt("US", "DC", "Washington", 38.9072, -77.0369, 1)
_PHOENIX = _opt("US", "AZ", "Phoenix", 33.4484, -112.0740, 2)
_DURHAM = (35.9940, -78.8986)


def test_picks_the_nearest_option_with_free_inventory() -> None:
    rec = recommend_login_geo(*_DURHAM, [_RALEIGH, _DC, _PHOENIX])
    assert rec is not None
    assert rec.geo == Geo("US", "NC", "Raleigh")
    assert rec.within_guard is True
    assert rec.distance_miles < 30


def test_skips_a_closer_city_that_has_no_free_lease() -> None:
    # Raleigh is closer but sold out; the pick moves to the next with stock.
    sold_out = _opt("US", "NC", "Raleigh", 35.7796, -78.6382, 0)
    rec = recommend_login_geo(*_DURHAM, [sold_out, _DC])
    assert rec is not None
    assert rec.geo == Geo("US", "DC", "Washington")


def test_none_when_no_option_has_free_inventory() -> None:
    sold_out = [
        _opt("US", "NC", "Raleigh", 35.7796, -78.6382, 0),
        _opt("US", "DC", "Washington", 38.9072, -77.0369, 0),
    ]
    assert recommend_login_geo(*_DURHAM, sold_out) is None


def test_none_for_an_empty_board() -> None:
    assert recommend_login_geo(*_DURHAM, []) is None


def test_distance_guard_flags_a_cross_country_nearest() -> None:
    # A Seattle account with only a Raleigh lease: nearest, but 2,000+ miles.
    seattle = (47.6062, -122.3321)
    rec = recommend_login_geo(*seattle, [_RALEIGH])
    assert rec is not None
    assert rec.geo == Geo("US", "NC", "Raleigh")
    assert rec.within_guard is False
    assert rec.distance_miles > 2000


def test_guard_threshold_is_configurable() -> None:
    # A 60-mile pick is within a 100-mile guard, outside a 25-mile one.
    charlotte = (35.2271, -80.8431)  # ~130 mi from Raleigh
    near = recommend_login_geo(*charlotte, [_RALEIGH], max_distance_miles=200)
    far = recommend_login_geo(*charlotte, [_RALEIGH], max_distance_miles=100)
    assert near.within_guard is True
    assert far.within_guard is False


def test_ties_break_deterministically_by_geo() -> None:
    # Two equidistant options must never depend on list order.
    a = _opt("US", "NC", "Raleigh", 35.0, -78.0, 1)
    b = _opt("US", "SC", "Columbia", 35.0, -78.0, 1)
    point = (35.0, -78.0)
    assert recommend_login_geo(*point, [a, b]).geo == recommend_login_geo(
        *point, [b, a]
    ).geo


# --- timezone resolution: the zero-permission signal the extension can send ---

from engagemate.proxy.geo_selection import resolve_timezone_point


def test_resolves_a_known_iana_zone_to_a_point() -> None:
    point = resolve_timezone_point("America/New_York")
    assert point is not None
    lat, lon = point
    assert 38 < lat < 42  # US east coast latitude band
    assert -76 < lon < -73  # US east coast longitude band


def test_resolves_copenhagen_zone() -> None:
    point = resolve_timezone_point("Europe/Copenhagen")
    assert point is not None
    lat, lon = point
    assert 55 < lat < 56 and 12 < lon < 13


def test_resolves_iana_zone_outside_the_curated_fallback_table() -> None:
    point = resolve_timezone_point("Asia/Makassar")
    assert point is not None
    lat, lon = point
    assert -6 < lat < -4
    assert 118 < lon < 121


def test_unknown_zone_returns_none_not_a_guess() -> None:
    assert resolve_timezone_point("Mars/Olympus") is None
    assert resolve_timezone_point("") is None
    assert resolve_timezone_point("not-a-zone") is None


def test_timezone_point_feeds_the_selector() -> None:
    point = resolve_timezone_point("America/New_York")
    rec = recommend_login_geo(*point, [_RALEIGH, _PHOENIX])
    # An east-coast zone should land on Raleigh, not Phoenix.
    assert rec.geo == Geo("US", "NC", "Raleigh")
