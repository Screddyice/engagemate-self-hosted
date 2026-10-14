"""Tenant-scoped Instagram account and session endpoints."""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from psycopg.errors import UniqueViolation

from engagemate.browser.fingerprint import FingerprintGeoRegistry
from engagemate.config import Settings
from engagemate.control.dependencies import (
    account_repository,
    current_user_id,
    fingerprint_geo_registry,
    geo_anchor_registry,
    get_settings,
    proxy_repository,
)
from engagemate.db.accounts import (
    AccountLimitReachedError,
    AccountRecord,
    AccountRepository,
    AccountSessionIdentityMismatchError,
    canonicalize_instagram_user_id,
    canonicalize_instagram_username,
)
from engagemate.db.proxies import ProxyRepository
from engagemate.proxy.base import Geo
from engagemate.proxy.geo_anchors import GeoAnchorRegistry
from engagemate.proxy.geo_selection import (
    GeoOption,
    recommend_login_geo,
    resolve_timezone_point,
)
from engagemate.proxy.iproyal import IPRoyalProvider


router = APIRouter(prefix="/accounts", tags=["accounts"])


class CookieCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=16_384, repr=False)
    domain: str = Field(min_length=1, max_length=253)
    path: str = Field(default="/", min_length=1, max_length=2_048, pattern=r"^/")
    secure: bool
    http_only: bool = Field(alias="httpOnly")
    same_site: Literal["Strict", "Lax", "None", "no_restriction", "unspecified"] = (
        Field(alias="sameSite")
    )
    expiration_date: float | None = Field(
        default=None,
        alias="expirationDate",
        gt=0,
        le=253_402_300_799,
        allow_inf_nan=False,
    )

    @field_validator("domain")
    @classmethod
    def require_instagram_domain(cls, value: str) -> str:
        normalized = value.lower()
        if not re.fullmatch(
            r"\.?(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)*instagram\.com",
            normalized,
        ):
            raise ValueError("cookie domain must belong to instagram.com")
        return normalized

    @field_validator("expiration_date")
    @classmethod
    def require_finite_expiration(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("cookie expiration must be finite")
        return value


class LoginGeo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    country: str = Field(pattern=r"^[A-Z]{2}$")
    region: str | None = Field(default=None, min_length=1, max_length=128)
    city: str | None = Field(default=None, min_length=1, max_length=128)


class SessionCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._]+$")
    ig_user_id: str = Field(min_length=1, max_length=128)
    cookies: list[CookieCapture] = Field(min_length=1, max_length=100)
    user_agent: str = Field(min_length=1, max_length=1_024)
    login_geo: LoginGeo

    @field_validator("username")
    @classmethod
    def canonicalize_username(cls, value: str) -> str:
        return canonicalize_instagram_username(value)

    @field_validator("ig_user_id")
    @classmethod
    def canonicalize_user_id(cls, value: str) -> str:
        return canonicalize_instagram_user_id(value)

    @field_validator("cookies")
    @classmethod
    def require_instagram_session_cookie(
        cls, cookies: list[CookieCapture], info: ValidationInfo
    ) -> list[CookieCapture]:
        session_cookies = [cookie for cookie in cookies if cookie.name == "sessionid"]
        if len(session_cookies) != 1:
            raise ValueError("Instagram sessionid cookie is required")
        session_cookie = session_cookies[0]
        if not session_cookie.secure or not session_cookie.http_only:
            raise ValueError("Instagram sessionid cookie must be secure and HTTP-only")
        identity_cookies = [cookie for cookie in cookies if cookie.name == "ds_user_id"]
        if (
            len(identity_cookies) != 1
            or identity_cookies[0].value != info.data.get("ig_user_id")
        ):
            raise ValueError("Instagram identity must match the session cookie")
        return cookies


class SessionAccountResponse(BaseModel):
    id: UUID
    username: str
    health: str
    captured_at: datetime
    proxy_bound: bool


class AccountResponse(BaseModel):
    id: UUID
    username: str
    health: str
    captured_at: datetime | None
    proxy_bound: bool
    paused_at: datetime | None
    pause_reason: str | None


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._]+$")

    @field_validator("username")
    @classmethod
    def canonicalize_username(cls, value: str) -> str:
        return canonicalize_instagram_username(value)


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


def _session_response(account: AccountRecord) -> SessionAccountResponse:
    if account.captured_at is None:
        raise RuntimeError("persisted session is missing its capture timestamp")
    return SessionAccountResponse(
        id=account.id,
        username=account.username,
        health=account.health,
        captured_at=account.captured_at,
        proxy_bound=account.proxy_bound,
    )


def _account_response(account: AccountRecord) -> AccountResponse:
    return AccountResponse(
        id=account.id,
        username=account.username,
        health=account.health,
        captured_at=account.captured_at,
        proxy_bound=account.proxy_bound,
        paused_at=account.paused_at,
        pause_reason=account.pause_reason,
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")


class SuggestLoginGeoRequest(BaseModel):
    """A location hint for picking an account's login geography.

    Either an explicit point or an IANA time zone. Time zone is what the
    extension sends, because a browser gives it up without a permission prompt.
    """

    model_config = ConfigDict(extra="forbid")

    timezone: str | None = Field(default=None, max_length=64)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class LoginGeoOption(BaseModel):
    country: str
    region: str
    city: str
    free_leases: int


class SuggestLoginGeoResponse(BaseModel):
    """The recommended login geography, plus every option for a manual picker.

    `recommended` is null when we could not resolve the hint to a point or hold
    no free inventory anywhere; the caller then shows `options` and lets the
    operator choose. `within_guard` false means the nearest lease is far enough
    that the operator should weigh buying one in the real metro instead.
    """

    recommended: LoginGeoOption | None
    distance_miles: float | None
    within_guard: bool
    options: list[LoginGeoOption]


@router.post("/suggest-login-geo", response_model=SuggestLoginGeoResponse)
def suggest_login_geo(
    request: SuggestLoginGeoRequest,
    _user_id: Annotated[UUID, Depends(current_user_id)],
    proxies: Annotated[ProxyRepository, Depends(proxy_repository)],
    anchors: Annotated[GeoAnchorRegistry, Depends(geo_anchor_registry)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SuggestLoginGeoResponse:
    free = proxies.free_leases_by_geo()
    if settings.iproyal_mode == "import":
        configured_free = IPRoyalProvider.from_settings(settings).free_leases_by_geo(
            excluded_references=proxies.active_provider_references("iproyal")
        )
        for geo, count in configured_free.items():
            free[geo] = free.get(geo, 0) + count
    anchor_map = anchors.as_mapping()
    options = [
        GeoOption(
            geo=geo,
            latitude=anchor_map[geo][0],
            longitude=anchor_map[geo][1],
            free_leases=count,
        )
        for geo, count in free.items()
        if geo in anchor_map and count > 0
    ]
    option_models = [
        LoginGeoOption(
            country=o.geo.country,
            region=o.geo.region,
            city=o.geo.city,
            free_leases=o.free_leases,
        )
        for o in sorted(
            options, key=lambda o: (o.geo.country, o.geo.region, o.geo.city)
        )
    ]

    point: tuple[float, float] | None = None
    if request.latitude is not None and request.longitude is not None:
        point = (request.latitude, request.longitude)
    elif request.timezone:
        point = resolve_timezone_point(request.timezone)

    recommendation = (
        recommend_login_geo(point[0], point[1], options) if point else None
    )
    if recommendation is None:
        return SuggestLoginGeoResponse(
            recommended=None,
            distance_miles=None,
            within_guard=False,
            options=option_models,
        )
    return SuggestLoginGeoResponse(
        recommended=LoginGeoOption(
            country=recommendation.geo.country,
            region=recommendation.geo.region,
            city=recommendation.geo.city,
            free_leases=recommendation.free_leases,
        ),
        distance_miles=round(recommendation.distance_miles, 1),
        within_guard=recommendation.within_guard,
        options=option_models,
    )


@router.post("/session", response_model=SessionAccountResponse)
def capture_session(
    request: SessionCapture,
    response: Response,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[AccountRepository, Depends(account_repository)],
    geo_registry: Annotated[
        FingerprintGeoRegistry, Depends(fingerprint_geo_registry)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionAccountResponse:
    unsupported_geo = False
    try:
        geo_registry.require(
            Geo(
                request.login_geo.country,
                request.login_geo.region,
                request.login_geo.city,
            )
        )
    except (TypeError, ValueError):
        unsupported_geo = True
    if unsupported_geo:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="unsupported login geography",
        )
    session_cookie = next(
        cookie for cookie in request.cookies if cookie.name == "sessionid"
    )
    expires_at = (
        datetime.fromtimestamp(session_cookie.expiration_date, tz=timezone.utc)
        if session_cookie.expiration_date is not None
        else None
    )
    try:
        account, created = repository.upsert_session(
            user_id=user_id,
            username=request.username,
            ig_user_id=request.ig_user_id,
            cookies=[
                cookie.model_dump(by_alias=True, exclude_none=True)
                for cookie in request.cookies
            ],
            user_agent=request.user_agent,
            login_country=request.login_geo.country,
            login_region=request.login_geo.region,
            login_city=request.login_geo.city,
            expires_at=expires_at,
        )
    except AccountLimitReachedError as error:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"plan allows {error.limit} Instagram accounts",
        ) from error
    except UniqueViolation as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username is already connected to another Instagram account",
        ) from error
    response.status_code = (
        status.HTTP_201_CREATED if created else status.HTTP_200_OK
    )
    return _session_response(account)


@router.get("/{account_id}", response_model=AccountResponse)
def get_account(
    account_id: UUID,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[AccountRepository, Depends(account_repository)],
) -> AccountResponse:
    account = repository.get(user_id, account_id)
    if account is None:
        raise _not_found()
    return _account_response(account)


@router.patch("/{account_id}", response_model=AccountResponse)
def update_account(
    account_id: UUID,
    request: AccountPatch,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[AccountRepository, Depends(account_repository)],
) -> AccountResponse:
    try:
        account = repository.rename(user_id, account_id, request.username)
    except UniqueViolation as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="username is already connected",
        ) from error
    if account is None:
        raise _not_found()
    return _account_response(account)


@router.post("/{account_id}/pause", response_model=AccountResponse)
def pause_account(
    account_id: UUID,
    request: PauseRequest,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[AccountRepository, Depends(account_repository)],
) -> AccountResponse:
    account = repository.set_paused(
        user_id, account_id, paused=True, reason=request.reason
    )
    if account is None:
        raise _not_found()
    return _account_response(account)


@router.post("/{account_id}/resume", response_model=AccountResponse)
def resume_account(
    account_id: UUID,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[AccountRepository, Depends(account_repository)],
) -> AccountResponse:
    try:
        account = repository.set_paused(user_id, account_id, paused=False)
    except AccountSessionIdentityMismatchError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    if account is None:
        raise _not_found()
    return _account_response(account)
