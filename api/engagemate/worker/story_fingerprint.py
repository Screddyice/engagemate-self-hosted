"""Stable story identities for history deduplication."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from typing import Final

_MAX_IDENTIFIER_LENGTH: Final = 256
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_BUCKET_MICROSECONDS: Final = 5 * 60 * 1_000_000
_UNIX_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


class StoryFingerprintError(Exception):
    """Raised when story identity material cannot be trusted."""


def fingerprint_story(
    owner_id: str,
    media_id: str | None,
    taken_at: datetime,
    media_hash: str | None,
) -> str:
    """Return a versioned SHA-256 identity for one logical story.

    Instagram media IDs take precedence. When Instagram does not expose one,
    the fallback uses the UTC five-minute timestamp bucket and a precomputed
    SHA-256 digest of sampled media.
    """

    owner = _validated_identifier(owner_id)
    media = None if media_id is None else _validated_identifier(media_id)
    timestamp = _as_utc(taken_at)
    hash_value = _validated_hash(media_hash)

    invalid = owner is None or timestamp is None
    invalid = invalid or (media_hash is not None and hash_value is None)
    if media_id is not None:
        invalid = invalid or media is None
    else:
        invalid = invalid or hash_value is None
    if invalid:
        del owner_id, media_id, taken_at, media_hash
        raise StoryFingerprintError("invalid story fingerprint input")

    if media is not None:
        canonical = ["engagemate-story-v1", "media-id", owner, media]
    else:
        since_epoch = timestamp - _UNIX_EPOCH
        timestamp_microseconds = (
            since_epoch.days * 86400 + since_epoch.seconds
        ) * 1_000_000 + since_epoch.microseconds
        bucket = timestamp_microseconds // _BUCKET_MICROSECONDS
        canonical = [
            "engagemate-story-v1",
            "sampled-media",
            owner,
            bucket,
            hash_value,
        ]
    encoded = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validated_identifier(value: object) -> str | None:
    if type(value) is not str:
        return None
    if not value or value != value.strip() or len(value) > _MAX_IDENTIFIER_LENGTH:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    return value


def _validated_hash(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        return None
    return value


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
