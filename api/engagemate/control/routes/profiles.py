"""Owned-account profile capture through the existing IPRoyal browser binding."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from engagemate.browser.lease import BrowserBusy
from engagemate.control.dependencies import current_user_id, get_settings
from engagemate.onboarding.profile_capture import (
    ProfileCaptureError,
    ProfileCaptureService,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ig_account_id: UUID = Field(strict=False)
    usernames: list[Annotated[str, Field(pattern=r"^[a-z0-9_][a-z0-9._]{0,29}$")]] = (
        Field(min_length=3, max_length=5)
    )


def profile_service(request: Request):
    return ProfileCaptureService(request.app.state.db_pool, get_settings(request))


@router.post("/profiles")
async def capture_profiles(
    body: ProfileRequest,
    user_id: Annotated[UUID, Depends(current_user_id)],
    service: Annotated[ProfileCaptureService, Depends(profile_service)],
):
    if len(set(body.usernames)) != len(body.usernames):
        raise HTTPException(422, "source accounts must be unique")
    try:
        profiles = await service.capture(user_id, body.ig_account_id, body.usernames)
        return {"profiles": profiles}
    except BrowserBusy:
        raise HTTPException(
            409,
            detail={
                "code": "account_browser_busy",
                "message": "An account check is already running. Try again shortly.",
            },
            headers={"Retry-After": "30"},
        ) from None
    except ProfileCaptureError as error:
        messages = {
            "source_accounts_changed": "Save your current inspiration accounts before checking profiles.",
            "reconnect_required": "Reconnect a healthy Instagram session before checking profiles.",
            "proxy_binding_required": "Connect your account's proxy before checking profiles.",
            "profile_check_cooldown": "Profile checks are cooling down. Your source accounts are saved.",
            "rate_limited": "Instagram limited this account. Profile checks stopped and your source accounts are saved.",
        }
        allowed = set(messages) | {
            "checkpoint_required",
            "authentication_required",
            "profile_unreadable",
            "profile_check_timeout",
        }
        reason = error.reason if error.reason in allowed else "profile_unreadable"
        retry_after = (
            max(1, min(error.retry_after, 1800))
            if type(error.retry_after) is int
            else None
        )
        raise HTTPException(
            429 if retry_after else 422,
            detail={
                "code": reason,
                "message": messages.get(
                    reason,
                    "Profile checks stopped. Your source accounts are saved; check account status before retrying.",
                ),
            },
            headers={"Retry-After": str(retry_after)} if retry_after else None,
        ) from None
