"""Email links come from ENGAGEMATE_WEB_ORIGIN, with no built-in domain.

The project's original domain lapsed. A default would send every reader of
these emails to whoever registers it, so an unset origin must stop rendering.
"""

import pytest

from engagemate.reports.layout import email_layout
from engagemate.reports.onboarding import render_onboarding
from engagemate.reports.reconnect import ReconnectEmailDispatcher
from engagemate.reports.recipients import UNSUBSCRIBE
from engagemate.reports.unsubscribe import unsubscribe_url
from engagemate.web_origin import WebOriginNotConfigured, normalize_origin, support_email, web_origin

USER = "11111111-1111-4111-8111-111111111111"


def test_unset_origin_stops_every_link_builder(monkeypatch):
    monkeypatch.delenv("ENGAGEMATE_WEB_ORIGIN")
    with pytest.raises(WebOriginNotConfigured, match="ENGAGEMATE_WEB_ORIGIN"):
        email_layout("T", "<p>b</p>")
    with pytest.raises(WebOriginNotConfigured):
        unsubscribe_url("key", USER, "activity")
    with pytest.raises(WebOriginNotConfigured):
        render_onboarding("welcome", {"name": "Ana"})
    with pytest.raises(WebOriginNotConfigured):
        ReconnectEmailDispatcher.render(None, {"details": {"account": "example"}})


def test_configured_origin_is_the_only_host_in_an_email(monkeypatch):
    monkeypatch.setenv("ENGAGEMATE_WEB_ORIGIN", "https://mail-test.example.org")
    email = render_onboarding("welcome", {"name": "Ana"}, unsubscribe_url("key", USER, "onboarding"))
    assert "https://mail-test.example.org/onboarding" in email.text
    assert "https://mail-test.example.org/unsubscribe?" in email.html
    assert 'href="https://mail-test.example.org/settings"' in email.html
    assert "engagemate.io" not in email.text + email.html


def test_support_address_is_shown_only_when_configured(monkeypatch):
    assert "mailto:" not in email_layout("T", "<p>b</p>")
    monkeypatch.setenv("ENGAGEMATE_SUPPORT_EMAIL", "help@example.com")
    assert 'href="mailto:help@example.com"' in email_layout("T", "<p>b</p>")
    monkeypatch.setenv("ENGAGEMATE_SUPPORT_EMAIL", "not an address")
    with pytest.raises(ValueError):
        support_email()


@pytest.mark.parametrize(
    "value",
    ["http://app.example.com", "https://app.example.com/path", "https://u:p@app.example.com", "app.example.com"],
)
def test_malformed_origin_is_rejected(monkeypatch, value):
    monkeypatch.setenv("ENGAGEMATE_WEB_ORIGIN", value)
    with pytest.raises(ValueError):
        web_origin()


def test_loopback_http_is_allowed_for_local_development():
    assert normalize_origin("http://localhost:3030") == "http://localhost:3030"


def test_owner_unsubscribe_links_are_stripped_on_any_host():
    for host in ("https://app.example.com", "https://old.example.net", "http://localhost:3030"):
        assert UNSUBSCRIBE.sub("X", f"go {host}/unsubscribe?u=1&t=s now") == "go X now"


def test_commercial_invitations_are_not_supported():
    with pytest.raises(ValueError):
        render_onboarding("invite", {"invite_url": "https://example.com/invitations/accept"})
