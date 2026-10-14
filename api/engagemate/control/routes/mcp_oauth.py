"""Trusted web-tier routes for MCP customer consent."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from mcp.server.auth.provider import construct_redirect_uri
from pydantic import BaseModel, Field

from engagemate.control.dependencies import current_user_id
from engagemate.mcp.oauth_provider import CODE_TTL, VALID_SCOPES
from engagemate.mcp.oauth_repository import OAuthRepository


router = APIRouter(prefix="/mcp/oauth", tags=["mcp-oauth"])
State = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")]


class ApprovalRequest(BaseModel):
    state: State
    approved: bool = True
    approved_scopes: list[str] = Field(default_factory=list, max_length=5)


def oauth_repository(request: Request) -> Any:
    injected = getattr(request.app.state, "oauth_repository", None)
    if injected is not None:
        return injected
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        )
    return OAuthRepository(pool)


async def _pending_or_404(repository: Any, state_value: str) -> dict[str, Any]:
    pending = await repository.get_authorization_request(state_value)
    if pending is None:
        raise HTTPException(status_code=404, detail="authorization request not found")
    return pending


@router.get("/pending")
async def pending_consent(
    state_value: Annotated[State, Query(alias="state")],
    _user_id: UUID = Depends(current_user_id),
    repository: Any = Depends(oauth_repository),
) -> dict[str, Any]:
    pending = await _pending_or_404(repository, state_value)
    client = await repository.get_client(pending["client_id"])
    if client is None:
        raise HTTPException(status_code=404, detail="OAuth client not found")
    return {
        "client_name": client.get("client_name") or "Connected agent",
        "requested_scopes": list(pending["scopes"]),
    }


@router.post("/approve")
async def approve_consent(
    body: ApprovalRequest,
    user_id: UUID = Depends(current_user_id),
    repository: Any = Depends(oauth_repository),
) -> dict[str, str]:
    pending = await _pending_or_404(repository, body.state)
    if body.approved:
        if not body.approved_scopes:
            raise HTTPException(status_code=400, detail="at least one scope is required")
        approved = list(dict.fromkeys(body.approved_scopes))
        if not set(approved).issubset(VALID_SCOPES) or not set(approved).issubset(
            pending["scopes"]
        ):
            raise HTTPException(status_code=400, detail="approved scopes are invalid")
        code = "em_ac_" + secrets.token_urlsafe(32)
        completed = await repository.approve_authorization_request(
            body.state,
            user_id,
            approved,
            code,
            datetime.now(UTC) + CODE_TTL,
        )
        if completed is None:
            raise HTTPException(status_code=404, detail="authorization request not found")
        return {
            "continue_url": construct_redirect_uri(
                completed["redirect_uri"],
                code=completed["code"],
                state=completed.get("client_state"),
            )
        }

    denied = await repository.deny_authorization_request(body.state)
    if denied is None:
        raise HTTPException(status_code=404, detail="authorization request not found")
    return {
        "continue_url": construct_redirect_uri(
            denied["redirect_uri"],
            error="access_denied",
            state=denied.get("client_state"),
        )
    }
