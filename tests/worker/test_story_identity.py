from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from engagemate.worker.story_fingerprint import fingerprint_story
from engagemate.worker.story_identity import (
    StoryIdentityError,
    parse_seen_mutation,
    verify_opportunity_identity,
)
from engagemate.worker.story_types import StoryContent, StoryMediaType, StoryOpportunity

OWNER = "12345678901"
MEDIA = "3000000000000000870"
HANDLE = "sample.viewer25"
TAKEN = datetime.fromtimestamp(1789246777, UTC)
NOW = TAKEN + timedelta(minutes=2)
URL = f"https://www.instagram.com/stories/{HANDLE}/"
VARIABLES = {
    "reelId": OWNER,
    "reelMediaId": MEDIA,
    "reelMediaOwnerId": OWNER,
    "reelMediaTakenAt": 1789246777,
    "viewSeenAt": int(NOW.timestamp()),
}


def form(variables=None, **changes):
    values = {
        "fb_api_req_friendly_name": "PolarisStoriesV3SeenMutation",
        "variables": json.dumps(VARIABLES if variables is None else variables),
        "csrf_placeholder": "synthetic-private-form-field",
    }
    values.update(changes)
    return urlencode(values)


def parse(post_data=None, **changes):
    arguments = {
        "expected_owner_id": OWNER,
        "expected_handle": HANDLE,
        "page_url": URL,
        "now": NOW,
    }
    arguments.update(changes)
    return parse_seen_mutation(form() if post_data is None else post_data, **arguments)


def opportunity(**changes):
    result = StoryOpportunity(
        uuid4(),
        OWNER,
        HANDLE,
        "discovery",
        MEDIA,
        TAKEN,
        fingerprint_story(OWNER, MEDIA, TAKEN, None),
        StoryContent(StoryMediaType.IMAGE, None, None, None, ()),
    )
    return replace(result, **changes)


@pytest.mark.parametrize(
    "page_url", [URL, f"{URL}{MEDIA}/", f"{URL}{MEDIA}", URL + "?r=1"]
)
def test_measured_seen_mutation_binds_regular_viewer_exact_owner_and_media(page_url):
    identity = parse(page_url=page_url)
    assert identity.media_id == MEDIA
    assert identity.owner_id == OWNER
    assert identity.taken_at == TAKEN
    assert identity.fingerprint == fingerprint_story(OWNER, MEDIA, TAKEN, None)
    with pytest.raises(FrozenInstanceError):
        identity.media_id = "123"


@pytest.mark.parametrize("field", ["reelId", "reelMediaOwnerId"])
def test_owner_and_reel_must_both_match_expected_owner(field):
    with pytest.raises(StoryIdentityError, match="story_identity_mismatch"):
        parse(form({**VARIABLES, field: "999"}))


@pytest.mark.parametrize("field", ["reelId", "reelMediaOwnerId", "reelMediaId"])
@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        {},
        [],
        "",
        "highlight:123",
        "highlights",
        "-1",
        "0",
        "01",
        "1.0",
        "１２３",
        "123\n",
        "9" * 33,
        -1,
        0,
        1.5,
    ],
)
def test_identifiers_reject_highlights_nonnumeric_and_malformed_values(field, value):
    with pytest.raises(StoryIdentityError):
        parse(form({**VARIABLES, field: value}))


@pytest.mark.parametrize("field", ["reelId", "reelMediaOwnerId", "reelMediaId"])
def test_exact_numeric_integer_identifiers_are_normalized(field):
    identity = parse(form({**VARIABLES, field: int(VARIABLES[field])}))
    assert identity.owner_id == OWNER and identity.media_id == MEDIA


@pytest.mark.parametrize(
    "value", [True, False, None, "1789246777", 1789246777.0, -1, 0, 10**40, {}, []]
)
def test_timestamp_requires_bounded_positive_integer_epoch_seconds(value):
    with pytest.raises(StoryIdentityError):
        parse(form({**VARIABLES, "reelMediaTakenAt": value}))


@pytest.mark.parametrize("age", [-1, 86400, 86401])
def test_future_or_expired_story_is_rejected(age):
    with pytest.raises(StoryIdentityError, match="story_expired_or_future"):
        parse(now=TAKEN + timedelta(seconds=age))


@pytest.mark.parametrize("age", [0, 86399])
def test_current_story_age_boundaries(age):
    assert parse(now=TAKEN + timedelta(seconds=age)).media_id == MEDIA


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.instagram.com/stories/highlights/{MEDIA}/",
        f"https://www.instagram.com/stories/other/{MEDIA}/",
        URL + "999/",
        URL + MEDIA + "/extra/",
        "https://evil.example/stories/sample.viewer25/",
        "https://www.instagram.com.evil.example/stories/sample.viewer25/",
        "https://secret@www.instagram.com/stories/sample.viewer25/",
        "http://www.instagram.com/stories/sample.viewer25/",
        URL + "#other",
        URL + "?r=2",
        URL + "?r=1&r=1",
        URL + "?next=highlights",
        "https://www.instagram.com/stories/%61lvarow25/",
        URL + "\n",
        URL + "/",
        URL + "x" * 2048,
    ],
)
def test_viewer_url_cannot_substitute_owner_media_host_or_highlight(url):
    with pytest.raises(StoryIdentityError):
        parse(page_url=url)


@pytest.mark.parametrize(
    "post_data",
    [
        "",
        "no-equals",
        "variables=%7B",
        "x=" + "a" * 131072,
        form(fb_api_req_friendly_name="OtherMutation"),
        form() + "&variables=%7B%7D",
        form() + "&fb_api_req_friendly_name=PolarisStoriesV3SeenMutation",
        form(variables="not object"),
        form(variables=[]),
        form(variables={}),
        form(variables={"reelId": OWNER}),
        form(variables="x" * 16384),
        urlencode(
            {
                "fb_api_req_friendly_name": "PolarisStoriesV3SeenMutation",
                "variables": '{"reelId":"1","reelId":"2"}',
            }
        ),
    ],
)
def test_malformed_forms_and_ambiguous_json_are_rejected_without_raw_body_errors(
    post_data,
):
    with pytest.raises(StoryIdentityError) as failure:
        parse(post_data)
    assert "synthetic-private-form-field" not in str(failure.value)
    assert len(str(failure.value)) < 64


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_owner_id": True},
        {"expected_owner_id": "highlight:123"},
        {"expected_handle": "../stories"},
        {"expected_handle": "highlights"},
        {"expected_handle": "other"},
        {"now": TAKEN.replace(tzinfo=None)},
        {"now": True},
    ],
)
def test_expected_identity_and_clock_are_validated(changes):
    with pytest.raises(StoryIdentityError):
        parse(**changes)


def test_opportunity_verification_recomputes_immutable_exact_identity():
    result = verify_opportunity_identity(opportunity(), now=NOW)
    assert result == parse()


@pytest.mark.parametrize(
    "changes",
    [
        {"owner_id": "999"},
        {"media_id": "999"},
        {"media_id": None},
        {"media_id": "highlight:123"},
        {"media_id": True},
        {"fingerprint": "0" * 64},
        {"fingerprint": ""},
        {"target_handle": "../stories"},
        {"taken_at": TAKEN + timedelta(days=1)},
        {"taken_at": TAKEN - timedelta(days=1)},
        {"taken_at": TAKEN.replace(tzinfo=None)},
    ],
)
def test_opportunity_malformed_or_changed_identity_fails_before_browser_work(changes):
    with pytest.raises(StoryIdentityError):
        verify_opportunity_identity(opportunity(**changes), now=NOW)


def test_opportunity_requires_concrete_contract_type():
    with pytest.raises(StoryIdentityError):
        verify_opportunity_identity({"owner_id": OWNER}, now=NOW)
