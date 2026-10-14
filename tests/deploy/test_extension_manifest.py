"""The session bridge can only POST to origins the manifest already allows.

Chrome MV3 blocks a service-worker fetch to any host outside `host_permissions`.
The committed manifest trusts local development only; a deployment's origin is
added at packaging time from ENGAGEMATE_WEB_ORIGIN, so no public hostname is
trusted by default. The project's original domain lapsed, and a built-in
default would have handed the bridge to whoever registered it next.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = json.loads(
    (REPO_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8")
)

DEV_PATTERN = "http://localhost:3030/*"
INSTAGRAM_PATTERNS = {"https://instagram.com/*", "https://*.instagram.com/*"}


def test_source_manifest_trusts_no_public_web_origin() -> None:
    """Unconfigured, the bridge may reach Instagram and local development only."""

    assert set(MANIFEST["host_permissions"]) == INSTAGRAM_PATTERNS | {DEV_PATTERN}


def test_instagram_stays_reachable() -> None:
    permissions = MANIFEST["host_permissions"]

    assert "https://instagram.com/*" in permissions
    assert "https://*.instagram.com/*" in permissions


def test_the_bridge_asks_for_no_capability_beyond_reading_cookies() -> None:
    """A widened host list must not become a widened permission list."""

    assert sorted(MANIFEST["permissions"]) == ["alarms", "cookies", "storage"]
    assert "tabs" not in MANIFEST["permissions"]
    assert "scripting" not in MANIFEST["permissions"]
    assert "webRequest" not in MANIFEST["permissions"]

    # The onboarding bridge earns exactly one content script, on EngageMate's
    # own pages. It must never be injected into Instagram, where the session
    # cookies it exists to protect are readable from the DOM.
    for script in MANIFEST.get("content_scripts", []):
        for pattern in script["matches"]:
            assert "instagram.com" not in pattern
            assert pattern not in ("<all_urls>", "*://*/*", "https://*/*")


def test_no_wildcard_host_grants_the_whole_web() -> None:
    for pattern in MANIFEST["host_permissions"]:
        assert pattern not in ("<all_urls>", "*://*/*", "https://*/*")


def test_engagemate_content_script_is_narrowly_scoped() -> None:
    """The page bridge may run on EngageMate origins and nowhere else.

    A content script is the only new execution surface this extension gained,
    so its match list is the whole of its blast radius.
    """

    [script] = MANIFEST["content_scripts"]
    assert script["js"] == ["content-script.js"]
    assert script["matches"] == [DEV_PATTERN]
    assert script.get("run_at") == "document_start"
