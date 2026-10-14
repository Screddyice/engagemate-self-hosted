"""FastAPI dependencies for trusted identity and repositories."""

from __future__ import annotations

import hmac
from uuid import UUID

from fastapi import Header, HTTPException, Request, status

from engagemate.browser.fingerprint import FingerprintGeoRegistry
from engagemate.config import Settings
from engagemate.db.accounts import AccountRepository
from engagemate.db.brand_profiles import BrandProfileRepository
from engagemate.db.onboarding_launch import OnboardingLaunchRepository
from engagemate.db.proxies import ProxyRepository
from engagemate.proxy.geo_anchors import GeoAnchorRegistry


def get_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="service configuration unavailable",
        )
    return settings


def current_user_id(
    request: Request,
    x_internal_api_key: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
) -> UUID:
    """Accept identity only from the owner-authenticated trusted web tier."""

    settings = get_settings(request)
    expected = settings.internal_api_key.get_secret_value()
    if (
        not x_internal_api_key
        or not hmac.compare_digest(
            x_internal_api_key.encode("utf-8"), expected.encode("utf-8")
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        )
    try:
        return UUID(x_user_id or "")
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthorized",
        ) from error


def account_repository(request: Request) -> AccountRepository:
    pool = getattr(request.app.state, "db_pool", None)
    settings = get_settings(request)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    return AccountRepository(
        pool, settings.session_encryption_key.get_secret_value()
    )


def brand_profile_repository(request: Request) -> BrandProfileRepository:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    return BrandProfileRepository(pool)


def onboarding_launch_repository(request: Request) -> OnboardingLaunchRepository:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    return OnboardingLaunchRepository(pool)


def proxy_repository(request: Request) -> ProxyRepository:
    pool = getattr(request.app.state, "db_pool", None)
    settings = get_settings(request)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    return ProxyRepository(
        pool, settings.session_encryption_key.get_secret_value()
    )


def geo_anchor_registry(request: Request) -> GeoAnchorRegistry:
    """The lat/lon table login-geography selection ranks against."""

    settings = get_settings(request)
    try:
        return GeoAnchorRegistry.from_settings(settings)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="proxy geo anchors unavailable",
        ) from error


def fingerprint_geo_registry(request: Request) -> FingerprintGeoRegistry:
    """Load the exact worker geography contract without exposing config input."""

    settings = get_settings(request)
    invalid = False
    try:
        registry = FingerprintGeoRegistry.from_settings(settings)
    except ValueError:
        invalid = True
        registry = None
    if invalid or registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="fingerprint geography configuration unavailable",
        )
    return registry
