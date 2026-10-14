#!/usr/bin/env python3
"""Measure what Instagram actually serves at a story URL.

`PlaywrightStoryBrowser`'s tray selectors have never matched Instagram, and
nobody can write real ones without watching an authenticated story tray load.
This is the instrument for that. It reports the same four facts
`classify_story_page` decides on, plus the same structural census
`StoryPageShape` captures, so a run against a live session produces the
evidence needed to replace the invented selectors with true ones.

Run it logged out and it reproduces the measurements pinned in
`tests/worker/test_story_browser.py`:

    python scripts/probe_story_page_shape.py instagram

Run it against a real session by pointing it at a Chrome that already has one:

    python scripts/probe_story_page_shape.py <target> --cdp http://127.0.0.1:9222 --observe-current

Attaching with --observe-current reads an already open viewer without navigation.
Navigating an authenticated browser requires --allow-story-view because Instagram
can record a view when the URL loads. The probe never types, likes, or replies.
It prints
element counts, attribute names, and ARIA roles, never text or attribute
values, because on an authenticated page those hold captions and DMs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

TRAY = '[data-engagemate-story-tray="true"]'
AUTH = (
    'a[href^="/accounts/login/"], '
    'a[href^="/accounts/emailsignup/"], '
    'a[href^="https://www.instagram.com/accounts/login/"], '
    'a[href^="https://www.instagram.com/accounts/emailsignup/"]'
)
CENSUS = """() => {
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
  return {scanned: scanned, tags: tags, attributes: attributes,
          roles: Object.keys(roles)};
}"""

# Measured on a regular Story, September 13, 2026. These controls identify
# the viewer shell only: they do not prove audience eligibility or an action result.
# Never emit arbitrary labels, media URLs, captions, or message text.
VIEWER_CENSUS = """() => {
  const labels = ['Play', 'Pause', 'Menu', 'Previous', 'Next', 'Close', 'Like', 'Direct'];
  const controls = Object.fromEntries(labels.map(label => [label, 0]));
  const visible = e => e.getClientRects().length > 0;
  for (const label of labels) {
    const buttons = new Set();
    for (const icon of document.querySelectorAll('svg[aria-label]')) {
      const button = icon.closest('button, [role="button"]');
      if (icon.getAttribute('aria-label') === label && button && visible(button)) {
        buttons.add(button);
      }
    }
    controls[label] = buttons.size;
  }
  return {
    controls,
    visible_reply_inputs: [...document.querySelectorAll('textarea[placeholder^="Reply to "]')].filter(visible).length,
    visible_time_elements: [...document.querySelectorAll('time[datetime]')].filter(visible).length,
    visible_video_elements: [...document.querySelectorAll('video')].filter(visible).length,
    fixture_markers: document.querySelectorAll('[data-engagemate-story-tray], [data-engagemate-active-story]').length
  };
}"""


def story_url(handle: str) -> str:
    if not isinstance(handle, str) or re.fullmatch(
        r"[A-Za-z0-9_](?:[A-Za-z0-9._]{0,28}[A-Za-z0-9_])?", handle
    ) is None:
        raise ValueError("expected an Instagram handle, not a URL")
    if handle.lower() == "highlights":
        raise ValueError("highlights are not current prospect Stories")
    return f"https://www.instagram.com/stories/{handle.lower()}/"


def matching_viewer(url: str, handle: str) -> bool:
    """Accept only the named prospect's regular Story route."""
    expected_path = urlsplit(story_url(handle)).path
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc in {"www.instagram.com", "instagram.com"}
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(re.escape(expected_path) + r"(?:[0-9]+/?)?", parsed.path) is not None
        )
    except ValueError:
        return False


async def probe(
    handle: str, cdp: str | None, settle_ms: int, *,
    observe_current: bool = False, allow_story_view: bool = False,
) -> dict:
    url = story_url(handle)
    if observe_current and not cdp:
        raise ValueError("--observe-current requires --cdp")
    if cdp and not observe_current and not allow_story_view:
        raise ValueError("authenticated navigation requires --allow-story-view")
    async with async_playwright() as driver:
        if cdp:
            browser = await driver.chromium.connect_over_cdp(cdp)
            context = browser.contexts[0]
        else:
            browser = await driver.chromium.launch(headless=True)
            context = await browser.new_context()
        if observe_current:
            pages = [page for page in context.pages if matching_viewer(page.url, handle)]
            if len(pages) != 1:
                raise ValueError("open exactly one matching Story viewer before observing")
            page = pages[0]
            # Confirm the displayed owner as well as the URL. Never use a
            # highlight as prospect evidence.
            owners = page.locator(f'a[href="/{handle.lower()}/"]:visible')
            if await owners.count() == 0:
                raise ValueError("the open viewer does not identify the expected account")
        else:
            page = await context.new_page()
        try:
            response = None
            if not observe_current:
                response = await page.goto(
                    url, wait_until="domcontentloaded", timeout=30_000
                )
                try:
                    await page.wait_for_load_state("networkidle", timeout=settle_ms)
                except Exception:  # noqa: BLE001 - a busy page is still measurable
                    pass
            census = await page.evaluate(CENSUS)
            viewer = await page.evaluate(VIEWER_CENSUS)
            if observe_current and not matching_viewer(page.url, handle):
                raise ValueError("the Story viewer changed accounts during observation")
            return {
                "requested": None if observe_current else url,
                "observation_only": observe_current,
                "status": response.status if response else None,
                "final_url": page.url,
                "redirected": page.url != url,
                "auth_affordances": await page.locator(AUTH).count(),
                "tray_matches": await page.locator(TRAY).count(),
                "scanned_elements": census["scanned"],
                "tag_counts": sorted(
                    census["tags"].items(), key=lambda item: -item[1]
                )[:40],
                "attribute_names": sorted(census["attributes"]),
                "role_values": sorted(census["roles"]),
                "viewer_shell": viewer,
            }
        finally:
            if not observe_current:
                await page.close()
            if not cdp:
                await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("handle", help="Instagram handle whose tray to measure")
    parser.add_argument(
        "--cdp",
        help="Attach to a Chrome already holding a session, e.g. http://127.0.0.1:9222",
    )
    parser.add_argument("--settle-ms", type=int, default=12_000)
    parser.add_argument("--observe-current", action="store_true")
    parser.add_argument("--allow-story-view", action="store_true")
    arguments = parser.parse_args()

    try:
        result = asyncio.run(probe(
            arguments.handle, arguments.cdp, arguments.settle_ms,
            observe_current=arguments.observe_current,
            allow_story_view=arguments.allow_story_view,
        ))
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))
    if result["tray_matches"] != 1:
        print(
            "\ntray_matches != 1: these selectors do not describe this page. "
            "The attribute names above are where real ones come from.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
