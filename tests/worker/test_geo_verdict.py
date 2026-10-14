"""Consensus geography verdicts over multiple location providers.

Every case here is drawn from the 2026-08-17 production readings. The two
starred ones are regressions: a single provider condemned a working Hempstead
lease, and a provider answering with a country centroid would have condemned
it from 1,200 miles away.
"""

import pytest
from engagemate.proxy.base import ProxyHealthStatus
from engagemate.proxy.geo_verdict import (
    GeoAnchor,
    Reading,
    evaluate,
    haversine_miles,
)

# The two geographies the production defects were sold as.
HEMPSTEAD = GeoAnchor(latitude=40.7062, longitude=-73.6187)
WASHINGTON = GeoAnchor(latitude=38.9072, longitude=-77.0369)

RADIUS = 25.0
MAX_ACCURACY_KM = 100.0


def _at(provider: str, latitude: float, longitude: float, accuracy_km=None):
    return Reading(
        provider=provider,
        latitude=latitude,
        longitude=longitude,
        accuracy_km=accuracy_km,
    )


class TestHaversine:
    def test_identical_points_are_zero_miles_apart(self):
        assert haversine_miles(40.7062, -73.6187, 40.7062, -73.6187) == 0.0

    @pytest.mark.parametrize(
        ("start", "end", "expected_miles"),
        [
            # New York to Los Angeles, a distance with a well-known value.
            ((40.7128, -74.0060), (34.0522, -118.2437), 2445),
            # Hempstead to Inwood NY: the five miles that started all of this.
            ((40.7062, -73.6187), (40.6218, -73.7515), 9),
            # Washington DC to Ashburn VA, just outside a 25-mile radius.
            ((38.9072, -77.0369), (39.0469, -77.4903), 27),
        ],
    )
    def test_known_distances(self, start, end, expected_miles):
        actual = haversine_miles(start[0], start[1], end[0], end[1])

        # Within 3%: the formula assumes a sphere, the earth is not one.
        assert abs(actual - expected_miles) <= max(1.0, expected_miles * 0.03)

    def test_distance_is_symmetric(self):
        there = haversine_miles(40.7062, -73.6187, 38.9072, -77.0369)
        back = haversine_miles(38.9072, -77.0369, 40.7062, -73.6187)

        assert there == pytest.approx(back)


class TestVerdict:
    def test_a_lease_every_provider_places_nearby_is_active(self):
        readings = (
            _at("ipwho.is", 40.7128, -74.0060),
            _at("ipinfo.io", 40.6895, -73.6510),
        )

        verdict = evaluate(HEMPSTEAD, readings, RADIUS, MAX_ACCURACY_KM)

        assert verdict.status is ProxyHealthStatus.HEALTHY

    def test_one_provider_placing_it_nearby_keeps_the_lease(self):
        """Regression: 203.0.113.221, sold as Hempstead NY.

        ipwho.is put it in Lewes DE, 156 miles away. ipinfo.io put it 8.9
        miles away. Requiring agreement to accept would bin a lease that
        works, which is the defect this rule exists to prevent.
        """

        readings = (
            _at("ipwho.is", 38.7746, -75.1393),
            _at("ipinfo.io", 40.6895, -73.7510),
        )

        verdict = evaluate(HEMPSTEAD, readings, RADIUS, MAX_ACCURACY_KM)

        assert verdict.status is ProxyHealthStatus.HEALTHY
        assert verdict.reason == "ok"

    def test_a_lease_every_provider_places_far_is_degraded(self):
        """Regression: 203.0.113.94, sold as Washington DC.

        Virginia on every provider asked. Nobody puts it near the capital.
        """

        readings = (
            _at("ipwho.is", 37.3568, -77.4400),
            _at("ipinfo.io", 39.1157, -77.5636),
        )

        verdict = evaluate(WASHINGTON, readings, RADIUS, MAX_ACCURACY_KM)

        assert verdict.status is ProxyHealthStatus.DEGRADED
        assert verdict.reason == "geo_mismatch"

    def test_a_vague_reading_never_votes(self):
        """Regression: geojs answered a New York lease with Kansas.

        latitude 37.751, longitude -97.822, accuracy 1000km — the geographic
        middle of the United States, which is what that provider returns when
        it does not know. Counted as a reading it condemns the lease.
        """

        readings = (
            _at("geojs.io", 37.751, -97.822, accuracy_km=1000.0),
            _at("ipinfo.io", 40.6895, -73.7510),
        )

        verdict = evaluate(HEMPSTEAD, readings, RADIUS, MAX_ACCURACY_KM)

        assert verdict.status is ProxyHealthStatus.HEALTHY
        assert [r.provider for r in verdict.counted] == ["ipinfo.io"]

    def test_every_reading_vague_is_indeterminate_not_degraded(self):
        readings = (
            _at("geojs.io", 37.751, -97.822, accuracy_km=1000.0),
            _at("other", 37.751, -97.822, accuracy_km=500.0),
        )

        verdict = evaluate(HEMPSTEAD, readings, RADIUS, MAX_ACCURACY_KM)

        assert verdict.status is ProxyHealthStatus.INDETERMINATE

    def test_no_providers_responding_is_indeterminate(self):
        verdict = evaluate(HEMPSTEAD, (), RADIUS, MAX_ACCURACY_KM)

        assert verdict.status is ProxyHealthStatus.INDETERMINATE
        assert verdict.reason == "no_geo_readings"

    def test_one_provider_down_still_reaches_a_verdict(self):
        readings = (_at("ipinfo.io", 40.6895, -73.7510),)

        verdict = evaluate(HEMPSTEAD, readings, RADIUS, MAX_ACCURACY_KM)

        assert verdict.status is ProxyHealthStatus.HEALTHY

    def test_the_verdict_records_every_distance_for_evidence(self):
        readings = (
            _at("ipwho.is", 38.7746, -75.1393),
            _at("ipinfo.io", 40.6895, -73.7510),
        )

        verdict = evaluate(HEMPSTEAD, readings, RADIUS, MAX_ACCURACY_KM)

        distances = {r.provider: round(d) for r, d in verdict.distances}
        assert distances["ipinfo.io"] < 25
        assert distances["ipwho.is"] > 100

    def test_a_radius_must_be_positive(self):
        with pytest.raises(ValueError):
            evaluate(HEMPSTEAD, (), 0.0, MAX_ACCURACY_KM)
