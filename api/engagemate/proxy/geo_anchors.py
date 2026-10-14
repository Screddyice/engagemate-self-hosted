"""Where each purchased geography actually is, as operator-supplied points.

The verdict measures distance from the city a lease was sold as, so something
has to say where that city is. There are five supported geographies, so a
static table beats a geocoding dependency: it needs no network on the health
path, and it is auditable in a pull request.

Nothing upstream validates these numbers. A transposed sign or a digit dropped
from a latitude relocates a city by hundreds of miles and then silently
condemns or accepts every lease in it, so parsing refuses bad input here
rather than letting it reach a verdict.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import SecretStr

from engagemate.proxy.base import Geo

_REQUIRED = ("country", "region", "city", "latitude", "longitude")


class GeoAnchorRegistry:
    """A lookup from an exact geography to its coordinates."""

    def __init__(self, points: dict[Geo, tuple[float, float]]) -> None:
        self._points = points

    @classmethod
    def from_json(cls, raw: SecretStr | str | None) -> "GeoAnchorRegistry":
        """Parse the table, refusing anything a verdict should not act on.

        An absent table is empty rather than an error: without anchors the
        health check falls back to its original single-oracle path, which is a
        working deployment, not a broken one.
        """

        if raw is None:
            return cls({})
        text = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
        if not text.strip():
            return cls({})

        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise ValueError("proxy geo anchors are invalid") from None
        if not isinstance(payload, list):
            raise ValueError("proxy geo anchors are invalid")

        points: dict[Geo, tuple[float, float]] = {}
        for record in payload:
            if not isinstance(record, dict) or not all(
                key in record for key in _REQUIRED
            ):
                raise ValueError("proxy geo anchors are invalid")
            try:
                geo = Geo(
                    str(record["country"]),
                    str(record["region"]),
                    str(record["city"]),
                )
                latitude = float(record["latitude"])
                longitude = float(record["longitude"])
            except (TypeError, ValueError):
                raise ValueError("proxy geo anchors are invalid") from None
            if not -90.0 <= latitude <= 90.0 or not -180.0 <= longitude <= 180.0:
                raise ValueError("proxy geo anchor is off the globe")
            # Two anchors for one geography would silently pick a city
            # depending on ordering, which is the kind of thing nobody
            # notices until a lease is wrongly condemned.
            if geo in points:
                raise ValueError("proxy geo anchors contain a duplicate geography")
            points[geo] = (latitude, longitude)
        return cls(points)

    @classmethod
    def from_settings(cls, settings: Any) -> "GeoAnchorRegistry":
        return cls.from_json(settings.proxy_geo_anchors_json)

    def point(self, geo: Geo) -> tuple[float, float] | None:
        return self._points.get(geo)

    def as_mapping(self) -> dict[Geo, tuple[float, float]]:
        return dict(self._points)

    def __len__(self) -> int:
        return len(self._points)

    def __repr__(self) -> str:
        return f"GeoAnchorRegistry(geographies={len(self._points)})"
