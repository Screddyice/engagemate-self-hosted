from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from uuid import UUID

import pytest
from pydantic import SecretStr

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from engagemate.browser.fingerprint import (
    CapturedBrowserSession,
    FingerprintAccount,
    FingerprintFactory,
    FingerprintGeoRegistry,
)
from engagemate.config import Settings
from engagemate.proxy.base import Geo


ACCOUNT_A = UUID("11111111-1111-4111-8111-111111111111")
ACCOUNT_B = UUID("22222222-2222-4222-8222-222222222222")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
)
SEED = SecretStr("fingerprint-seed-that-is-at-least-32-bytes")
GEO_MAP = json.dumps(
    [
        {
            "country": "IN",
            "region": "MH",
            "city": "Mumbai",
            "timezone": "Asia/Kolkata",
            "locale": "en-IN",
        },
        {
            "country": "US",
            "region": "NY",
            "city": "New York",
            "timezone": "America/New_York",
            "locale": "en-US",
        },
        {
            "country": "GB",
            "region": "ENG",
            "city": "London",
            "timezone": "Europe/London",
            "locale": "en-GB",
        },
    ]
)
GEO_REGISTRY = FingerprintGeoRegistry.from_json(GEO_MAP)


def _factory(
    seed: str | SecretStr | None = SEED,
    registry: FingerprintGeoRegistry = GEO_REGISTRY,
) -> FingerprintFactory:
    return FingerprintFactory(seed, registry)


def _account(
    account_id: UUID = ACCOUNT_A,
    geo: Geo = Geo("IN", "MH", "Mumbai"),
) -> FingerprintAccount:
    return FingerprintAccount(id=account_id, geo=geo)


def _session(user_agent: str = USER_AGENT) -> CapturedBrowserSession:
    return CapturedBrowserSession(captured_user_agent=user_agent)


def test_fingerprint_is_stable_for_the_same_account_session_and_geo() -> None:
    """Removing the account-bound HMAC derivation would make fields drift."""

    first = _factory().for_account(_account(), _session())
    second = _factory().for_account(_account(), _session())

    assert first == second
    assert first.user_agent == USER_AGENT
    assert first.timezone == "Asia/Kolkata"
    assert first.locale == "en-IN"
    assert first.viewport == {
        "width": first.viewport_width,
        "height": first.viewport_height,
    }
    assert 1_280 <= first.viewport_width <= 1_920
    assert 720 <= first.viewport_height <= 1_200
    assert first.color_scheme in {"light", "dark"}


def test_different_accounts_never_reuse_canvas_or_webgl_seeds() -> None:
    """Dropping the canonical account UUID from the HMAC would share seeds."""

    factory = _factory()

    first = factory.for_account(_account(ACCOUNT_A), _session())
    second = factory.for_account(_account(ACCOUNT_B), _session())

    assert first.canvas_seed != second.canvas_seed
    assert first.webgl_seed != second.webgl_seed


def test_user_agent_and_exact_geo_participate_in_stable_derivation() -> None:
    """Ignoring captured login inputs would reuse a stale browser identity."""

    factory = _factory()
    baseline = factory.for_account(_account(), _session())
    changed_agent = factory.for_account(
        _account(), _session(USER_AGENT.replace("126.0.0.0", "127.0.0.0"))
    )
    changed_geo = factory.for_account(
        _account(geo=Geo("US", "NY", "New York")), _session()
    )

    assert changed_agent.canvas_seed != baseline.canvas_seed
    assert changed_geo.webgl_seed != baseline.webgl_seed
    assert changed_geo.timezone == "America/New_York"
    assert changed_geo.locale == "en-US"


@pytest.mark.parametrize(
    "geo",
    [Geo("IN", "MH", "Pune"), Geo("US", "NY", "Albany")],
)
def test_unknown_exact_geo_fails_instead_of_guessing(geo: Geo) -> None:
    """A country-only fallback would silently launch in the wrong timezone."""

    with pytest.raises(ValueError, match="unsupported fingerprint geography"):
        _factory().for_account(_account(geo=geo), _session())


@pytest.mark.parametrize(
    "seed",
    [None, "", "short", SecretStr("still-too-short")],
)
def test_factory_fails_closed_when_seed_is_missing_or_weak(
    seed: str | SecretStr | None,
) -> None:
    """Accepting a missing or short key would make fingerprints predictable."""

    with pytest.raises(ValueError, match="fingerprint seed"):
        FingerprintFactory(seed, GEO_REGISTRY)


def test_worker_seed_setting_is_optional_but_factory_from_settings_fails_closed() -> None:
    """Making the worker-only secret API-required or silently optional would break startup."""

    common = {
        "DATABASE_URL": "postgresql://localhost/engagemate",
        "SESSION_ENCRYPTION_KEY": "A" * 43,
        "INTERNAL_API_KEY": "internal-test-key",
    }
    api_settings = Settings(**common)
    assert api_settings.fingerprint_seed is None
    assert api_settings.fingerprint_geo_map_json is None
    with pytest.raises(ValueError, match="fingerprint seed"):
        FingerprintFactory.from_settings(api_settings)

    worker_settings = Settings(
        **common,
        FINGERPRINT_SEED="fingerprint-seed-that-is-at-least-32-bytes",
        FINGERPRINT_GEO_MAP_JSON=GEO_MAP,
    )
    fingerprint = FingerprintFactory.from_settings(worker_settings).for_account(
        _account(), _session()
    )
    assert fingerprint.timezone == "Asia/Kolkata"


def test_canonical_uuid_is_required_and_secrets_are_redacted() -> None:
    """Permissive identifiers or repr output could destabilize or expose the key."""

    with pytest.raises(ValueError, match="canonical account UUID"):
        _factory().for_account(
            replace(_account(), id="11111111111141118111111111111111"),  # type: ignore[arg-type]
            _session(),
        )

    factory_repr = repr(_factory())
    fingerprint_repr = repr(
        _factory().for_account(_account(), _session())
    )
    assert SEED.get_secret_value() not in factory_repr
    assert SEED.get_secret_value() not in fingerprint_repr
    assert "redacted" in factory_repr
    assert "canvas=<derived>" in fingerprint_repr


@pytest.mark.parametrize("user_agent", ["", " ", "x" * 1_025, "valid\ninjected"])
def test_invalid_captured_user_agent_fails_closed(user_agent: str) -> None:
    """Unbounded or control-bearing user agents could corrupt launch options."""

    with pytest.raises(ValueError, match="captured user agent"):
        _factory().for_account(_account(), _session(user_agent))


def test_registry_supports_london_and_arbitrary_valid_exact_mapping() -> None:
    """Hard-coded launch cities would reject a valid configured customer location."""

    london = _factory().for_account(
        _account(geo=Geo("GB", "ENG", "London")), _session()
    )
    tokyo_registry = FingerprintGeoRegistry.from_json(
        json.dumps(
            [
                {
                    "country": "JP",
                    "region": "13",
                    "city": "Tokyo",
                    "timezone": "Asia/Tokyo",
                    "locale": "ja-JP",
                }
            ]
        )
    )
    tokyo = _factory(registry=tokyo_registry).for_account(
        _account(geo=Geo("JP", "13", "Tokyo")), _session()
    )

    assert (london.timezone, london.locale) == ("Europe/London", "en-GB")
    assert (tokyo.timezone, tokyo.locale) == ("Asia/Tokyo", "ja-JP")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "secret-bearing-invalid-json",
        "{}",
        "[]",
        json.dumps(
            [
                {
                    "country": "IN",
                    "region": "MH",
                    "city": "Mumbai",
                    "timezone": "Not/AZone",
                    "locale": "en-IN",
                }
            ]
        ),
        json.dumps(
            [
                {
                    "country": "IN",
                    "region": "MH",
                    "city": "Mumbai",
                    "timezone": "Asia/Kolkata",
                    "locale": "invalid_locale",
                }
            ]
        ),
        json.dumps(
            [
                {
                    "country": "IN",
                    "region": "MH",
                    "city": "Mumbai",
                    "timezone": "Asia/Kolkata",
                    "locale": "en-IN",
                },
                {
                    "country": "IN",
                    "region": "MH",
                    "city": "Mumbai",
                    "timezone": "Asia/Calcutta",
                    "locale": "en-IN",
                },
            ]
        ),
    ],
)
def test_registry_rejects_missing_malformed_duplicate_or_invalid_entries(
    raw: str,
) -> None:
    """Accepting an ambiguous registry could create contradictory identities."""

    with pytest.raises(ValueError) as raised:
        FingerprintGeoRegistry.from_json(raw)
    assert str(raised.value) == "fingerprint geography registry is invalid"
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
    assert "secret-bearing" not in repr(raised.value)


@pytest.mark.parametrize(
    ("user_agent", "vendor_fragments", "renderer_fragment"),
    [
        (
            USER_AGENT,
            ("Apple",),
            "ANGLE (Apple",
        ),
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
            ("NVIDIA", "Intel"),
            "Direct3D11",
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36",
            ("Intel", "AMD"),
            "Mesa",
        ),
    ],
)
def test_webgl_profile_matches_the_captured_desktop_operating_system(
    user_agent: str,
    vendor_fragments: tuple[str, ...],
    renderer_fragment: str,
) -> None:
    """An OS-contradicting renderer would expose a synthetic fingerprint."""

    fingerprint = _factory().for_account(_account(), _session(user_agent))

    assert any(fragment in fingerprint.webgl_vendor for fragment in vendor_fragments)
    assert renderer_fragment in fingerprint.webgl_renderer
    assert fingerprint.webgl_seed not in fingerprint.webgl_renderer


def test_unsupported_user_agent_os_fails_closed() -> None:
    """Guessing a renderer for an unsupported OS would create a contradiction."""

    with pytest.raises(ValueError, match="unsupported captured user agent"):
        _factory().for_account(
            _account(),
            _session("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Safari/604.1"),
        )
