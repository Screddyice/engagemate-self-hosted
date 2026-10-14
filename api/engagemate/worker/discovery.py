"""Tenant-scoped active-story discovery and deterministic ranking."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar
from uuid import UUID

from engagemate.domain.budgets import ActionBudget
from engagemate.worker.story_fingerprint import (
    StoryFingerprintError,
    fingerprint_story,
)
from engagemate.worker.story_types import (
    ActiveStory,
    DiscoveryContext,
    DiscoveryHistory,
    DiscoveryTarget,
    StoryBrowserAdapter,
    StoryContent,
    StoryHistoryRepository,
    StoryMedia,
    StoryMediaType,
    StoryOpportunity,
    TargetEngagement,
)

_TIERS = {"vip": 0, "broad": 1, "discovery": 2}
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_HANDLE = re.compile(r"^[a-z0-9_](?:[a-z0-9._]{0,28}[a-z0-9_])?$")
_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_STORY_LIFETIME = timedelta(hours=24)
_MAX_STORY_TEXT = 4_000
_MAX_MEDIA_BYTES = 5 * 1024 * 1024
_T = TypeVar("_T")
_SAFE_DISCOVERY_REASONS = frozenset({
    "authentication_required", "checkpoint_required", "rate_limited", "story_unavailable",
})


class DiscoveryError(Exception):
    """Raised when discovery cannot safely produce opportunities."""

    def __init__(self, message: str, *, reason: str = "story_unreadable") -> None:
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _DiscoveryOutcome:
    error: str | None
    opportunities: tuple[StoryOpportunity, ...]
    reason: str = "story_unreadable"


class DiscoveryService:
    """Fetch, deduplicate, and rank active stories for one account."""

    def __init__(
        self,
        browser: StoryBrowserAdapter,
        history: StoryHistoryRepository,
    ) -> None:
        self._browser = browser
        self._history = history

    async def find(
        self,
        context: DiscoveryContext,
        targets: tuple[DiscoveryTarget, ...] | list[DiscoveryTarget],
        budget: ActionBudget,
    ) -> list[StoryOpportunity]:
        outcome = await self._find(context, targets, budget)
        del context, targets, budget
        if outcome.error is not None:
            message, reason = outcome.error, outcome.reason
            del outcome
            raise DiscoveryError(message, reason=reason)
        return list(outcome.opportunities)

    async def _find(
        self,
        context: DiscoveryContext,
        targets: tuple[DiscoveryTarget, ...] | list[DiscoveryTarget],
        budget: ActionBudget,
    ) -> _DiscoveryOutcome:
        normalized = _validate_inputs(context, targets, budget)
        if normalized is None:
            return _failure("invalid discovery input")
        now, enabled_targets, capacity = normalized
        if capacity == 0:
            return _DiscoveryOutcome(None, ())

        candidates: dict[str, StoryOpportunity] = {}
        for target in enabled_targets:
            succeeded, returned, restriction = await _bounded_call(
                lambda target=target: self._browser.active_stories(context, target)
            )
            if not succeeded:
                return _failure("story discovery failed", restriction or "story_unreadable")
            if type(returned) not in (tuple, list):
                return _failure("invalid discovery result")
            for active_story in returned:
                opportunity = _normalize_story(active_story, target, now)
                if opportunity is False:
                    return _failure("invalid discovery result")
                if opportunity is None:
                    continue
                incumbent = candidates.get(opportunity.fingerprint)
                if incumbent is None or _canonical_story_key(
                    opportunity
                ) < _canonical_story_key(incumbent):
                    candidates[opportunity.fingerprint] = opportunity

        fingerprints = frozenset(candidates)
        target_ids = frozenset(target.id for target in enabled_targets)
        succeeded, history, _ = await _bounded_call(
            lambda: self._history.lookup(
                context.user_id,
                context.ig_account_id,
                fingerprints,
                target_ids,
            )
        )
        if not succeeded:
            return _failure("story discovery failed")
        engagement_by_target = _validate_history(
            history,
            target_ids,
            fingerprints,
            now,
        )
        if engagement_by_target is None:
            return _failure("invalid discovery result")

        unseen = [
            opportunity
            for fingerprint, opportunity in candidates.items()
            if fingerprint not in history.seen_fingerprints
        ]
        unseen.sort(
            key=lambda opportunity: _ranking_key(
                opportunity,
                engagement_by_target.get(opportunity.target_id),
            )
        )
        return _DiscoveryOutcome(None, tuple(unseen[:capacity]))


def _validate_inputs(
    context: object,
    targets: object,
    budget: object,
) -> tuple[datetime, tuple[DiscoveryTarget, ...], int] | None:
    if type(context) is not DiscoveryContext:
        return None
    if type(context.user_id) is not UUID or type(context.ig_account_id) is not UUID:
        return None
    now = _as_utc(context.now)
    if now is None or type(targets) not in (tuple, list):
        return None
    if type(budget) is not ActionBudget:
        return None
    counts = (budget.likes, budget.replies, budget.dms, budget.plan_total)
    if any(type(value) is not int or value < 0 for value in counts):
        return None

    seen_ids: set[UUID] = set()
    seen_owners: set[tuple[UUID, UUID, str]] = set()
    seen_handles: set[tuple[UUID, UUID, str]] = set()
    enabled: list[DiscoveryTarget] = []
    for target in targets:
        if not _valid_target(target, context):
            return None
        owner_identity = (target.user_id, target.ig_account_id, target.owner_id)
        handle_identity = (target.user_id, target.ig_account_id, target.handle)
        if (
            target.id in seen_ids
            or owner_identity in seen_owners
            or handle_identity in seen_handles
        ):
            return None
        seen_ids.add(target.id)
        seen_owners.add(owner_identity)
        seen_handles.add(handle_identity)
        if target.enabled:
            enabled.append(target)
    # Views are additional to engagements, so an account whose engagement
    # budget is spent still has work while it has views left.
    capacity = budget.engagements + budget.views
    return now, tuple(enabled), capacity


def _valid_target(target: object, context: DiscoveryContext) -> bool:
    return (
        type(target) is DiscoveryTarget
        and type(target.id) is UUID
        and type(target.user_id) is UUID
        and type(target.ig_account_id) is UUID
        and target.user_id == context.user_id
        and target.ig_account_id == context.ig_account_id
        and type(target.tier) is str
        and target.tier in _TIERS
        and type(target.enabled) is bool
        and _valid_identifier(target.owner_id)
        and type(target.handle) is str
        and _HANDLE.fullmatch(target.handle) is not None
    )


def _normalize_story(
    story: object,
    target: DiscoveryTarget,
    now: datetime,
) -> StoryOpportunity | None | bool:
    if (
        type(story) is not ActiveStory
        or type(story.target_id) is not UUID
        or story.target_id != target.id
        or type(story.owner_id) is not str
        or story.owner_id != target.owner_id
        or not _valid_story_content(story.content)
    ):
        return False
    taken_at = _as_utc(story.taken_at)
    if taken_at is None:
        return False
    if taken_at > now or now - taken_at >= _STORY_LIFETIME:
        return None
    try:
        fingerprint = fingerprint_story(
            story.owner_id,
            story.media_id,
            taken_at,
            story.media_hash,
        )
    except StoryFingerprintError:
        return False
    return StoryOpportunity(
        target_id=target.id,
        owner_id=target.owner_id,
        target_handle=target.handle,
        tier=target.tier,
        media_id=story.media_id,
        taken_at=taken_at,
        fingerprint=fingerprint,
        content=story.content,
    )


def _validate_history(
    value: object,
    target_ids: frozenset[UUID],
    queried_fingerprints: frozenset[str],
    now: datetime,
) -> dict[UUID, datetime] | None:
    if type(value) is not DiscoveryHistory:
        return None
    if type(value.seen_fingerprints) is not frozenset or any(
        type(item) is not str or _FINGERPRINT.fullmatch(item) is None
        for item in value.seen_fingerprints
    ):
        return None
    if not value.seen_fingerprints.issubset(queried_fingerprints):
        return None
    if type(value.most_recent_engagements) is not tuple:
        return None

    result: dict[UUID, datetime] = {}
    for engagement in value.most_recent_engagements:
        if type(engagement) is not TargetEngagement:
            return None
        target_id = engagement.target_id
        engaged_at = _as_utc(engagement.engaged_at)
        if (
            type(target_id) is not UUID
            or target_id not in target_ids
            or target_id in result
            or engaged_at is None
            or engaged_at > now
        ):
            return None
        result[target_id] = engaged_at
    return result


def _ranking_key(
    opportunity: StoryOpportunity,
    last_engaged_at: datetime | None,
) -> tuple[object, ...]:
    never_engaged = 0 if last_engaged_at is None else 1
    history_order = (
        datetime.min.replace(tzinfo=UTC) if last_engaged_at is None else last_engaged_at
    )
    return (
        _TIERS[opportunity.tier],
        never_engaged,
        history_order,
        opportunity.target_handle,
        opportunity.target_id.hex,
        opportunity.media_id or "",
        opportunity.fingerprint,
    )


def _canonical_story_key(
    opportunity: StoryOpportunity,
) -> tuple[str, str, str, str, str]:
    return (
        opportunity.target_handle,
        opportunity.target_id.hex,
        opportunity.media_id or "",
        opportunity.fingerprint,
        _content_key(opportunity.content),
    )


def _valid_story_content(value: object) -> bool:
    if type(value) is not StoryContent or type(value.media_type) is not StoryMediaType:
        return False
    if not _valid_optional_story_text(
        value.caption_text
    ) or not _valid_optional_story_text(value.sticker_text):
        return False
    if type(value.video_frames) is not tuple:
        return False
    if value.media_type is StoryMediaType.IMAGE:
        return _valid_story_media(value.image) and value.video_frames == ()
    return (
        value.media_type is StoryMediaType.VIDEO
        and value.image is None
        and 1 <= len(value.video_frames) <= 4
        and all(_valid_story_media(frame) for frame in value.video_frames)
    )


def _valid_story_media(value: object) -> bool:
    return (
        type(value) is StoryMedia
        and type(value.mime_type) is str
        and value.mime_type in _IMAGE_MIME_TYPES
        and type(value.data) is bytes
        and 0 < len(value.data) <= _MAX_MEDIA_BYTES
    )


def _valid_optional_story_text(value: object) -> bool:
    if value is None:
        return True
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= _MAX_STORY_TEXT
        and all(
            character in "\n\t" or not unicodedata.category(character).startswith("C")
            for character in value
        )
    )


def _content_key(content: StoryContent) -> str:
    digest = hashlib.sha256()
    digest.update(content.media_type.value.encode())
    digest.update(b"\0")
    digest.update((content.caption_text or "").encode())
    digest.update(b"\0")
    digest.update((content.sticker_text or "").encode())
    payloads = (content.image,) if content.image is not None else content.video_frames
    for payload in payloads:
        digest.update(b"\0")
        digest.update(payload.mime_type.encode())
        digest.update(b"\0")
        digest.update(payload.data)
    return digest.hexdigest()


def _valid_identifier(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= 256
        and all(
            not unicodedata.category(character).startswith("C") for character in value
        )
    )


def _as_utc(value: object) -> datetime | None:
    if type(value) is not datetime or value.tzinfo is None:
        return None
    try:
        if value.utcoffset() is None:
            return None
        normalized = value.astimezone(UTC)
        normalized.timestamp()
        return normalized
    except Exception:  # noqa: BLE001 - malformed tzinfo must become bounded input
        return None


async def _bounded_call(
    operation: Callable[[], Awaitable[_T]],
) -> tuple[bool, _T | None, str | None]:
    try:
        return True, await operation(), None
    except Exception as error:  # noqa: BLE001 - preserve only bounded discovery reasons
        try:
            reason = getattr(error, "reason", None)
            safe_reason = reason if type(reason) is str and reason in _SAFE_DISCOVERY_REASONS else None
        except Exception:  # noqa: BLE001 - an untrusted reason property may itself fail
            safe_reason = None
        return False, None, safe_reason


def _failure(message: str, reason: str = "story_unreadable") -> _DiscoveryOutcome:
    return _DiscoveryOutcome(message, (), reason)
