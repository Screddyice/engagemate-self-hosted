"""Stable account-bound browser fingerprints."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import re
from typing import Any, Final, TYPE_CHECKING
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr

from engagemate.proxy.base import Geo

if TYPE_CHECKING:
    from engagemate.config import Settings


_MINIMUM_SEED_BYTES: Final = 32
_VIEWPORTS: Final = (
    (1280, 720),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1600, 900),
    (1920, 1080),
)
_LOCALE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}$")
_WEBGL_PROFILES: Final = {
    "mac": (
        (
            "Google Inc. (Apple)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)",
        ),
        (
            "Google Inc. (Apple)",
            "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)",
        ),
    ),
    "windows": (
        (
            "Google Inc. (NVIDIA)",
            "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0)",
        ),
        (
            "Google Inc. (Intel)",
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0)",
        ),
    ),
    "linux": (
        (
            "Google Inc. (Intel)",
            "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630, OpenGL 4.6)",
        ),
        (
            "Google Inc. (AMD)",
            "ANGLE (AMD, Mesa AMD Radeon RX 6600, OpenGL 4.6)",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class FingerprintAccount:
    """The immutable account identity needed for fingerprint generation."""

    id: UUID
    geo: Geo


@dataclass(frozen=True, slots=True)
class CapturedBrowserSession:
    """The captured browser input retained with an encrypted session."""

    captured_user_agent: str


@dataclass(frozen=True, slots=True, repr=False)
class FingerprintGeoRegistry:
    """Validated exact geography to browser locale/timezone mappings."""

    _entries: dict[Geo, tuple[str, str]]

    @classmethod
    def from_json(cls, raw: str | SecretStr | None) -> "FingerprintGeoRegistry":
        if isinstance(raw, SecretStr):
            raw_value = raw.get_secret_value()
        else:
            raw_value = raw
        parsed: Any = None
        failed = False
        try:
            if not isinstance(raw_value, str) or not raw_value.strip():
                raise ValueError
            parsed = json.loads(raw_value)
            if not isinstance(parsed, list) or not 1 <= len(parsed) <= 1_000:
                raise ValueError
            entries: dict[Geo, tuple[str, str]] = {}
            expected = {
                "country",
                "region",
                "city",
                "timezone",
                "locale",
            }
            for item in parsed:
                if not isinstance(item, dict) or set(item) != expected:
                    raise ValueError
                if not all(isinstance(item[key], str) for key in expected):
                    raise ValueError
                geo = Geo(item["country"], item["region"], item["city"])
                timezone_name = item["timezone"]
                locale = item["locale"]
                if (
                    not 1 <= len(timezone_name) <= 128
                    or timezone_name != timezone_name.strip()
                    or timezone_name.startswith("/")
                    or ".." in timezone_name.split("/")
                    or _LOCALE.fullmatch(locale) is None
                ):
                    raise ValueError
                ZoneInfo(timezone_name)
                if geo in entries:
                    raise ValueError
                entries[geo] = (timezone_name, locale)
        except (
            AttributeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            ZoneInfoNotFoundError,
        ):
            failed = True
            entries = {}
        finally:
            parsed = None
        if failed:
            raise ValueError("fingerprint geography registry is invalid")
        return cls(entries)

    @classmethod
    def from_settings(cls, settings: "Settings") -> "FingerprintGeoRegistry":
        return cls.from_json(settings.fingerprint_geo_map_json)

    def require(self, geo: Geo) -> tuple[str, str]:
        identity = self._entries.get(geo)
        if identity is None:
            raise ValueError("unsupported fingerprint geography")
        return identity

    def __repr__(self) -> str:
        return f"FingerprintGeoRegistry(entries={len(self._entries)})"


@dataclass(frozen=True, slots=True, repr=False)
class Fingerprint:
    """Stable browser launch values and derived rendering seeds."""

    user_agent: str
    viewport_width: int
    viewport_height: int
    timezone: str
    locale: str
    color_scheme: str
    canvas_seed: str
    webgl_seed: str
    webgl_vendor: str
    webgl_renderer: str

    @property
    def viewport(self) -> dict[str, int]:
        return {"width": self.viewport_width, "height": self.viewport_height}

    def __repr__(self) -> str:
        return (
            "Fingerprint("
            f"user_agent={self.user_agent!r}, "
            f"viewport={self.viewport_width}x{self.viewport_height}, "
            f"timezone={self.timezone!r}, locale={self.locale!r}, "
            f"color_scheme={self.color_scheme!r}, "
            "canvas=<derived>, webgl=<derived>, renderer=<profile>)"
        )


class FingerprintFactory:
    """Derive deterministic identity values with a secret HMAC key."""

    def __init__(
        self,
        seed: str | SecretStr | None,
        geo_registry: FingerprintGeoRegistry,
    ) -> None:
        if isinstance(seed, SecretStr):
            raw_seed = seed.get_secret_value()
        else:
            raw_seed = seed
        if not isinstance(raw_seed, str):
            raise ValueError("fingerprint seed is required")
        try:
            seed_bytes = raw_seed.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("fingerprint seed must be valid UTF-8") from None
        if len(seed_bytes) < _MINIMUM_SEED_BYTES:
            raise ValueError("fingerprint seed must be at least 32 bytes")
        self._seed = seed_bytes
        self._geo_registry = geo_registry

    def __repr__(self) -> str:
        return "FingerprintFactory(seed=<redacted>, algorithm='HMAC-SHA256')"

    @classmethod
    def from_settings(cls, settings: "Settings") -> "FingerprintFactory":
        if settings.fingerprint_seed is None:
            raise ValueError("fingerprint seed is required")
        return cls(
            settings.fingerprint_seed,
            FingerprintGeoRegistry.from_settings(settings),
        )

    def for_account(
        self,
        account: FingerprintAccount,
        session: CapturedBrowserSession,
    ) -> Fingerprint:
        """Return one stable identity for the exact account, UA, and login geo."""

        if type(account.id) is not UUID:
            raise ValueError("canonical account UUID is required")
        timezone, locale = self._geo_registry.require(account.geo)

        user_agent = session.captured_user_agent
        if (
            not isinstance(user_agent, str)
            or not 1 <= len(user_agent) <= 1_024
            or user_agent != user_agent.strip()
            or re.search(r"[\x00-\x1f\x7f]", user_agent) is not None
        ):
            raise ValueError("invalid captured user agent")
        if "Macintosh; Intel Mac OS X" in user_agent:
            operating_system = "mac"
        elif "Windows NT" in user_agent:
            operating_system = "windows"
        elif "X11; Linux" in user_agent:
            operating_system = "linux"
        else:
            raise ValueError("unsupported captured user agent operating system")

        material = "\0".join(
            (
                "engagemate-fingerprint-v1",
                str(account.id),
                user_agent,
                account.geo.country,
                account.geo.region,
                account.geo.city,
            )
        ).encode("utf-8")

        def derive(label: str) -> bytes:
            return hmac.new(
                self._seed,
                material + b"\0" + label.encode("ascii"),
                hashlib.sha256,
            ).digest()

        viewport_index = int.from_bytes(derive("viewport")[:4], "big") % len(
            _VIEWPORTS
        )
        width, height = _VIEWPORTS[viewport_index]
        color_scheme = (
            "dark" if int.from_bytes(derive("color-scheme")[:4], "big") % 2 else "light"
        )
        canvas_seed = derive("canvas").hex()
        webgl_seed = derive("webgl").hex()
        profiles = _WEBGL_PROFILES[operating_system]
        vendor, renderer = profiles[int(webgl_seed[:8], 16) % len(profiles)]
        return Fingerprint(
            user_agent=user_agent,
            viewport_width=width,
            viewport_height=height,
            timezone=timezone,
            locale=locale,
            color_scheme=color_scheme,
            canvas_seed=canvas_seed,
            webgl_seed=webgl_seed,
            webgl_vendor=vendor,
            webgl_renderer=renderer,
        )
