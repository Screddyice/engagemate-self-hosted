"""Tenant-scoped worker job control endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from psycopg.errors import UniqueViolation

from engagemate.control.dependencies import account_repository, current_user_id
from engagemate.db.accounts import (
    AccountNotRunnableError,
    AccountRepository,
    JobRecord,
)


account_jobs_router = APIRouter(prefix="/accounts", tags=["jobs"])
jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheduled_for: datetime | None = None
    schedule_key: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("scheduled_for")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("scheduled_for must include a timezone")
        return value


class JobResponse(BaseModel):
    id: UUID
    ig_account_id: UUID
    schedule_key: str
    scheduled_for: datetime
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _response(job: JobRecord) -> JobResponse:
    return JobResponse(
        id=job.id,
        ig_account_id=job.ig_account_id,
        schedule_key=job.schedule_key,
        scheduled_for=job.scheduled_for,
        status=job.status,
        attempts=job.attempts,
        last_error=job.last_error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@account_jobs_router.post(
    "/{account_id}/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_job(
    account_id: UUID,
    request: JobCreate,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[AccountRepository, Depends(account_repository)],
) -> JobResponse:
    try:
        job = repository.create_job(
            user_id,
            account_id,
            scheduled_for=request.scheduled_for,
            schedule_key=request.schedule_key,
        )
    except UniqueViolation as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="schedule key already exists",
        ) from error
    except AccountNotRunnableError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="account is not runnable",
        ) from error
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="account not found",
        )
    return _response(job)


@jobs_router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: UUID,
    user_id: Annotated[UUID, Depends(current_user_id)],
    repository: Annotated[AccountRepository, Depends(account_repository)],
) -> JobResponse:
    job = repository.get_job(user_id, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="job not found",
        )
    return _response(job)
