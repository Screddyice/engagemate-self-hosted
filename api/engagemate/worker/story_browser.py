"""Extract one target's active stories from a live Chrome context.

THE TRAY SELECTORS BELOW HAVE NEVER MATCHED INSTAGRAM. `StoryTraySelectors`
describes a DOM that nothing produces: no content script injects those
attributes, the Chrome extension ships only a cookie bridge, and a headless
visit to a real story URL on 2026-08-26 matched the tray selector zero times.
An authenticated session measured the regular Story viewer controls on
2026-09-13, but discovery metadata and action confirmation still need live
verification. These fixture selectors remain unverified,
`SELECTORS_VERIFIED_AGAINST_INSTAGRAM` stays False, and paths that depend on
them fail loudly.

This is the only place that reads Instagram's DOM for discovery. It produces
normalized `ActiveStory` values and nothing else — no ranking, no dedupe, no
decisions. Every field is bounded here so `DiscoveryService` never has to
trust the page.

A story that cannot be normalized safely is dropped, not guessed at: a bad
timestamp, an unreadable image, or an owner that does not match the target
means one fewer opportunity, never a wrong one. A failure to reach the tray at
all is raised, because "the page did not load" must not look like "this
account posted nothing today".

That last sentence used to be a comment rather than the behavior. `_story_nodes`
returned an empty list whenever the tray selector missed, and `DiscoveryService`
cannot tell an empty success from a real one, so every unreadable page (a login
wall, a checkpoint, or simply the selectors being wrong) was reported to the run
as "this target posted nothing today". `classify_story_page` now separates the
one empty result this adapter is entitled to return, a tray that was found and
holds nothing, from every page it could not read.

Only one signal here is measured rather than assumed. Instagram serves a
logged-out story URL as HTTP 200 at the requested address, with no redirect,
and renders the login wall in place; the affordance it renders links to
`/accounts/login/` or `/accounts/emailsignup/`. Detecting the authwall by
redirect, as this module's contract originally implied, would classify a dead
session as a readable page with no stories.

`scripts/probe_story_page_shape.py` produced those measurements and prints the
same census `StoryPageShape` captures here. Point it at a live authenticated
tray and its output is the evidence needed to write selectors that are real.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from engagemate.worker.story_types import (
    ActiveStory,
    DiscoveryContext,
    DiscoveryTarget,
    StoryContent,
    StoryMedia,
    StoryMediaType,
)

_HANDLE = re.compile(r"^[a-z0-9_](?:[a-z0-9._]{0,28}[a-z0-9_])?$")
_IMAGE_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
_MAX_STORY_TEXT = 4_000
_MAX_MEDIA_BYTES = 5 * 1024 * 1024
_MAX_VIDEO_FRAMES = 4
_MAX_STORIES = 20
_MAX_IDENTIFIER = 256
_MAX_URL = 2_048
_STORY_LIFETIME = timedelta(hours=24)
_NAVIGATION_TIMEOUT_MS = 20_000
_INSTAGRAM_HOSTS = frozenset({"www.instagram.com", "instagram.com"})
_AUTH_PATH_PREFIXES = ("/accounts/", "/challenge/")
_MAX_SHAPE_ENTRIES = 120
_MAX_SHAPE_NAME = 64
_ROLE_VALUE = re.compile(r"^[a-z][a-z-]{0,31}$")

logger = logging.getLogger(__name__)

# False until someone has watched these selectors match a live authenticated
# story tray. A test pins it, so flipping it is a deliberate act.
SELECTORS_VERIFIED_AGAINST_INSTAGRAM = False


@dataclass(frozen=True, slots=True)
class StoryTraySelectors:
    """Semantic hooks for the story tray. Keep every selector in one place.

    Everything from `tray` to `media` is UNVERIFIED and known not to match
    Instagram; see the module docstring. They are kept so the parser they feed
    stays reviewable, not because they work.

    `auth_affordance` is the exception, and the only selector here measured
    against the live site: a logged-out story URL renders exactly one link to
    `/accounts/emailsignup/`, and an unavailable target renders two auth links.
    An authenticated page has no reason to offer either.
    """

    tray: str = '[data-engagemate-story-tray="true"]'
    story: str = '[data-engagemate-story="true"]'
    caption: str = '[data-testid="story-caption"]'
    sticker: str = '[data-testid="story-sticker"]'
    media: str = '[data-testid="story-media"]'
    auth_affordance: str = (
        'a[href^="/accounts/login/"], '
        'a[href^="/accounts/emailsignup/"], '
        'a[href^="https://www.instagram.com/accounts/login/"], '
        'a[href^="https://www.instagram.com/accounts/emailsignup/"]'
    )


SELECTORS = StoryTraySelectors()


class StoryBrowserError(RuntimeError):
    """Raised when the tray cannot be reached or the request is unsafe.

    Kept as the base of every tray failure because `DiscoveryService` only
    distinguishes "raised" from "returned"; the subclasses exist to tell
    whoever reads the log which of three very different things went wrong.
    """


class StoryTrayNavigationError(StoryBrowserError):
    """The tray URL could not be reached: the navigation or the status failed."""


class StoryTrayAuthwallError(StoryBrowserError):
    """Instagram answered with a login wall or a checkpoint, not a story tray.

    Distinct from the others because it is the one that means the account's
    session is dead. It is not a selector problem and no amount of retrying
    the page will fix it.
    """


class StoryTrayUnrecognizedError(StoryBrowserError):
    """The page loaded and is not a login wall, and we still cannot read it.

    Today this is the expected outcome of any real run, because the tray
    selectors do not match Instagram. `shape` carries a bounded, text-free
    census of what was actually on the page so the next engineer can write
    selectors from evidence.
    """

    def __init__(self, message: str, shape: "StoryPageShape | None" = None) -> None:
        super().__init__(message)
        self.shape = shape


class StoryPageVerdict(StrEnum):
    """What the page we landed on actually is."""

    READABLE_TRAY = "readable_tray"
    AUTHWALL = "authwall"
    NAVIGATION_FAILED = "navigation_failed"
    UNRECOGNIZED = "unrecognized"


@dataclass(frozen=True, slots=True)
class StoryPageShape:
    """A bounded, text-free description of a page the tray selectors missed.

    Structure only: element counts, attribute *names*, and ARIA role values.
    No attribute values beyond roles, no text, no cookies. Captions, display
    names, and DM contents all live in text and attribute values, so leaving
    both behind is what makes this safe to log from a logged-in session.
    """

    url: str
    status: int | None
    tag_counts: tuple[tuple[str, int], ...]
    attribute_names: tuple[str, ...]
    role_values: tuple[str, ...]


def classify_story_page(
    *,
    handle: str,
    final_url: object,
    status: object,
    auth_affordances: object,
    tray_matches: object,
) -> StoryPageVerdict:
    """Decide what we are looking at from four facts the page cannot fake.

    Pure so it can be tested against numbers measured from the live site
    instead of numbers invented by a fake page. Every argument crosses the
    page boundary, so none of them is trusted to be the type it should be.
    """

    if type(auth_affordances) is not int or type(tray_matches) is not int:
        return StoryPageVerdict.UNRECOGNIZED
    if status is not None and (type(status) is not int or status >= 400):
        return StoryPageVerdict.NAVIGATION_FAILED

    parsed = _split(final_url)
    if parsed is None:
        return StoryPageVerdict.NAVIGATION_FAILED
    host, path = parsed
    if host not in _INSTAGRAM_HOSTS:
        return StoryPageVerdict.NAVIGATION_FAILED

    # A story id may be appended to the tray path, so match the prefix.
    if not path.startswith(f"/stories/{handle}/"):
        if path.startswith(_AUTH_PATH_PREFIXES):
            return StoryPageVerdict.AUTHWALL
        return StoryPageVerdict.NAVIGATION_FAILED

    # Checked before the tray: a logged-out page has no tray either, and
    # "we cannot parse this" would send the reader hunting for selectors when
    # the real answer is that the session died.
    if auth_affordances > 0:
        return StoryPageVerdict.AUTHWALL

    if tray_matches == 1:
        return StoryPageVerdict.READABLE_TRAY
    return StoryPageVerdict.UNRECOGNIZED


def _split(value: object) -> tuple[str, str] | None:
    if type(value) is not str or not 1 <= len(value) <= _MAX_URL:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return parsed.hostname.lower(), parsed.path


class MediaFetcher(Protocol):
    """Fetch one story image through the account's own browser context."""

    async def fetch(self, url: str) -> tuple[str, bytes]: ...


class PlaywrightStoryBrowser:
    """Read active stories for one target from an already-authenticated page."""

    def __init__(
        self,
        page: Any,
        media: MediaFetcher,
        *,
        max_stories: int = _MAX_STORIES,
    ) -> None:
        if type(max_stories) is not int or not 1 <= max_stories <= _MAX_STORIES:
            raise ValueError("max_stories must be between 1 and 20")
        self._page = page
        self._media = media
        self._max_stories = max_stories

    def __repr__(self) -> str:
        return "PlaywrightStoryBrowser(page=<browser>, media=<fetcher>)"

    async def active_stories(
        self, context: DiscoveryContext, target: DiscoveryTarget
    ) -> tuple[ActiveStory, ...]:
        """Return this target's normalized active stories, newest tray order."""

        self._require_same_tenant(context, target)
        handle = self._require_safe_handle(target.handle)

        status = await self._open_tray(handle)
        await self._require_readable_tray(handle, status)
        nodes = await self._story_nodes()

        stories: list[ActiveStory] = []
        for node in nodes[: self._max_stories]:
            story = await self._normalize(node, context, target)
            if story is not None:
                stories.append(story)
        return tuple(stories)

    def _require_same_tenant(
        self, context: DiscoveryContext, target: DiscoveryTarget
    ) -> None:
        if (
            type(context) is not DiscoveryContext
            or type(target) is not DiscoveryTarget
            or target.user_id != context.user_id
            or target.ig_account_id != context.ig_account_id
        ):
            raise StoryBrowserError("target does not belong to this run")

    def _require_safe_handle(self, handle: object) -> str:
        # The handle is interpolated into a URL, so validate it rather than
        # escaping it: anything but a real Instagram handle is a bug or an attack.
        if type(handle) is not str or _HANDLE.fullmatch(handle) is None:
            raise StoryBrowserError("unsafe target handle")
        return handle

    async def _open_tray(self, handle: str) -> int | None:
        try:
            response = await self._page.goto(
                f"https://www.instagram.com/stories/{handle}/",
                wait_until="domcontentloaded",
                timeout=_NAVIGATION_TIMEOUT_MS,
            )
        # Navigation failures must not be reported as "no stories".
        except Exception:  # noqa: BLE001
            raise StoryTrayNavigationError("story tray navigation failed") from None
        # A same-document navigation legitimately has no response; that is an
        # unknown status, not a failed one.
        status = getattr(response, "status", None)
        return status if type(status) is int else None

    async def _require_readable_tray(self, handle: str, status: int | None) -> None:
        """Refuse to parse anything we have not first recognized as the tray."""

        try:
            final_url = self._page.url
            auth_affordances = await self._page.locator(
                SELECTORS.auth_affordance
            ).count()
            tray_matches = await self._page.locator(SELECTORS.tray).count()
        # A locator call that blows up tells us nothing about the page, and
        # guessing "no stories" from it is the failure this module now refuses.
        except Exception:  # noqa: BLE001
            raise StoryTrayUnrecognizedError("story tray unreadable") from None

        verdict = classify_story_page(
            handle=handle,
            final_url=final_url,
            status=status,
            auth_affordances=auth_affordances,
            tray_matches=tray_matches,
        )
        if verdict is StoryPageVerdict.READABLE_TRAY:
            return
        if verdict is StoryPageVerdict.NAVIGATION_FAILED:
            raise StoryTrayNavigationError("story tray navigation failed")
        if verdict is StoryPageVerdict.AUTHWALL:
            raise StoryTrayAuthwallError("story tray answered with a login wall")

        shape = await self._page_shape(final_url, status)
        # The only channel out: DiscoveryService redacts every exception it
        # catches, so an unlogged shape is a shape nobody will ever read.
        logger.warning("story tray unrecognized: %r", shape)
        raise StoryTrayUnrecognizedError("story tray unrecognized", shape)

    async def _page_shape(
        self, final_url: object, status: int | None
    ) -> StoryPageShape | None:
        """Census what was actually on the page, structure only.

        Best effort by design: a diagnostic that can fail the run it is
        diagnosing is worse than no diagnostic.
        """

        try:
            census = await self._page.evaluate(_PAGE_SHAPE_SCRIPT)
        except Exception:  # noqa: BLE001
            return None
        if type(census) is not dict:
            return None
        return StoryPageShape(
            url=_shape_url(final_url),
            status=status,
            tag_counts=_shape_counts(census.get("tags")),
            attribute_names=_shape_names(census.get("attributes")),
            role_values=_shape_roles(census.get("roles")),
        )

    async def _story_nodes(self) -> list[Any]:
        try:
            tray = self._page.locator(SELECTORS.tray)
            return list(await tray.locator(SELECTORS.story).all())
        # The tray was recognized a moment ago, so a failure here is the page
        # changing under us, never "this account posted nothing today".
        except Exception:  # noqa: BLE001
            raise StoryTrayUnrecognizedError("story tray unreadable") from None

    async def _normalize(
        self,
        node: Any,
        context: DiscoveryContext,
        target: DiscoveryTarget,
    ) -> ActiveStory | None:
        try:
            owner = await node.get_attribute("data-story-owner")
            if owner != target.owner_id:
                return None

            media_id = _identifier(await node.get_attribute("data-story-media-id"))
            taken_at = _timestamp(await node.get_attribute("data-story-taken-at"))
            if taken_at is None:
                return None
            if taken_at > context.now or context.now - taken_at >= _STORY_LIFETIME:
                return None

            media_type = _media_type(
                await node.get_attribute("data-story-media-type")
            )
            if media_type is None:
                return None

            content = await self._content(node, media_type)
            if content is None:
                return None
        # One unreadable story must not lose the rest of the tray.
        except StoryBrowserError:
            raise
        except Exception:  # noqa: BLE001
            return None

        return ActiveStory(
            target_id=target.id,
            owner_id=target.owner_id,
            media_id=media_id,
            taken_at=taken_at,
            media_hash=_media_hash(content),
            content=content,
        )

    async def _content(
        self, node: Any, media_type: StoryMediaType
    ) -> StoryContent | None:
        caption = await self._text(node, SELECTORS.caption)
        sticker = await self._text(node, SELECTORS.sticker)
        images = await self._images(node, media_type)
        if images is None:
            return None

        if media_type is StoryMediaType.IMAGE:
            return StoryContent(media_type, caption, sticker, images[0], ())
        return StoryContent(media_type, caption, sticker, None, tuple(images))

    async def _images(
        self, node: Any, media_type: StoryMediaType
    ) -> list[StoryMedia] | None:
        locator = node.locator(SELECTORS.media)
        elements = list(await locator.all())
        wanted = 1 if media_type is StoryMediaType.IMAGE else _MAX_VIDEO_FRAMES
        if not elements:
            return None

        images: list[StoryMedia] = []
        for element in elements[:wanted]:
            url = await element.get_attribute("data-media-url")
            image = await self._image(url)
            if image is None:
                return None
            images.append(image)
        return images if images else None

    async def _image(self, url: object) -> StoryMedia | None:
        if (
            type(url) is not str
            or not 1 <= len(url) <= _MAX_URL
            or not url.startswith("https://")
        ):
            return None
        try:
            mime, data = await self._media.fetch(url)
        # A single unreachable image is an ordinary outcome, not a run failure.
        except Exception:  # noqa: BLE001
            return None
        if (
            type(mime) is not str
            or mime not in _IMAGE_MIME_TYPES
            or type(data) is not bytes
            or not 0 < len(data) <= _MAX_MEDIA_BYTES
        ):
            return None
        return StoryMedia(mime, data)

    async def _text(self, node: Any, selector: str) -> str | None:
        try:
            locator = node.locator(selector)
            if await locator.count() != 1:
                return None
            raw = await locator.inner_text()
        # Story text is optional; an unreadable node simply has none.
        except Exception:  # noqa: BLE001
            return None
        return _story_text(raw)


# Names and counts leave the page; text and attribute values do not. The
# element cap keeps one enormous page from producing an enormous log line.
_PAGE_SHAPE_SCRIPT = """() => {
  const tags = {}, attributes = {}, roles = {};
  let scanned = 0;
  for (const element of document.querySelectorAll('*')) {
    if (++scanned > 4000) break;
    const tag = element.tagName.toLowerCase();
    tags[tag] = (tags[tag] || 0) + 1;
    for (const attribute of element.attributes) {
      attributes[attribute.name] = (attributes[attribute.name] || 0) + 1;
      if (attribute.name === 'role') roles[attribute.value] = 1;
    }
  }
  return {tags: tags, attributes: attributes, roles: Object.keys(roles)};
}"""


def _shape_url(value: object) -> str:
    """Keep the path, drop the query and fragment that can carry tokens."""

    parsed = _split(value)
    if parsed is None:
        return ""
    host, path = parsed
    return f"https://{host}{path}"[:_MAX_URL]


def _shape_name(value: object) -> str | None:
    if type(value) is not str:
        return None
    stripped = value.strip()[:_MAX_SHAPE_NAME]
    if not stripped or any(
        unicodedata.category(item).startswith("C") for item in stripped
    ):
        return None
    return stripped


def _shape_names(value: object) -> tuple[str, ...]:
    if type(value) is not dict:
        return ()
    names = {name for name in map(_shape_name, value) if name is not None}
    return tuple(sorted(names)[:_MAX_SHAPE_ENTRIES])


def _shape_counts(value: object) -> tuple[tuple[str, int], ...]:
    if type(value) is not dict:
        return ()
    counts: list[tuple[str, int]] = []
    for name, count in value.items():
        clean = _shape_name(name)
        if clean is not None and type(count) is int and count >= 0:
            counts.append((clean, count))
    counts.sort(key=lambda item: (-item[1], item[0]))
    return tuple(counts[:_MAX_SHAPE_ENTRIES])


def _shape_roles(value: object) -> tuple[str, ...]:
    """ARIA roles are a closed vocabulary, so a strict pattern is enough."""

    if type(value) not in (list, tuple):
        return ()
    roles = {
        role
        for role in value
        if type(role) is str and _ROLE_VALUE.fullmatch(role) is not None
    }
    return tuple(sorted(roles)[:_MAX_SHAPE_ENTRIES])


def _story_text(value: object) -> str | None:
    """Bound and clean page text to exactly what discovery will accept."""

    if type(value) is not str:
        return None
    cleaned = "".join(
        character
        for character in value
        if character in "\n\t"
        or not unicodedata.category(character).startswith("C")
    ).strip()
    if not cleaned:
        return None
    return cleaned[:_MAX_STORY_TEXT].strip() or None


def _identifier(value: object) -> str | None:
    if type(value) is not str:
        return None
    stripped = value.strip()
    if not 1 <= len(stripped) <= _MAX_IDENTIFIER:
        return None
    if any(unicodedata.category(item).startswith("C") for item in stripped):
        return None
    return stripped


def _timestamp(value: object) -> datetime | None:
    if type(value) is not str or not 1 <= len(value) <= 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A naive timestamp has no defensible interpretation on a global service.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _media_type(value: object) -> StoryMediaType | None:
    try:
        return StoryMediaType(value)
    except ValueError:
        return None


def _media_hash(content: StoryContent) -> str:
    """Digest the story's pixels so a re-posted story keeps one identity."""

    digest = hashlib.sha256()
    digest.update(b"engagemate-story-media-v1")
    images = (content.image,) if content.image is not None else content.video_frames
    for image in images:
        digest.update(b"\0")
        digest.update(image.mime_type.encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(image.data).digest())
    return digest.hexdigest()


__all__ = [
    "MediaFetcher",
    "PlaywrightStoryBrowser",
    "StoryBrowserError",
    "StoryPageShape",
    "StoryPageVerdict",
    "StoryTrayAuthwallError",
    "StoryTrayNavigationError",
    "StoryTraySelectors",
    "StoryTrayUnrecognizedError",
    "SELECTORS",
    "SELECTORS_VERIFIED_AGAINST_INSTAGRAM",
    "classify_story_page",
]
