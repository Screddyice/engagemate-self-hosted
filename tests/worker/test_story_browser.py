"""Playwright story extraction behind the StoryBrowserAdapter port."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from engagemate.worker.story_browser import (
    SELECTORS,
    SELECTORS_VERIFIED_AGAINST_INSTAGRAM,
    PlaywrightStoryBrowser,
    StoryBrowserError,
    StoryPageVerdict,
    StoryTrayAuthwallError,
    StoryTrayNavigationError,
    StoryTrayUnrecognizedError,
    classify_story_page,
)
from engagemate.worker.story_types import (
    ActiveStory,
    DiscoveryContext,
    DiscoveryTarget,
    StoryMediaType,
)

USER = UUID("00000000-0000-0000-0000-000000000001")
ACCOUNT = UUID("00000000-0000-0000-0000-000000000002")
TARGET = UUID("00000000-0000-0000-0000-000000000003")
OTHER_USER = UUID("00000000-0000-0000-0000-0000000000ff")
NOW = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
JPEG = b"\xff\xd8\xff" + b"payload"


def context() -> DiscoveryContext:
    return DiscoveryContext(USER, ACCOUNT, NOW)


def target(handle: str = "target.one") -> DiscoveryTarget:
    return DiscoveryTarget(
        id=TARGET,
        user_id=USER,
        ig_account_id=ACCOUNT,
        owner_id="owner-1",
        handle=handle,
        tier="vip",
        enabled=True,
    )


def story_node(**overrides):
    node = {
        "data-story-owner": "owner-1",
        "data-story-media-id": "media-1",
        "data-story-taken-at": (NOW - timedelta(hours=1)).isoformat(),
        "data-story-media-type": "image",
        "caption": "A caption",
        "sticker": "A sticker",
        "media": [("https://cdn.example.com/a.jpg", "image/jpeg", JPEG)],
    }
    node.update(overrides)
    return node


class FakeLocator:
    def __init__(self, page, nodes, kind="story"):
        self.page = page
        self.nodes = nodes
        self.kind = kind

    async def count(self):
        return len(self.nodes)

    async def is_visible(self):
        return len(self.nodes) >= 1

    async def all(self):
        return [FakeLocator(self.page, [node], self.kind) for node in self.nodes]

    async def get_attribute(self, name):
        return self.nodes[0].get(name)

    async def inner_text(self):
        return self.nodes[0].get("text")

    def locator(self, selector):
        node = self.nodes[0]
        if selector == '[data-engagemate-story="true"]':
            return FakeLocator(self.page, node.get("stories", []))
        if selector == '[data-testid="story-caption"]':
            value = node.get("caption")
            return FakeLocator(
                self.page, [] if value is None else [{"text": value}], "text"
            )
        if selector == '[data-testid="story-sticker"]':
            value = node.get("sticker")
            return FakeLocator(
                self.page, [] if value is None else [{"text": value}], "text"
            )
        if selector == '[data-testid="story-media"]':
            return FakeLocator(
                self.page,
                [{"data-media-url": url} for url, _, _ in node.get("media", [])],
                "media",
            )
        return FakeLocator(self.page, [], "unknown")


class FakeResponse:
    def __init__(self, status):
        self.status = status


class FakePage:
    """A page that answers the four facts the adapter classifies on.

    `tray` is a match count, so `tray=2` models the ambiguous case as well as
    the present and absent ones. `url` defaults to where a real navigation to
    `target.one`'s tray actually lands, measured 2026-08-26.
    """

    def __init__(
        self,
        stories=None,
        *,
        tray=True,
        goto_error=None,
        url="https://www.instagram.com/stories/target.one/",
        status=200,
        auth_affordances=0,
    ):
        self.tray = int(tray)
        self.stories = list(stories if stories is not None else [story_node()])
        self.goto_error = goto_error
        self.url = url
        self.status = status
        self.auth_affordances = auth_affordances
        self.visited = []
        self.shape_override = None
        self.shape_error = None

    async def goto(self, url, **kwargs):
        self.visited.append(url)
        if self.goto_error is not None:
            raise self.goto_error
        return FakeResponse(self.status)

    async def evaluate(self, script, *args):
        if self.shape_error is not None:
            raise self.shape_error
        if self.shape_override is not None:
            return self.shape_override
        # Shaped like the census the real probe returned for a logged-out page.
        return {
            "tags": {"section": 1, "div": 37, "a": 5},
            "attributes": {"role": 7, "data-visualcompletion": 1, "href": 115},
            "roles": ["button", "presentation"],
        }

    def locator(self, selector):
        if selector == SELECTORS.tray:
            nodes = [{"stories": self.stories}] * self.tray
            return FakeLocator(self, nodes, "tray")
        if selector == SELECTORS.auth_affordance:
            return FakeLocator(self, [{}] * self.auth_affordances, "auth")
        return FakeLocator(self, [], "unknown")


class FakeFetcher:
    def __init__(self, page=None, error=None, override=None):
        self.page = page
        self.error = error
        self.override = override
        self.calls = []

    async def fetch(self, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if self.override is not None:
            return self.override
        for node in self.page.stories:
            for media_url, mime, data in node.get("media", []):
                if media_url == url:
                    return mime, data
        raise AssertionError(f"unexpected media url {url}")


def build(page=None, fetcher=None, **kwargs):
    page = page if page is not None else FakePage()
    fetcher = fetcher if fetcher is not None else FakeFetcher(page)
    return PlaywrightStoryBrowser(page, fetcher, **kwargs), page, fetcher


async def test_returns_one_normalized_image_story():
    browser, page, _ = build()

    stories = await browser.active_stories(context(), target())

    assert len(stories) == 1
    story = stories[0]
    assert type(story) is ActiveStory
    assert story.target_id == TARGET
    assert story.owner_id == "owner-1"
    assert story.media_id == "media-1"
    assert story.content.media_type is StoryMediaType.IMAGE
    assert story.content.caption_text == "A caption"
    assert story.content.sticker_text == "A sticker"
    assert story.content.image.mime_type == "image/jpeg"
    assert story.content.image.data == JPEG
    assert story.content.video_frames == ()


async def test_navigates_to_the_targets_story_tray():
    browser, page, _ = build()

    await browser.active_stories(context(), target("target.one"))

    assert page.visited == ["https://www.instagram.com/stories/target.one/"]


async def test_result_is_accepted_by_discovery_normalization():
    from engagemate.worker.discovery import _normalize_story

    browser, _, _ = build()
    stories = await browser.active_stories(context(), target())

    opportunity = _normalize_story(stories[0], target(), NOW)

    assert opportunity is not False and opportunity is not None
    assert opportunity.target_id == TARGET
    assert len(opportunity.fingerprint) == 64


async def test_a_video_story_returns_bounded_frames():
    frames = [
        (f"https://cdn.example.com/f{index}.jpg", "image/jpeg", JPEG + bytes([index]))
        for index in range(6)
    ]
    page = FakePage([story_node(**{"data-story-media-type": "video", "media": frames})])
    browser, _, _ = build(page)

    stories = await browser.active_stories(context(), target())

    assert stories[0].content.media_type is StoryMediaType.VIDEO
    assert stories[0].content.image is None
    assert len(stories[0].content.video_frames) == 4


async def test_a_story_owned_by_another_account_is_skipped():
    page = FakePage([story_node(**{"data-story-owner": "someone-else"})])
    browser, _, _ = build(page)

    assert await browser.active_stories(context(), target()) == ()


async def test_an_empty_tray_means_no_active_stories():
    browser, _, _ = build(FakePage([]))

    assert await browser.active_stories(context(), target()) == ()


async def test_a_navigation_failure_is_raised_not_reported_as_empty():
    browser, _, _ = build(FakePage(goto_error=RuntimeError("timeout")))

    with pytest.raises(StoryBrowserError):
        await browser.active_stories(context(), target())


async def test_a_malformed_timestamp_skips_the_story():
    page = FakePage([story_node(**{"data-story-taken-at": "not-a-time"})])
    browser, _, _ = build(page)

    assert await browser.active_stories(context(), target()) == ()


async def test_a_naive_timestamp_skips_the_story():
    page = FakePage([story_node(**{"data-story-taken-at": "2026-07-31T07:00:00"})])
    browser, _, _ = build(page)

    assert await browser.active_stories(context(), target()) == ()


async def test_an_unsupported_media_type_skips_the_story():
    page = FakePage([story_node(**{"data-story-media-type": "carousel"})])
    browser, _, _ = build(page)

    assert await browser.active_stories(context(), target()) == ()


async def test_an_unsupported_mime_type_skips_the_story():
    page = FakePage()
    fetcher = FakeFetcher(page, override=("application/pdf", JPEG))
    browser, _, _ = build(page, fetcher)

    assert await browser.active_stories(context(), target()) == ()


async def test_oversized_media_skips_the_story():
    page = FakePage()
    fetcher = FakeFetcher(page, override=("image/jpeg", b"x" * (5 * 1024 * 1024 + 1)))
    browser, _, _ = build(page, fetcher)

    assert await browser.active_stories(context(), target()) == ()


async def test_empty_media_skips_the_story():
    page = FakePage()
    fetcher = FakeFetcher(page, override=("image/jpeg", b""))
    browser, _, _ = build(page, fetcher)

    assert await browser.active_stories(context(), target()) == ()


async def test_a_media_fetch_failure_skips_the_story():
    page = FakePage()
    fetcher = FakeFetcher(page, error=RuntimeError("connection reset"))
    browser, _, _ = build(page, fetcher)

    assert await browser.active_stories(context(), target()) == ()


async def test_a_non_https_media_url_is_never_fetched():
    page = FakePage(
        [story_node(media=[("http://cdn.example.com/a.jpg", "image/jpeg", JPEG)])]
    )
    browser, _, fetcher = build(page)

    assert await browser.active_stories(context(), target()) == ()
    assert fetcher.calls == []


async def test_missing_text_becomes_none_rather_than_empty():
    page = FakePage([story_node(caption=None, sticker=None)])
    browser, _, _ = build(page)

    stories = await browser.active_stories(context(), target())

    assert stories[0].content.caption_text is None
    assert stories[0].content.sticker_text is None


async def test_whitespace_only_text_becomes_none():
    page = FakePage([story_node(caption="   ", sticker="\n")])
    browser, _, _ = build(page)

    stories = await browser.active_stories(context(), target())

    assert stories[0].content.caption_text is None


async def test_surrounding_whitespace_is_stripped():
    page = FakePage([story_node(caption="  hello  ")])
    browser, _, _ = build(page)

    stories = await browser.active_stories(context(), target())

    assert stories[0].content.caption_text == "hello"


async def test_overlong_text_is_truncated_to_the_discovery_limit():
    page = FakePage([story_node(caption="x" * 5_000)])
    browser, _, _ = build(page)

    stories = await browser.active_stories(context(), target())

    assert len(stories[0].content.caption_text) == 4_000


async def test_control_characters_are_removed_from_text():
    page = FakePage([story_node(caption="hel\x00lo\nworld")])
    browser, _, _ = build(page)

    stories = await browser.active_stories(context(), target())

    assert stories[0].content.caption_text == "hello\nworld"


async def test_a_story_without_a_media_id_still_gets_a_media_hash():
    page = FakePage([story_node(**{"data-story-media-id": None})])
    browser, _, _ = build(page)

    stories = await browser.active_stories(context(), target())

    assert stories[0].media_id is None
    assert len(stories[0].media_hash) == 64


async def test_the_media_hash_is_stable_for_identical_media():
    first = await build()[0].active_stories(context(), target())
    second = await build()[0].active_stories(context(), target())

    assert first[0].media_hash == second[0].media_hash


async def test_different_media_produces_a_different_hash():
    page = FakePage(
        [story_node(media=[("https://cdn.example.com/b.jpg", "image/jpeg", b"other")])]
    )
    baseline = await build()[0].active_stories(context(), target())
    changed = await build(page)[0].active_stories(context(), target())

    assert baseline[0].media_hash != changed[0].media_hash


async def test_the_story_count_is_bounded():
    many = [
        story_node(
            **{
                "data-story-media-id": f"media-{index}",
                "media": [
                    (f"https://cdn.example.com/{index}.jpg", "image/jpeg", JPEG)
                ],
            }
        )
        for index in range(50)
    ]
    browser, _, _ = build(FakePage(many), max_stories=10)

    assert len(await browser.active_stories(context(), target())) == 10


async def test_a_cross_tenant_target_is_refused_before_navigation():
    browser, page, _ = build()
    foreign = DiscoveryTarget(
        id=TARGET,
        user_id=OTHER_USER,
        ig_account_id=ACCOUNT,
        owner_id="owner-1",
        handle="target.one",
        tier="vip",
        enabled=True,
    )

    with pytest.raises(StoryBrowserError):
        await browser.active_stories(context(), foreign)

    assert page.visited == []


async def test_an_unsafe_handle_is_refused_before_navigation():
    browser, page, _ = build()

    with pytest.raises(StoryBrowserError):
        await browser.active_stories(context(), target("../../evil"))

    assert page.visited == []


async def test_the_adapter_repr_exposes_no_page_state():
    browser, _, _ = build()

    assert repr(browser) == "PlaywrightStoryBrowser(page=<browser>, media=<fetcher>)"


# --- The tray seam must never report "unreadable page" as "no stories" ---------
#
# The numbers in the classification tests below are measurements, not guesses.
# `scripts/probe_story_page_shape.py` produced them on 2026-08-26 against real
# Instagram from headless Chromium with no session, and any change to the
# selectors should re-run it rather than reason about what Instagram "probably"
# does. Two of those measurements contradict the assumptions this adapter and
# its README were written on, which is exactly why they are pinned here.


async def test_a_missing_tray_is_raised_not_reported_as_no_stories():
    # Supersedes test_a_missing_tray_means_no_active_stories. The tray selectors
    # have never matched real Instagram, so "the tray is absent" is evidence the
    # selectors are wrong, not evidence the target posted nothing.
    browser, _, _ = build(FakePage(tray=False))

    with pytest.raises(StoryTrayUnrecognizedError):
        await browser.active_stories(context(), target())


async def test_an_ambiguous_tray_is_raised_not_reported_as_no_stories():
    browser, _, _ = build(FakePage(tray=2))

    with pytest.raises(StoryTrayUnrecognizedError):
        await browser.active_stories(context(), target())


async def test_a_login_wall_is_a_distinct_loud_error():
    browser, _, _ = build(FakePage(tray=False, auth_affordances=1))

    with pytest.raises(StoryTrayAuthwallError):
        await browser.active_stories(context(), target())


async def test_a_login_wall_outranks_an_unrecognized_page():
    # A logged-out page has no tray either. Reporting "we cannot parse this"
    # would send whoever reads the log hunting for selectors when the real
    # answer is that the session died.
    browser, _, _ = build(FakePage(tray=True, auth_affordances=1))

    with pytest.raises(StoryTrayAuthwallError):
        await browser.active_stories(context(), target())


async def test_an_http_error_status_is_raised():
    browser, _, _ = build(FakePage(status=500))

    with pytest.raises(StoryTrayNavigationError):
        await browser.active_stories(context(), target())


async def test_landing_on_another_page_entirely_is_raised():
    browser, _, _ = build(FakePage(url="https://www.instagram.com/accounts/login/"))

    with pytest.raises(StoryTrayAuthwallError):
        await browser.active_stories(context(), target())


async def test_a_story_id_in_the_url_is_still_the_targets_tray():
    page = FakePage(url="https://www.instagram.com/stories/target.one/123456789/")
    browser, _, _ = build(page)

    assert len(await browser.active_stories(context(), target())) == 1


def test_the_real_logged_out_instagram_page_is_an_authwall_not_an_empty_tray():
    # Measured 2026-08-26, https://www.instagram.com/stories/instagram/,
    # headless Chromium, no cookies: HTTP 200, no redirect, one
    # /accounts/emailsignup/ affordance, zero matches for the tray selector.
    verdict = classify_story_page(
        handle="instagram",
        final_url="https://www.instagram.com/stories/instagram/",
        status=200,
        auth_affordances=1,
        tray_matches=0,
    )

    assert verdict is StoryPageVerdict.AUTHWALL


def test_the_logged_out_page_does_not_redirect():
    # The adapter's original contract assumed a logged-out request bounces to a
    # login URL. It does not: the measurement above returned 200 at the
    # requested URL, so a redirect check alone would classify a login wall as a
    # readable page with no stories.
    verdict = classify_story_page(
        handle="instagram",
        final_url="https://www.instagram.com/stories/instagram/",
        status=200,
        auth_affordances=0,
        tray_matches=0,
    )

    assert verdict is StoryPageVerdict.UNRECOGNIZED


def test_a_recognized_tray_is_the_only_readable_verdict():
    assert (
        classify_story_page(
            handle="target.one",
            final_url="https://www.instagram.com/stories/target.one/",
            status=200,
            auth_affordances=0,
            tray_matches=1,
        )
        is StoryPageVerdict.READABLE_TRAY
    )


def test_an_offsite_landing_is_a_navigation_failure():
    assert (
        classify_story_page(
            handle="target.one",
            final_url="https://example.com/",
            status=200,
            auth_affordances=0,
            tray_matches=1,
        )
        is StoryPageVerdict.NAVIGATION_FAILED
    )


async def test_an_unrecognized_page_captures_its_shape_for_the_next_engineer():
    page = FakePage(tray=False)
    browser, _, _ = build(page)

    with pytest.raises(StoryTrayUnrecognizedError) as caught:
        await browser.active_stories(context(), target())

    shape = caught.value.shape
    assert shape is not None
    assert shape.url == "https://www.instagram.com/stories/target.one/"
    assert ("section", 1) in shape.tag_counts
    assert "data-visualcompletion" in shape.attribute_names


async def test_the_captured_shape_carries_no_page_text_or_attribute_values():
    page = FakePage(tray=False)
    page.shape_override = {
        "tags": {"div": 4},
        "attributes": {"data-testid": 2, "aria-label": 9},
        "roles": ["button"],
        "text": "a private DM from someone",
        "aria-label": ["Message from alice"],
    }
    browser, _, _ = build(page)

    with pytest.raises(StoryTrayUnrecognizedError) as caught:
        await browser.active_stories(context(), target())

    rendered = repr(caught.value.shape)
    assert "private DM" not in rendered
    assert "alice" not in rendered
    assert caught.value.shape.attribute_names == ("aria-label", "data-testid")


async def test_a_failed_capture_still_raises_the_real_error():
    page = FakePage(tray=False)
    page.shape_error = RuntimeError("evaluate is unavailable")
    browser, _, _ = build(page)

    with pytest.raises(StoryTrayUnrecognizedError) as caught:
        await browser.active_stories(context(), target())

    assert caught.value.shape is None


async def test_every_tray_failure_is_still_a_story_browser_error():
    # DiscoveryService only distinguishes "raised" from "returned", so the new
    # subclasses must not escape the type it already fails the run on.
    for page in (
        FakePage(tray=False),
        FakePage(tray=False, auth_affordances=1),
        FakePage(status=500),
        FakePage(goto_error=RuntimeError("timeout")),
    ):
        browser, _, _ = build(page)
        with pytest.raises(StoryBrowserError):
            await browser.active_stories(context(), target())


def test_the_tray_selectors_are_declared_unverified():
    # This pin is the point of the flag: it fails the moment someone flips it,
    # forcing them to have actually watched these selectors match a live
    # authenticated story tray first.
    assert SELECTORS_VERIFIED_AGAINST_INSTAGRAM is False


def test_counts_the_page_did_not_supply_as_integers_are_unrecognized():
    # `.count()` returns an int, but everything here crosses the page boundary,
    # and comparing a surprise against 0 would escape as a TypeError rather
    # than the StoryBrowserError this module promises.
    assert (
        classify_story_page(
            handle="target.one",
            final_url="https://www.instagram.com/stories/target.one/",
            status=200,
            auth_affordances=None,
            tray_matches=1,
        )
        is StoryPageVerdict.UNRECOGNIZED
    )
