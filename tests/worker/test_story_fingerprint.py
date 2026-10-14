from __future__ import annotations

import traceback
from datetime import UTC, datetime, timedelta, timezone, tzinfo

import pytest
from engagemate.worker.story_fingerprint import (
    StoryFingerprintError,
    fingerprint_story,
)

OWNER = "17841400000000001"
MEDIA = "3412345678901234567"
MEDIA_HASH = "ab" * 32
TAKEN_AT = datetime(2026, 7, 31, 1, 2, 3, tzinfo=UTC)
SECRET = "raw-object-secret"


class BrokenTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise ValueError(SECRET)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def __repr__(self) -> str:
        return SECRET


class InterruptingTimezone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        raise KeyboardInterrupt(SECRET)

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None


class EvilDate(datetime):
    def astimezone(self, tz: tzinfo | None = None) -> EvilDate:
        return self

    def __repr__(self) -> str:
        return SECRET


def test_media_id_fingerprint_is_stable_across_incidental_story_metadata() -> None:
    first = fingerprint_story(OWNER, MEDIA, TAKEN_AT, MEDIA_HASH)
    second = fingerprint_story(
        OWNER,
        MEDIA,
        TAKEN_AT + timedelta(hours=10),
        "cd" * 32,
    )

    assert first == second
    assert len(first) == 64
    assert first == first.lower()
    assert set(first) <= set("0123456789abcdef")


def test_media_id_fingerprint_changes_with_owner_or_media_identity() -> None:
    baseline = fingerprint_story(OWNER, MEDIA, TAKEN_AT, None)

    assert fingerprint_story(OWNER + "2", MEDIA, TAKEN_AT, None) != baseline
    assert fingerprint_story(OWNER, MEDIA + "2", TAKEN_AT, None) != baseline


@pytest.mark.parametrize(
    "media_hash",
    [MEDIA_HASH.upper(), b"raw-media", True, object()],
)
def test_media_id_rejects_any_provided_malformed_sample_hash(
    media_hash: object,
) -> None:
    with pytest.raises(
        StoryFingerprintError, match="^invalid story fingerprint input$"
    ):
        fingerprint_story(OWNER, MEDIA, TAKEN_AT, media_hash)  # type: ignore[arg-type]


def test_canonical_encoding_cannot_confuse_field_boundaries() -> None:
    assert fingerprint_story("ab", "c", TAKEN_AT, None) != fingerprint_story(
        "a", "bc", TAKEN_AT, None
    )


def test_fallback_uses_utc_five_minute_bucket_and_sampled_media_hash() -> None:
    first = fingerprint_story(OWNER, None, TAKEN_AT, MEDIA_HASH)
    same_bucket = fingerprint_story(
        OWNER,
        None,
        TAKEN_AT + timedelta(minutes=2, seconds=56),
        MEDIA_HASH,
    )
    offset_equivalent = fingerprint_story(
        OWNER,
        None,
        TAKEN_AT.astimezone(timezone(timedelta(hours=5, minutes=30))),
        MEDIA_HASH,
    )

    assert same_bucket == first
    assert offset_equivalent == first
    assert (
        fingerprint_story(OWNER, None, TAKEN_AT + timedelta(minutes=3), MEDIA_HASH)
        != first
    )
    assert fingerprint_story(OWNER, None, TAKEN_AT, "cd" * 32) != first


def test_fallback_uses_floor_safe_buckets_before_the_unix_epoch() -> None:
    just_before_epoch = datetime(
        1969,
        12,
        31,
        23,
        59,
        59,
        500000,
        tzinfo=UTC,
    )
    bucket_start = datetime(1969, 12, 31, 23, 55, tzinfo=UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    assert fingerprint_story(
        OWNER, None, just_before_epoch, MEDIA_HASH
    ) == fingerprint_story(OWNER, None, bucket_start, MEDIA_HASH)
    assert fingerprint_story(
        OWNER, None, just_before_epoch, MEDIA_HASH
    ) != fingerprint_story(OWNER, None, epoch, MEDIA_HASH)


def test_fallback_changes_at_exact_normal_bucket_edge() -> None:
    bucket_start = datetime(2026, 7, 31, 1, 5, tzinfo=UTC)

    assert fingerprint_story(
        OWNER,
        None,
        bucket_start - timedelta(microseconds=1),
        MEDIA_HASH,
    ) != fingerprint_story(OWNER, None, bucket_start, MEDIA_HASH)


@pytest.mark.parametrize(
    ("owner_id", "media_id", "taken_at", "media_hash"),
    [
        ("", MEDIA, TAKEN_AT, None),
        (" ", MEDIA, TAKEN_AT, None),
        ("a\nb", MEDIA, TAKEN_AT, None),
        ("a" * 257, MEDIA, TAKEN_AT, None),
        (OWNER, "", TAKEN_AT, None),
        (OWNER, "a\x00b", TAKEN_AT, None),
        (OWNER, "a" * 257, TAKEN_AT, None),
        (True, MEDIA, TAKEN_AT, None),
        (OWNER, True, TAKEN_AT, None),
        (OWNER, None, TAKEN_AT.replace(tzinfo=None), MEDIA_HASH),
        (OWNER, None, "2026-07-31", MEDIA_HASH),
        (OWNER, None, TAKEN_AT, None),
        (OWNER, None, TAKEN_AT, MEDIA_HASH.upper()),
        (OWNER, None, TAKEN_AT, "ab"),
        (OWNER, None, TAKEN_AT, b"raw-media"),
        (OWNER, None, datetime(2026, 7, 31, tzinfo=BrokenTimezone()), MEDIA_HASH),
    ],
)
def test_fingerprint_rejects_malformed_inputs_with_one_bounded_error(
    owner_id: object,
    media_id: object,
    taken_at: object,
    media_hash: object,
) -> None:
    with pytest.raises(
        StoryFingerprintError, match="^invalid story fingerprint input$"
    ) as raised:
        fingerprint_story(owner_id, media_id, taken_at, media_hash)  # type: ignore[arg-type]

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert SECRET not in "".join(
        traceback.format_exception(
            type(raised.value), raised.value, raised.value.__traceback__
        )
    )
    captured = traceback.TracebackException(
        type(raised.value),
        raised.value,
        raised.value.__traceback__,
        capture_locals=True,
    )
    production_frame = next(
        frame for frame in captured.stack if frame.name == "fingerprint_story"
    )
    assert SECRET not in repr(production_frame.locals)


def test_fingerprint_does_not_swallow_base_exceptions() -> None:
    with pytest.raises(KeyboardInterrupt, match=SECRET):
        fingerprint_story(
            OWNER,
            None,
            datetime(2026, 7, 31, tzinfo=InterruptingTimezone()),
            MEDIA_HASH,
        )


def test_fingerprint_rejects_datetime_subclass_without_retaining_its_repr() -> None:
    evil = EvilDate(2026, 7, 31, tzinfo=UTC)

    with pytest.raises(StoryFingerprintError) as raised:
        fingerprint_story(OWNER, MEDIA, evil, MEDIA_HASH.upper())

    captured = traceback.TracebackException(
        type(raised.value),
        raised.value,
        raised.value.__traceback__,
        capture_locals=True,
    )
    production_frames = [
        frame for frame in captured.stack if "/engagemate/worker/" in frame.filename
    ]
    assert SECRET not in repr(
        [(frame.name, frame.locals) for frame in production_frames]
    )
