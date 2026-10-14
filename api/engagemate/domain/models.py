"""Core worker values without infrastructure dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping
from uuid import UUID


VIEW_SLOT_COUNT = 4


def _text(value: str | bytes) -> str:
    """Normalize text from UTF-8 and SQL_ASCII PostgreSQL clusters."""

    return value.decode("utf-8") if isinstance(value, bytes) else value


class Action(StrEnum):
    LIKE = "like"
    REPLY = "reply"
    DM = "dm"
    # Watching a Story sends nothing and is never billed, so it carries its own
    # daily allowance instead of drawing on the plan's engagement total.
    VIEW = "view"


class AccountHealth(StrEnum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    PAUSED = "paused"
    CHALLENGED = "challenged"
    BANNED = "banned"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    id: UUID
    user_id: UUID
    ig_account_id: UUID
    schedule_key: str
    scheduled_for: datetime
    status: JobStatus
    claimed_by: str
    lease_expires_at: datetime
    heartbeat_at: datetime
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ClaimedJob":
        """Build a typed job from a psycopg dictionary row."""

        return cls(
            id=row["id"],
            user_id=row["user_id"],
            ig_account_id=row["ig_account_id"],
            schedule_key=_text(row["schedule_key"]),
            scheduled_for=row["scheduled_for"],
            status=JobStatus(_text(row["status"])),
            claimed_by=_text(row["claimed_by"]),
            lease_expires_at=row["lease_expires_at"],
            heartbeat_at=row["heartbeat_at"],
            attempts=row["attempts"],
            last_error=(
                _text(row["last_error"]) if row["last_error"] is not None else None
            ),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def is_view_job(schedule_key: str) -> bool:
    """Return whether a scheduled job belongs to the dedicated view lane."""

    return isinstance(schedule_key, str) and schedule_key.startswith("views:")
