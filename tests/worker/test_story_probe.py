"""Probe guards must reject highlights and leave observed tabs untouched."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts/probe_story_page_shape.py"
_SPEC = importlib.util.spec_from_file_location("story_probe", _PATH)
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


@pytest.mark.parametrize("suffix", ["", "123", "123/"])
def test_regular_prospect_routes(suffix):
    assert probe.matching_viewer(
        "https://www.instagram.com/stories/new.prospect/" + suffix, "new.prospect"
    )


@pytest.mark.parametrize("url", [
    "https://www.instagram.com/stories/highlights/123/",
    "https://www.instagram.com/stories/demo.creator/123/",
    "https://www.instagram.com/stories/new.prospect/123/?r=1",
    "https://www.instagram.com/stories/new.prospect/123/#other",
    "https://instagram.com.evil/stories/new.prospect/",
    "http://instagram.com/stories/new.prospect/",
    "https://user@instagram.com/stories/new.prospect/",
    "https://instagram.com:443/stories/new.prospect/",
    "https://instagram.com/stories/new.prospect/１２３/",
    "https://[broken",
])
def test_other_surfaces_are_rejected(url):
    assert not probe.matching_viewer(url, "new.prospect")


@pytest.mark.parametrize("handle", ["highlights", "HIGHLIGHTS", "../other", "@user", "a/b", ""])
def test_invalid_target_rejected(handle):
    with pytest.raises(ValueError):
        probe.story_url(handle)


async def test_authenticated_navigation_needs_view_acknowledgement():
    with pytest.raises(ValueError, match="allow-story-view"):
        await probe.probe("new.prospect", "http://localhost:9222", 10)


def attached_driver(monkeypatch, urls, *, change_owner=False):
    pages = []
    for url in urls:
        page = SimpleNamespace(url=url, close=AsyncMock(), goto=AsyncMock())
        page.locator = lambda selector: SimpleNamespace(count=AsyncMock(return_value=1))

        async def evaluate(script, page=page):
            if script == probe.CENSUS:
                return {"scanned": 1, "tags": {}, "attributes": {}, "roles": []}
            if change_owner:
                page.url = "https://www.instagram.com/stories/other/123/"
            return {"controls": {"Like": 1}}

        page.evaluate = evaluate
        pages.append(page)
    context = SimpleNamespace(pages=pages, new_page=AsyncMock())
    browser = SimpleNamespace(contexts=[context], close=AsyncMock())
    driver = SimpleNamespace(chromium=SimpleNamespace(
        connect_over_cdp=AsyncMock(return_value=browser),
    ))
    manager = AsyncMock()
    manager.__aenter__.return_value = driver
    monkeypatch.setattr(probe, "async_playwright", lambda: manager)
    return pages, context, browser


async def test_observation_neither_opens_nor_navigates_nor_closes_tabs(monkeypatch):
    pages, context, browser = attached_driver(monkeypatch, [
        "https://www.instagram.com/stories/new.prospect/123/",
    ])
    result = await probe.probe("new.prospect", "http://localhost:9222", 10, observe_current=True)
    assert result["observation_only"] is True
    assert result["status"] is None
    context.new_page.assert_not_called()
    pages[0].goto.assert_not_called()
    pages[0].close.assert_not_called()
    browser.close.assert_not_called()


@pytest.mark.parametrize("urls", [
    ["https://www.instagram.com/stories/highlights/123/"],
    ["https://www.instagram.com/stories/new.prospect/123/"] * 2,
])
async def test_wrong_or_ambiguous_viewer_does_not_create_a_tab(monkeypatch, urls):
    _, context, browser = attached_driver(monkeypatch, urls)
    with pytest.raises(ValueError, match="exactly one"):
        await probe.probe("new.prospect", "http://localhost:9222", 10, observe_current=True)
    context.new_page.assert_not_called()
    browser.close.assert_not_called()


async def test_autoplay_cannot_relabel_another_accounts_measurement(monkeypatch):
    attached_driver(monkeypatch, [
        "https://www.instagram.com/stories/new.prospect/123/",
    ], change_owner=True)
    with pytest.raises(ValueError, match="changed accounts"):
        await probe.probe("new.prospect", "http://localhost:9222", 10, observe_current=True)
