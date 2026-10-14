"""Bind measured Story-seen requests and opportunities to exact media identities."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from engagemate.worker.story_fingerprint import fingerprint_story
from engagemate.worker.story_types import StoryOpportunity

_IDENTIFIER = re.compile(r"[1-9][0-9]{0,31}\Z")
_HANDLE = re.compile(r"[a-z0-9_][a-z0-9._]{0,29}\Z", re.ASCII)
_FRIENDLY_NAME = "PolarisStoriesV3SeenMutation"
_MAX_FORM_LENGTH = 131_072
_MAX_VARIABLES_LENGTH = 16_384


class StoryIdentityError(ValueError):
    """Expose a bounded reason, never request forms or browser diagnostics."""

    def __init__(self, reason: str = "story_identity_unreadable") -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class StoryIdentity:
    media_id: str
    owner_id: str
    taken_at: datetime
    fingerprint: str


def _identifier(value: object) -> str:
    if type(value) is int:
        if not 0 < value < 10**32:
            raise StoryIdentityError()
        value = str(value)
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise StoryIdentityError()
    return value


def _handle(value: object) -> str:
    if type(value) is not str or _HANDLE.fullmatch(value.lower()) is None:
        raise StoryIdentityError()
    handle = value.lower()
    if handle in {"highlights", "stories", "archive"}:
        raise StoryIdentityError()
    return handle


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None:
        raise StoryIdentityError()
    try:
        if value.utcoffset() is None:
            raise StoryIdentityError()
        return value.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        raise StoryIdentityError() from None


def _identity(
    owner_id: object, media_id: object, taken_at: object, now: datetime
) -> StoryIdentity:
    owner, media = _identifier(owner_id), _identifier(media_id)
    taken, clock = _utc(taken_at), _utc(now)
    if not timedelta(0) <= clock - taken < timedelta(hours=24):
        raise StoryIdentityError("story_expired_or_future")
    return StoryIdentity(
        media, owner, taken, fingerprint_story(owner, media, taken, None)
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise StoryIdentityError()
        result[key] = value
    return result


def _verify_url(page_url: object, handle: str, media_id: str) -> None:
    if (
        type(page_url) is not str
        or len(page_url) > 2048
        or any(ord(c) < 32 for c in page_url)
    ):
        raise StoryIdentityError()
    try:
        parsed = urlsplit(page_url)
        query = parse_qs(
            parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4
        )
    except ValueError:
        raise StoryIdentityError() from None
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}
        or parsed.fragment
        or query not in ({}, {"r": ["1"]})
    ):
        raise StoryIdentityError()
    path = parsed.path.removesuffix("/")
    if path not in {f"/stories/{handle}", f"/stories/{handle}/{media_id}"}:
        raise StoryIdentityError("story_identity_mismatch")


def parse_seen_mutation(
    post_data: str,
    *,
    expected_owner_id: str,
    expected_handle: str,
    page_url: str,
    now: datetime,
) -> StoryIdentity:
    """Read only the measured seen mutation and bind it to its regular viewer.

    Receiving a request proves its identity payload, not server acceptance of
    the view. The caller must separately verify the response when confirming
    an action. This helper performs no navigation or network operations.
    """
    expected_owner = _identifier(expected_owner_id)
    handle = _handle(expected_handle)
    if type(post_data) is not str or not post_data or len(post_data) > _MAX_FORM_LENGTH:
        raise StoryIdentityError()
    try:
        form = parse_qs(
            post_data, keep_blank_values=True, strict_parsing=True, max_num_fields=128
        )
        if form.get("fb_api_req_friendly_name") != [_FRIENDLY_NAME]:
            raise StoryIdentityError()
        encoded = form.get("variables")
        if (
            encoded is None
            or len(encoded) != 1
            or len(encoded[0]) > _MAX_VARIABLES_LENGTH
        ):
            raise StoryIdentityError()
        variables = json.loads(encoded[0], object_pairs_hook=_unique_object)
        if type(variables) is not dict:
            raise StoryIdentityError()
        owner = _identifier(variables.get("reelMediaOwnerId"))
        reel = _identifier(variables.get("reelId"))
        media = _identifier(variables.get("reelMediaId"))
        timestamp = variables.get("reelMediaTakenAt")
        if type(timestamp) is not int or timestamp <= 0:
            raise StoryIdentityError()
        taken_at = datetime.fromtimestamp(timestamp, UTC)
    except (ValueError, TypeError, OverflowError, OSError, RecursionError):
        raise StoryIdentityError() from None
    if owner != expected_owner or reel != expected_owner:
        raise StoryIdentityError("story_identity_mismatch")
    identity = _identity(owner, media, taken_at, now)
    _verify_url(page_url, handle, identity.media_id)
    return identity


def verify_opportunity_identity(
    opportunity: StoryOpportunity, *, now: datetime
) -> StoryIdentity:
    """Validate exact execution identity before any browser navigation."""
    if type(opportunity) is not StoryOpportunity:
        raise StoryIdentityError()
    _handle(opportunity.target_handle)
    identity = _identity(
        opportunity.owner_id, opportunity.media_id, opportunity.taken_at, now
    )
    if (
        type(opportunity.fingerprint) is not str
        or opportunity.fingerprint != identity.fingerprint
    ):
        raise StoryIdentityError("story_identity_mismatch")
    return identity
