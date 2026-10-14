"""Parsing the anchor table that says where each purchased geography is.

Anchors are operator-supplied and never validated by anything upstream, so a
typo silently relocates a city and condemns or accepts every lease in it.
Parsing refuses bad input rather than carrying it into a verdict.
"""

import base64
import json
import os

import pytest
from engagemate.config import Settings
from engagemate.proxy.base import Geo
from engagemate.proxy.geo_anchors import GeoAnchorRegistry

PHOENIX = Geo("US", "AZ", "Phoenix")


def _anchors(*records):
    return json.dumps(list(records))


def _record(country="US", region="AZ", city="Phoenix", latitude=33.4484,
            longitude=-112.0740):
    return {
        "country": country,
        "region": region,
        "city": city,
        "latitude": latitude,
        "longitude": longitude,
    }


def _settings(anchors_json):
    return Settings(
        DATABASE_URL="postgresql://localhost/x",
        SESSION_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32))
        .decode()
        .rstrip("="),
        INTERNAL_API_KEY="internal-test-key-that-is-long-enough",
        PROXY_GEO_ANCHORS_JSON=anchors_json,
        _env_file=None,
    )


class TestParsing:
    def test_reads_a_point_for_a_geography(self):
        registry = GeoAnchorRegistry.from_json(_anchors(_record()))

        assert registry.point(PHOENIX) == (33.4484, -112.0740)

    def test_a_geography_with_no_anchor_returns_nothing(self):
        registry = GeoAnchorRegistry.from_json(_anchors(_record()))

        assert registry.point(Geo("DK", "84", "Copenhagen")) is None

    def test_an_absent_table_is_empty_rather_than_an_error(self):
        """Anchors are optional: without them the old single-oracle path runs."""

        assert GeoAnchorRegistry.from_json(None).point(PHOENIX) is None

    @pytest.mark.parametrize(
        "bad",
        [
            "not json",
            json.dumps({"country": "US"}),
            json.dumps([{"country": "US", "region": "AZ", "city": "Phoenix"}]),
            json.dumps([_record(latitude="north")]),
        ],
    )
    def test_malformed_input_is_refused(self, bad):
        with pytest.raises(ValueError):
            GeoAnchorRegistry.from_json(bad)

    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)],
    )
    def test_a_point_off_the_globe_is_refused(self, latitude, longitude):
        with pytest.raises(ValueError):
            GeoAnchorRegistry.from_json(
                _anchors(_record(latitude=latitude, longitude=longitude))
            )

    def test_two_anchors_for_one_geography_are_refused(self):
        """Ambiguity here silently picks a city, so refuse it outright."""

        with pytest.raises(ValueError):
            GeoAnchorRegistry.from_json(
                _anchors(_record(), _record(latitude=0.0, longitude=0.0))
            )


class TestSettings:
    def test_settings_carry_the_anchor_table(self):
        registry = GeoAnchorRegistry.from_settings(_settings(_anchors(_record())))

        assert registry.point(PHOENIX) == (33.4484, -112.0740)

    def test_the_radius_defaults_to_the_calibrated_value(self):
        assert _settings(None).proxy_geo_radius_miles == 25.0

    def test_a_non_positive_radius_is_refused(self):
        with pytest.raises(ValueError):
            Settings(
                DATABASE_URL="postgresql://localhost/x",
                SESSION_ENCRYPTION_KEY=base64.urlsafe_b64encode(os.urandom(32))
                .decode()
                .rstrip("="),
                INTERNAL_API_KEY="internal-test-key-that-is-long-enough",
                PROXY_GEO_RADIUS_MILES=0,
                _env_file=None,
            )

    def test_the_trial_lease_ceiling_defaults_to_five(self):
        assert _settings(None).iproyal_max_trial_leases == 5


class TestProviderWiring:
    """Settings must reach the provider, or the panel is configured and unused."""

    def _configured(self, **overrides):
        values = {
            "DATABASE_URL": "postgresql://localhost/x",
            "SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32))
            .decode()
            .rstrip("="),
            "INTERNAL_API_KEY": "internal-test-key-that-is-long-enough",
            "IPROYAL_MODE": "inventory",
            "PROXY_HEALTH_URL": "https://ipwho.is/",
            "PROXY_GEO_ANCHORS_JSON": _anchors(_record()),
            "PROXY_GEO_PROVIDERS": "ipwho.is,ipinfo.io",
            "_env_file": None,
        }
        values.update(overrides)
        return Settings(**values)

    def test_a_configured_panel_reaches_the_provider(self):
        from engagemate.proxy.iproyal import IPRoyalProvider

        provider = IPRoyalProvider.from_settings(self._configured())

        assert [p.name for p in provider._geo_panel] == ["ipwho.is", "ipinfo.io"]
        assert provider._geo_anchors[PHOENIX] == (33.4484, -112.0740)
        assert provider._geo_radius_miles == 25.0

    def test_no_configured_panel_keeps_the_original_behaviour(self):
        from engagemate.proxy.iproyal import IPRoyalProvider

        provider = IPRoyalProvider.from_settings(
            self._configured(PROXY_GEO_PROVIDERS="")
        )

        assert provider._geo_panel == ()

    def test_an_unknown_provider_name_is_refused_at_startup(self):
        from engagemate.proxy.iproyal import IPRoyalProvider

        with pytest.raises(ValueError):
            IPRoyalProvider.from_settings(
                self._configured(PROXY_GEO_PROVIDERS="ip-api.com")
            )

    def test_whitespace_between_provider_names_is_tolerated(self):
        from engagemate.proxy.iproyal import IPRoyalProvider

        provider = IPRoyalProvider.from_settings(
            self._configured(PROXY_GEO_PROVIDERS=" ipwho.is , ipinfo.io ")
        )

        assert [p.name for p in provider._geo_panel] == ["ipwho.is", "ipinfo.io"]
