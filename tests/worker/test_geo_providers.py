"""Per-provider parsing of "where am I" responses.

Each provider answers a different shape, and two of them can answer "I do not
know" in a way that looks like data. Parsing is explicit per provider rather
than configured with field paths, because the shapes differ enough that
configuration would be a small unreadable language.
"""

import pytest
from engagemate.proxy.geo_providers import PROVIDERS, provider_by_name


def _parse(name, payload):
    return provider_by_name(name).parse(payload)


class TestIpWhoIs:
    def test_reads_a_complete_answer(self):
        reading = _parse(
            "ipwho.is",
            {"ip": "1.2.3.4", "latitude": 40.6895, "longitude": -73.7510},
        )

        assert reading.ip == "1.2.3.4"
        assert reading.latitude == pytest.approx(40.6895)
        assert reading.accuracy_km is None

    def test_a_failed_lookup_is_not_a_reading(self):
        assert _parse("ipwho.is", {"success": False, "message": "no"}) is None


class TestIpInfoIo:
    def test_splits_the_combined_location_field(self):
        reading = _parse("ipinfo.io", {"ip": "1.2.3.4", "loc": "40.6895,-73.7510"})

        assert reading.latitude == pytest.approx(40.6895)
        assert reading.longitude == pytest.approx(-73.7510)

    def test_a_missing_location_is_not_a_reading(self):
        assert _parse("ipinfo.io", {"ip": "1.2.3.4", "bogon": True}) is None

    def test_a_malformed_location_is_not_a_reading(self):
        assert _parse("ipinfo.io", {"ip": "1.2.3.4", "loc": "north-ish"}) is None


class TestFreeIpApi:
    def test_uses_current_https_requester_ip_endpoint(self):
        assert provider_by_name("freeipapi").url == "https://free.freeipapi.com/api/v1/json/"

    def test_parses_documented_v1_response(self):
        reading = _parse(
            "freeipapi",
            {
                "ipVersion": 4,
                "ipAddress": "1.1.1.1",
                "latitude": -33.8688,
                "longitude": 151.209,
                "countryCode": "AU",
                "cityName": "Sydney",
                "regionName": "New South Wales",
            },
        )

        assert reading.ip == "1.1.1.1"
        assert reading.latitude == pytest.approx(-33.8688)
        assert reading.longitude == pytest.approx(151.209)
        assert reading.accuracy_km is None

    def test_reads_its_own_field_names(self):
        reading = _parse(
            "freeipapi",
            {"ipAddress": "1.2.3.4", "latitude": 40.7128, "longitude": -74.006},
        )

        assert reading.ip == "1.2.3.4"
        assert reading.longitude == pytest.approx(-74.006)


class TestGeoJs:
    def test_carries_the_accuracy_it_reports(self):
        """geojs is the reason accuracy exists in the model.

        It answers an unknown address with the geographic centre of the
        country and an accuracy of 1000km rather than an error. Without
        carrying that number forward the verdict treats a shrug as evidence.
        """

        reading = _parse(
            "geojs.io",
            {
                "ip": "1.2.3.4",
                "latitude": "37.751",
                "longitude": "-97.822",
                "accuracy": 1000,
            },
        )

        assert reading.accuracy_km == pytest.approx(1000.0)
        assert reading.latitude == pytest.approx(37.751)


class TestRegistry:
    def test_every_provider_uses_https(self):
        """The proxy check refuses plaintext, and so should the panel.

        ip-api.com is absent for exactly this reason: its free tier answers
        `SSL unavailable for this endpoint` and the code requires HTTPS.
        """

        for provider in PROVIDERS:
            assert provider.url.startswith("https://")

    def test_an_unknown_provider_name_is_refused(self):
        with pytest.raises(ValueError):
            provider_by_name("ip-api.com")

    def test_no_provider_parses_a_non_mapping(self):
        for provider in PROVIDERS:
            assert provider.parse([]) is None
            assert provider.parse("nope") is None
