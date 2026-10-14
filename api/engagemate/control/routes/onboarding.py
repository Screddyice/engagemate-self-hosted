"""Hermes-guided brand onboarding from authenticated public profile snapshots."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from engagemate.config import Settings
from engagemate.control.dependencies import (
    account_repository,
    brand_profile_repository,
    current_user_id,
    get_settings,
    onboarding_launch_repository,
)
from engagemate.db.accounts import AccountRepository
from engagemate.db.brand_profiles import BrandProfileRepository
from engagemate.db.onboarding_launch import OnboardingLaunchRepository
from engagemate.onboarding.analysis import analyze_brand
from engagemate.onboarding.brand import (
    MAX_REFERENCE_ACCOUNTS,
    MIN_REFERENCE_ACCOUNTS,
    align_profiles,
    canonicalize_reference_urls,
    reference_username,
)
from engagemate.onboarding.candidates import select_balanced_candidates
from engagemate.onboarding.hypercrawl import HyperCrawlInstagramClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


REQUIRED_ANSWERS = ("brand", "audience", "interests", "voice", "goals")
MAX_PUBLIC_COUNT = 2_147_483_647


class ReferencePost(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    likes_count: int = Field(alias="likesCount", ge=0, le=MAX_PUBLIC_COUNT)
    comments_count: int = Field(alias="commentsCount", ge=0, le=MAX_PUBLIC_COUNT)
    caption: str = Field(max_length=2_200)


class ReferenceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9._]+$")
    full_name: str = Field(alias="fullName", max_length=256)
    biography: str = Field(max_length=2_200)
    followers_count: int = Field(alias="followersCount", ge=0, le=MAX_PUBLIC_COUNT)
    following_count: int = Field(alias="followingCount", ge=0, le=MAX_PUBLIC_COUNT)
    posts_count: int = Field(alias="postsCount", ge=0, le=MAX_PUBLIC_COUNT)
    is_private: bool = Field(alias="isPrivate")
    is_verified: bool = Field(alias="isVerified")
    latest_posts: list[ReferencePost] = Field(
        default_factory=list,
        alias="latestPosts",
        max_length=12,
    )


class BrandAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ig_account_id: UUID = Field(strict=False)
    reference_urls: list[str] = Field(
        min_length=MIN_REFERENCE_ACCOUNTS,
        max_length=MAX_REFERENCE_ACCOUNTS,
    )
    reference_profiles: list[ReferenceProfile] = Field(
        min_length=MIN_REFERENCE_ACCOUNTS,
        max_length=MAX_REFERENCE_ACCOUNTS,
    )
    answers: dict[str, str] = Field(default_factory=dict, max_length=12)
    audience_mode: Literal["followers", "manual"] = "followers"
    source_revision: datetime | None = Field(default=None, strict=False)


class CandidateResponse(BaseModel):
    candidate_id: UUID
    username: str
    score: int
    accepted: bool
    imported: bool = False
    us_signal: bool
    engagement_rate: float
    reasons: list[str]


class BrandAnalyzeResponse(BaseModel):
    profile_id: UUID
    ig_account_id: UUID
    reference_urls: list[str]
    analysis: dict[str, object]
    # Manual discovery may remain pending; source-audience analysis completes here.
    status: str = "complete"
    candidates: list[CandidateResponse]


class BrandProfileResponse(BaseModel):
    profile_id: UUID
    ig_account_id: UUID
    status: str
    reference_urls: list[str]
    answers: dict[str, str]
    analysis: dict[str, object] | None
    candidates: list[CandidateResponse]
    follower_audience_approved: bool = False


class BrandApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile_id: UUID = Field(strict=False)
    candidate_ids: list[Annotated[UUID, Field(strict=False)]] = Field(
        min_length=1,
        max_length=50,
    )
    ig_account_id: UUID = Field(strict=False)


class BrandApproveResponse(BaseModel):
    imported: int


class SourceSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ig_account_id: UUID = Field(strict=False)
    reference_urls: list[str] = Field(
        min_length=MIN_REFERENCE_ACCOUNTS, max_length=MAX_REFERENCE_ACCOUNTS,
    )
    answers: dict[str, str] = Field(default_factory=dict, max_length=12)


@router.post("/brand/sources")
def save_onboarding_sources(
    request: SourceSaveRequest,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[BrandProfileRepository, Depends(brand_profile_repository)],
) -> dict[str, object]:
    try:
        references = canonicalize_reference_urls(request.reference_urls)
        profile_id, revision = repository.save_sources(
            user_id=user_id, ig_account_id=request.ig_account_id,
            reference_urls=references,
            answers={key.strip()[:80]: value.strip()[:1_000] for key, value in request.answers.items()},
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {
        "profile_id": str(profile_id), "ig_account_id": str(request.ig_account_id),
        "reference_urls": references, "status": "pending", "source_revision": revision.isoformat(),
    }


class ManualSetupRequest(SourceSaveRequest):
    persona: str = Field(min_length=1, max_length=2_000)
    approve: Literal[True]


@router.post("/manual")
def save_manual_setup(
    request: ManualSetupRequest,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[BrandProfileRepository, Depends(brand_profile_repository)],
) -> dict[str, bool]:
    """Save explicit owner-selected sources without remote retrieval or analysis."""
    from engagemate.onboarding.brand import reference_username
    try:
        urls = canonicalize_reference_urls(request.reference_urls)
        persona = request.persona.strip()
        if not persona:
            raise ValueError("describe your voice before continuing")
        analysis = {"voice_summary": persona, "audience_summary": "Owner-approved source followers",
                    "brand_identity": "Manual setup; no model analysis", "engagement_style": persona,
                    "content_pillars": [], "conversation_topics": [], "search_terms": [],
                    "us_audience_signals": [], "next_question": "Review your saved limits before launch"}
        profile_id, _ = repository.save(user_id=user_id, ig_account_id=request.ig_account_id,
            references=[{"url":url,"username":reference_username(url)} for url in urls],
            answers={"setup_mode":"manual"}, analysis=analysis, candidates=[], status="complete")
        repository.approve_sources(user_id=user_id, profile_id=profile_id,
                                   ig_account_id=request.ig_account_id, reference_urls=urls)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"approved":True}


class SourceApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profile_id: UUID = Field(strict=False)
    ig_account_id: UUID = Field(strict=False)
    reference_urls: list[str] = Field(
        min_length=MIN_REFERENCE_ACCOUNTS, max_length=MAX_REFERENCE_ACCOUNTS
    )


@router.post("/brand/approve-sources")
def approve_onboarding_sources(
    request: SourceApproveRequest,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[BrandProfileRepository, Depends(brand_profile_repository)],
) -> dict[str, bool]:
    try:
        references = canonicalize_reference_urls(request.reference_urls)
        repository.approve_sources(
            user_id=user_id, profile_id=request.profile_id,
            ig_account_id=request.ig_account_id, reference_urls=references,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return {"approved": True}


class OnboardingLaunchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ig_account_id: UUID = Field(strict=False)
    timezone: str = Field(min_length=1, max_length=64)


class OnboardingLaunchResponse(BaseModel):
    enabled: bool
    timezone: str
    schedule_hour: int
    daily_like_limit: int
    daily_reply_limit: int
    daily_dm_limit: int
    daily_view_limit: int


@router.post("/brand/analyze", response_model=BrandAnalyzeResponse)
async def analyze_onboarding_brand(
    request: BrandAnalyzeRequest,
    background: BackgroundTasks,
    user_id: Annotated[UUID, Depends(current_user_id)],
    settings: Annotated[Settings, Depends(get_settings)],
    accounts: Annotated[AccountRepository, Depends(account_repository)],
    repository: Annotated[
        BrandProfileRepository, Depends(brand_profile_repository)
    ],
) -> BrandAnalyzeResponse:
    account = await asyncio.to_thread(accounts.get, user_id, request.ig_account_id)
    if account is None or account.captured_at is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="connect Instagram before brand analysis",
        )
    if settings.hermes_agent_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="brand analysis is not configured",
        )
    try:
        reference_urls = canonicalize_reference_urls(request.reference_urls)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    reference_usernames = [reference_username(url) for url in reference_urls]
    if [profile.username for profile in request.reference_profiles] != reference_usernames:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="reference profiles must match reference URLs",
        )
    profiles = [
        profile.model_dump(by_alias=True)
        for profile in request.reference_profiles
    ]
    answers = {
        key.strip()[:80]: value.strip()[:1_000]
        for key, value in request.answers.items()
        if value.strip()
    }
    missing = [key for key in REQUIRED_ANSWERS if not answers.get(key)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"onboarding answers are required: {', '.join(missing)}",
        )

    async with httpx.AsyncClient(
        timeout=settings.hermes_agent_timeout_seconds
    ) as http:
        try:
            analysis = await analyze_brand(
                http=http,
                endpoint=settings.hermes_agent_url,
                api_key=settings.hermes_agent_api_key,
                model=settings.hermes_agent_model,
                reference_urls=reference_urls,
                answers=answers,
                scraped_profiles=profiles,
            )
        except RuntimeError as error:
            raise HTTPException(
                status_code=502,
                detail="brand analysis is temporarily unavailable",
            ) from error
        except httpx.RequestError as error:
            raise HTTPException(
                status_code=502,
                detail="brand analysis is temporarily unavailable",
            ) from error

    # Approve the saved source audience before the worker collects followers.
    discovery_configured = request.audience_mode == "manual" and settings.hypercrawl_url is not None
    try:
        profile_id, discovery_revision = await asyncio.to_thread(
            repository.save,
            user_id=user_id,
            ig_account_id=request.ig_account_id,
            references=[
                {
                    "url": url,
                    "username": reference_username(url),
                    "scraped": scraped,
                }
                for url, scraped in zip(
                    reference_urls,
                    align_profiles(reference_urls, profiles),
                )
            ],
            answers=answers,
            analysis=analysis.model_dump(),
            candidates=[],
            expected_revision=request.source_revision,
            status="pending" if discovery_configured else "complete",
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if discovery_configured:
        background.add_task(
            _discover_candidates,
            repository=repository, profile_id=profile_id,
            expected_revision=discovery_revision, settings=settings,
            search_terms=list(analysis.search_terms), reference_urls=list(reference_urls),
        )
    return BrandAnalyzeResponse(
        profile_id=profile_id,
        ig_account_id=request.ig_account_id,
        reference_urls=reference_urls,
        analysis=analysis.model_dump(),
        status="pending" if discovery_configured else "complete",
        candidates=[],
    )


async def _discover_candidates(
    *,
    repository: BrandProfileRepository,
    profile_id: UUID,
    expected_revision: object,
    settings: Settings,
    search_terms: list[str],
    reference_urls: list[str],
) -> None:
    """Find, score and store candidates after the response has gone out.

    Runs in-process. That is a real limitation and the reader should know it: if
    the API restarts mid-run nothing resumes this, and the row would sit at
    'pending' forever. It does not, because a reader treats a pending row older
    than BrandProfileRepository.PENDING_STALE_AFTER as failed -- the UI stops
    spinning on work that no longer exists.

    Every failure is caught. This runs after the response, so raising here would
    log a traceback nobody is waiting on and leave the row pending; recording
    'failed' is what the poller can actually act on.
    """

    try:
        async with httpx.AsyncClient() as http:
            hypercrawl = HyperCrawlInstagramClient(
                http=http,
                base_url=settings.hypercrawl_url,
                rest_token=settings.hypercrawl_rest_token,
                timeout_seconds=settings.hypercrawl_timeout_seconds,
            )
            pooled = await hypercrawl.candidate_profiles(
                search_terms,
                exclude=frozenset(reference_username(u) for u in reference_urls),
            )
        candidates = select_balanced_candidates(
            [("search", pooled)] if pooled else [],
            reference_usernames={reference_username(url) for url in reference_urls},
        )
        await asyncio.to_thread(
            repository.complete_discovery,
            profile_id,
            expected_revision=expected_revision,
            candidates=candidates,
        )
    except Exception:
        logger.exception("candidate discovery failed for brand profile %s", profile_id)
        try:
            await asyncio.to_thread(
                repository.fail_discovery,
                profile_id,
                expected_revision=expected_revision,
            )
        except Exception:
            logger.exception("could not mark brand profile %s failed", profile_id)


@router.post("/brand/approve", response_model=BrandApproveResponse)
def approve_onboarding_candidates(
    request: BrandApproveRequest,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[
        BrandProfileRepository, Depends(brand_profile_repository)
    ],
) -> BrandApproveResponse:
    try:
        imported = repository.approve_candidates(
            user_id=user_id,
            profile_id=request.profile_id,
            candidate_ids=request.candidate_ids,
            ig_account_id=request.ig_account_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return BrandApproveResponse(imported=imported)


@router.get("/brand/profile", response_model=BrandProfileResponse)
def get_onboarding_brand_profile(
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[
        BrandProfileRepository, Depends(brand_profile_repository)
    ],
    ig_account_id: UUID,
    details: bool = True,
) -> BrandProfileResponse:
    """Poll the profile while candidate discovery runs.

    `status` is what the caller waits on: 'pending' means discovery is still
    going, 'complete' means the candidate list below is final, and 'failed'
    means it will not arrive -- including the case where the API restarted
    mid-discovery and nothing is left to finish it.
    """

    record = (
        repository.load_for_user(user_id, ig_account_id)
        if details
        else repository.load_status_for_user(user_id, ig_account_id)
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="no brand profile for this Instagram account",
        )
    return BrandProfileResponse(
        profile_id=record["profile_id"],
        ig_account_id=record["ig_account_id"],
        status=record["status"],
        reference_urls=record.get("reference_urls", []),
        answers=record.get("answers", {}),
        analysis=record.get("analysis"),
        follower_audience_approved=record.get("follower_audience_approved", False),
        candidates=[
            CandidateResponse.model_validate(candidate)
            for candidate in record.get("candidates", [])[:20]
        ],
    )


@router.post("/launch", response_model=OnboardingLaunchResponse)
def launch_onboarding_account(
    request: OnboardingLaunchRequest,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[
        OnboardingLaunchRepository, Depends(onboarding_launch_repository)
    ],
) -> OnboardingLaunchResponse:
    timezone_name = request.timezone.strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise HTTPException(status_code=422, detail="timezone must be a valid IANA name") from error

    try:
        launched = repository.launch(
            user_id=user_id,
            ig_account_id=request.ig_account_id,
            timezone_name=timezone_name,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return OnboardingLaunchResponse.model_validate(launched)
