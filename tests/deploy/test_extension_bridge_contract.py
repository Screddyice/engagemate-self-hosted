"""The service worker's onboarding handlers, asserted at the source.

The onboarding branches are the only path by which a web page can make the
extension act. There is no jsdom harness for a MV3 service worker, so these
assertions guard the contract the content script depends on: both handlers
exist, the Instagram login URL is the literal one the flow opens, and no
handler ever reads cookies off an inbound message.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = (REPO_ROOT / "extension" / "background.js").read_text(encoding="utf-8")


def test_background_handles_onboarding_messages() -> None:
    assert 'message.type === "ONBOARDING_PING"' in SOURCE
    assert 'message.type === "ONBOARDING_CONNECT"' in SOURCE
    assert "https://www.instagram.com/accounts/login/" in SOURCE
    assert "message.cookies" not in SOURCE


def test_onboarding_connect_validates_nonce_and_origin_before_opening_instagram() -> None:
    """Only a nonce-bound request from EngageMate may start connection."""

    assert "invalid_connection_input" in SOURCE
    assert "instagram_open_failed" in SOURCE
    assert "ONBOARDING_NONCE.test(message.nonce" in SOURCE
    assert "!origin" in SOURCE
    handler = SOURCE.split('message.type === "ONBOARDING_CONNECT"', 1)[1]
    handler = handler.split('message.type === "ONBOARDING_PROFILE_LOOKUP"', 1)[0]
    assert "message.username" not in handler
    assert "message.loginGeo" not in handler


def test_onboarding_ping_revalidates_nonce_and_origin() -> None:
    handler = SOURCE.split('message.type === "ONBOARDING_PING"', 1)[1]
    handler = handler.split('message.type === "ONBOARDING_CONNECT"', 1)[0]

    assert "ONBOARDING_NONCE.test(message.nonce" in handler
    assert "onboardingOrigin(sender)" in handler


def test_onboarding_connect_uses_the_content_script_origin() -> None:
    """A fresh unpacked install must sync to the page that invoked it."""

    assert "function onboardingOrigin(sender)" in SOURCE
    assert "onboardingOrigin(sender)" in SOURCE
    assert "engagemateUrl: origin" in SOURCE
    # The source trusts local development only; packaging adds real origins.
    assert 'const ENGAGEMATE_WEB_ORIGINS = ["http://localhost:3030"];' in SOURCE
    assert "engagemate.io" not in SOURCE


def test_retired_popup_configuration_cannot_select_an_arbitrary_origin() -> None:
    config = SOURCE.split("async function getConfig()", 1)[1]
    config = config.split("function onboardingOrigin", 1)[0]

    assert "ONBOARDING_ENGAGEMATE_ORIGINS.has(origin)" in config
    assert "return { url: ENGAGEMATE_WEB_ORIGINS[0] }" in config


def test_onboarding_reply_exposes_no_derived_account_metadata() -> None:
    """The page confirms connection through its own account API."""

    handler = SOURCE.split('message.type === "ONBOARDING_CONNECT"', 1)[1]
    assert "ext.runtime.onMessage.addListener" in handler
    handler = handler.split("ext.runtime.onMessage.addListener", 1)[0]

    assert 'sendResponse({ status: "connected" })' in handler
    assert "onboardingAccount" not in SOURCE


EXTENSION_SCRIPTS = ("background.js", "content-script.js", "popup.js")


def test_every_extension_script_reaches_apis_through_the_namespace_shim() -> None:
    """Safari runs the same files, so no script may hard-code `chrome.`.

    Each script picks Safari's `browser` namespace only when it carries a real
    `runtime.sendMessage` function and falls back to `chrome` otherwise. A
    direct `chrome.` call elsewhere would bypass that selection.
    """

    for name in EXTENSION_SCRIPTS:
        source = (REPO_ROOT / "extension" / name).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("//")
        )
        assert 'typeof globalThis.browser?.runtime?.sendMessage === "function"' in code or (
            'typeof safari?.sendMessage === "function"' in code
        ), name
        assert code.count("globalThis.chrome") == 1, name
        stripped = code.replace("globalThis.chrome", "")
        assert "chrome." not in stripped, name


def test_identity_comes_from_an_authenticated_bounded_instagram_lookup() -> None:
    assert "https://www.instagram.com/api/v1/accounts/current_user/?edit=true" in SOURCE
    assert 'credentials: "include"' in SOURCE
    assert 'cache: "no-store"' in SOURCE
    assert '"X-IG-App-ID": INSTAGRAM_WEB_APP_ID' in SOURCE
    assert "new AbortController()" in SOURCE
    assert "INSTAGRAM_IDENTITY_TIMEOUT_MS" in SOURCE


def test_instagram_identity_must_match_the_session_cookie_user_id() -> None:
    assert 'c.name === "ds_user_id"' in SOURCE
    assert "endpointUserIds.includes(igUserId)" in SOURCE
    assert 'code: "identity_discovery_failed"' in SOURCE
    assert 'code: "identity_rate_limited"' in SOURCE


def test_login_geography_is_selected_automatically() -> None:
    assert "/api/extension/suggest-geo" in SOURCE
    assert "browserTimezone()" in SOURCE
    assert 'code: "geo_discovery_failed"' in SOURCE


def test_popup_has_one_connect_action_and_no_manual_configuration() -> None:
    popup = (REPO_ROOT / "extension" / "popup.html").read_text(encoding="utf-8")
    popup_script = (REPO_ROOT / "extension" / "popup.js").read_text(encoding="utf-8")

    assert popup.count("<button") == 1
    assert ">Connect Instagram</button>" in popup
    assert "<input" not in popup
    for forbidden in (
        "username",
        "country",
        "region",
        "city",
        "EngageMate URL",
        "Health",
        "Last sync",
    ):
        assert forbidden.lower() not in popup.lower()

    assert "Connected" in popup_script
    assert "Connecting…" in popup_script
    assert "Reconnect Instagram" in popup_script
    assert "account.health" not in popup_script
    assert "lastSyncAt" not in popup_script
