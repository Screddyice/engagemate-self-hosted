"""Narrow Playwright adapter for exact active-story actions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from html import escape
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from engagemate.worker.circuit_breaker import HealthSignal

MAX_SCREENSHOT_BYTES = 1_000_000
MAX_DOM_BYTES = 16_384
_ALLOWED_DOM_ATTRIBUTES = (
    "tagName",
    "role",
    "aria-label",
    "data-testid",
    "data-status",
    "aria-pressed",
)
_SAFE_ATTRIBUTE_VALUES = {
    "role": frozenset({"dialog", "button", "textbox", "status", "alert"}),
    "aria-pressed": frozenset({"true", "false"}),
    "data-status": frozenset({"idle", "sent", "failed", "pending"}),
}


@dataclass(frozen=True, slots=True)
class StorySelectors:
    active_story: str = '[data-engagemate-active-story="true"]'
    like: str = '[data-testid="story-like"]'
    reply: str = '[data-testid="story-reply"]'
    reply_send: str = '[data-testid="story-reply-send"]'
    dm: str = '[data-testid="story-dm"]'
    dm_input: str = '[data-testid="story-dm-input"]'
    dm_send: str = '[data-testid="story-dm-send"]'
    status: str = '[data-testid="story-action-status"]'
    health: str = '[data-testid="account-health"]'
    diagnostic_masks: tuple[str, ...] = (
        "input",
        "textarea",
        '[role="dialog"]',
        '[data-testid*="message"]',
        '[data-private]',
        "img",
        "video",
        '[data-text-content]',
    )


SELECTORS = StorySelectors()


class _RedactedText(str):
    def __repr__(self) -> str:
        return "<redacted-text>"


def _redacted_text(value: object) -> _RedactedText:
    valid = type(value) is str and bool(value.strip()) and len(value) <= 200
    if not valid:
        del value
        raise ValueError("invalid generated text") from None
    safe = _RedactedText(value)
    del value
    return safe


class _SafeExcerptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        rendered = []
        for name, value in attrs:
            if name not in _ALLOWED_DOM_ATTRIBUTES:
                continue
            safe_values = _SAFE_ATTRIBUTE_VALUES.get(name)
            safe = value if safe_values is not None and value in safe_values else "redacted"
            rendered.append(f' {name}="{escape(safe, quote=True)}"')
        self.parts.append(f"<{escape(tag)}{''.join(rendered)}>")

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{escape(tag)}>")


def _sanitize_excerpt(value: object) -> str:
    if type(value) is not str:
        return ""
    parser = _SafeExcerptParser()
    try:
        parser.feed(value)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed diagnostic input must be bounded
        return ""
    return "".join(parser.parts)


def _sanitize_url(value: object) -> str:
    if type(value) is not str:
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return ""


class ActionOutcome(StrEnum):
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, repr=False)
class DiagnosticArtifact:
    screenshot: bytes
    dom_excerpt: str
    current_url: str

    def __repr__(self) -> str:
        return (
            "DiagnosticArtifact(screenshot=<redacted>, "
            "dom_excerpt=<redacted>, current_url=<redacted>)"
        )


class ArtifactSink(Protocol):
    async def write(self, artifact: DiagnosticArtifact) -> None: ...


class InstagramAdapter:
    """Execute one semantic story action without broad selectors."""

    def __init__(self, page: object, artifact_sink: ArtifactSink) -> None:
        self._page = page
        self._sink = artifact_sink

    async def like_story(self, target_handle: str, fingerprint: str) -> ActionOutcome:
        async def action() -> bool:
            root = await self._validate_story(target_handle, fingerprint)
            control = await self._one_visible_in(root, SELECTORS.like)
            if await control.get_attribute("aria-pressed") != "false":
                return False
            await control.click()
            return await control.get_attribute("aria-pressed") == "true"

        return await self._bounded(action())

    async def view_story(self, target_handle: str, fingerprint: str) -> ActionOutcome:
        """Watch a Story without sending anything.

        Opening the Story is the view. This confirms the identified Story is
        the one on screen and clicks no control, so nothing reaches the owner
        beyond the seen marker Instagram records for any viewer.
        """

        async def action() -> bool:
            await self._validate_story(target_handle, fingerprint)
            return True

        return await self._bounded(action())

    async def reply_to_story(
        self, target_handle: str, fingerprint: str, generated_text: str
    ) -> ActionOutcome:
        try:
            safe_text = _redacted_text(generated_text)
        finally:
            del generated_text
        try:
            return await self._bounded(
                self._reply_action(target_handle, fingerprint, safe_text)
            )
        except BaseException:
            del safe_text
            raise

    async def dm_about_story(
        self, target_handle: str, fingerprint: str, generated_text: str
    ) -> ActionOutcome:
        try:
            safe_text = _redacted_text(generated_text)
        finally:
            del generated_text
        try:
            return await self._bounded(
                self._dm_action(target_handle, fingerprint, safe_text)
            )
        except BaseException:
            del safe_text
            raise

    async def _reply_action(
        self, target_handle: str, fingerprint: str, safe_text: _RedactedText
    ) -> bool:
        root = await self._validate_story(target_handle, fingerprint)
        await self._require_unsent(root)
        reply = await self._one_visible_in(root, SELECTORS.reply)
        send = await self._one_visible_in(root, SELECTORS.reply_send)
        if await send.get_attribute("data-recipient") != target_handle:
            return False
        await reply.fill(safe_text)
        await send.click()
        return await self._sent(target_handle, fingerprint, "reply")

    async def _dm_action(
        self, target_handle: str, fingerprint: str, safe_text: _RedactedText
    ) -> bool:
        root = await self._validate_story(target_handle, fingerprint)
        await self._require_unsent(root)
        origin = await self._one_visible_in(root, SELECTORS.dm)
        if await origin.get_attribute("data-recipient") != target_handle:
            return False
        await origin.click()
        composer = await self._one_visible_in(root, SELECTORS.dm_input)
        send = await self._one_visible_in(root, SELECTORS.dm_send)
        if await send.get_attribute("data-recipient") != target_handle:
            return False
        await composer.fill(safe_text)
        await send.click()
        return await self._sent(target_handle, fingerprint, "dm")

    async def detect_health_signal(self) -> HealthSignal:
        try:
            locator = self._page.locator(SELECTORS.health)
            if await locator.count() != 1 or not await locator.is_visible():
                return HealthSignal.HEALTH_UNAVAILABLE
            raw = await locator.get_attribute("data-health-signal")
            return HealthSignal(raw)
        except Exception:  # noqa: BLE001 - absent or unreadable health fails closed
            return HealthSignal.HEALTH_UNAVAILABLE

    async def _bounded(self, action: object) -> ActionOutcome:
        failed = False
        try:
            confirmed = await action
        except Exception:  # noqa: BLE001 - ordinary Playwright failures are bounded
            failed = True
            confirmed = False
        if failed or not confirmed:
            await self._capture_best_effort()
            return ActionOutcome.FAILED
        return ActionOutcome.CONFIRMED

    async def _validate_story(self, target_handle: str, fingerprint: str) -> object:
        if (
            type(target_handle) is not str
            or not target_handle
            or target_handle != target_handle.strip()
            or type(fingerprint) is not str
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        ):
            raise ValueError("invalid story identity")
        root = await self._one_visible(SELECTORS.active_story)
        if (
            await root.get_attribute("data-story-owner") != target_handle
            or await root.get_attribute("data-story-fingerprint") != fingerprint
        ):
            raise ValueError("story identity mismatch")
        return root

    async def _one_visible(self, selector: str) -> object:
        locator = self._page.locator(selector)
        if await locator.count() != 1 or not await locator.is_visible():
            raise ValueError("semantic control unavailable")
        return locator

    async def _require_unsent(self, root: object) -> None:
        status = await self._one_visible_in(root, SELECTORS.status)
        if await status.get_attribute("data-status") == "sent":
            raise ValueError("stale sent state")

    async def _sent(
        self, target_handle: str, fingerprint: str, action: str
    ) -> bool:
        root = await self._one_visible(SELECTORS.active_story)
        status = await self._one_visible_in(root, SELECTORS.status)
        return (
            await status.get_attribute("data-status") == "sent"
            and await status.get_attribute("data-action") == action
            and await status.get_attribute("data-recipient") == target_handle
            and await status.get_attribute("data-story-fingerprint") == fingerprint
        )

    async def _capture_best_effort(self) -> None:
        try:
            masks = [self._page.locator(selector) for selector in SELECTORS.diagnostic_masks]
            masks.append(self._page.locator("body"))
            screenshot = await self._page.screenshot(mask=masks)
            if type(screenshot) is not bytes:
                screenshot = b""
            screenshot = screenshot[:MAX_SCREENSHOT_BYTES]
            raw_excerpt = await self._page.safe_dom_excerpt(
                _ALLOWED_DOM_ATTRIBUTES, MAX_DOM_BYTES
            )
            excerpt = _sanitize_excerpt(raw_excerpt)
            encoded = excerpt.encode("utf-8")[:MAX_DOM_BYTES]
            excerpt = encoded.decode("utf-8", errors="ignore")
            url = _sanitize_url(self._page.url)
            await self._sink.write(DiagnosticArtifact(screenshot, excerpt, url[:2048]))
        except Exception:  # noqa: BLE001 - diagnostics cannot replace action result
            return

    @staticmethod
    async def _one_visible_in(root: object, selector: str) -> object:
        locator = root.locator(selector)
        if await locator.count() != 1 or not await locator.is_visible():
            raise ValueError("semantic control unavailable")
        return locator
