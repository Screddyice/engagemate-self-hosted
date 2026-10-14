"""Immutable domain values and narrow ports for story discovery."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class StoryMediaType(StrEnum):
    """Media shape supplied to the vision decision boundary."""

    IMAGE = "image"
    VIDEO = "video"


@dataclass(frozen=True, slots=True, repr=False)
class StoryMedia:
    """One immutable image payload with a credential-safe representation."""

    mime_type: str
    data: bytes

    def __repr__(self) -> str:
        size = len(self.data) if type(self.data) is bytes else "invalid"
        return f"StoryMedia(<redacted>, size={size})"


@dataclass(frozen=True, slots=True, repr=False)
class StoryContent:
    """Normalized multimodal evidence for a Task 10 decision."""

    media_type: StoryMediaType
    caption_text: str | None
    sticker_text: str | None
    image: StoryMedia | None
    video_frames: tuple[StoryMedia, ...]

    def __repr__(self) -> str:
        return "StoryContent(<redacted>)"


@dataclass(frozen=True, slots=True)
class DiscoveryContext:
    """Tenant identity and the run's fixed clock value."""

    user_id: UUID
    ig_account_id: UUID
    now: datetime


@dataclass(frozen=True, slots=True)
class DiscoveryTarget:
    """One configured Instagram target."""

    id: UUID
    user_id: UUID
    ig_account_id: UUID
    owner_id: str
    handle: str
    tier: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class ActiveStory:
    """Normalized story metadata returned by the browser adapter."""

    target_id: UUID
    owner_id: str
    media_id: str | None
    taken_at: datetime
    media_hash: str | None
    content: StoryContent


@dataclass(frozen=True, slots=True)
class TargetEngagement:
    """A target's most recent successful engagement."""

    target_id: UUID
    engaged_at: datetime


@dataclass(frozen=True, slots=True)
class DiscoveryHistory:
    """One tenant-scoped batch history result."""

    seen_fingerprints: frozenset[str]
    most_recent_engagements: tuple[TargetEngagement, ...]


@dataclass(frozen=True, slots=True)
class StoryOpportunity:
    """A ranked, unseen active story."""

    target_id: UUID
    owner_id: str
    target_handle: str
    tier: str
    media_id: str | None
    taken_at: datetime
    fingerprint: str
    content: StoryContent


class StoryBrowserAdapter(Protocol):
    """Infrastructure port that extracts normalized active stories."""

    async def active_stories(
        self, context: DiscoveryContext, target: DiscoveryTarget
    ) -> Sequence[ActiveStory]: ...


class StoryHistoryRepository(Protocol):
    """Infrastructure port for one tenant-scoped history lookup."""

    async def lookup(
        self,
        user_id: UUID,
        ig_account_id: UUID,
        fingerprints: frozenset[str],
        target_ids: frozenset[UUID],
    ) -> DiscoveryHistory: ...
