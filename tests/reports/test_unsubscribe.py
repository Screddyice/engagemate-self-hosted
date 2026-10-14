"""Unsubscribe links: one signed token per user and category, shared with the web app."""
from uuid import UUID

import pytest

from engagemate.reports.layout import email_layout
from engagemate.reports.unsubscribe import text_footer, unsubscribe_token, unsubscribe_url

USER = "11111111-1111-4111-8111-111111111111"


def test_token_matches_the_web_verifier_vector():
    # app/src/__tests__/lib/email-unsubscribe.test.ts checks the same value.
    assert unsubscribe_token("test-internal-key", USER, "activity") == "eybfO-LGLXcdkNUa3FpsBKwzAa-pdPhbwXu2Y4kfL1Q"
    assert unsubscribe_url("test-internal-key", UUID(USER), "weekly_report") == (
        "https://app.example.com/unsubscribe?u=11111111-1111-4111-8111-111111111111"
        "&c=weekly_report&t=PR1dqN1IIxGL1YhP_59jCSkaWTuBLCY_0Ks8SPR1CmY")


def test_a_token_is_bound_to_its_user_category_and_key():
    base = unsubscribe_token("key", USER, "activity")
    assert unsubscribe_token("key", USER, "onboarding") != base
    assert unsubscribe_token("key", "22222222-2222-4222-8222-222222222222", "activity") != base
    assert unsubscribe_token("other", USER, "activity") != base
    with pytest.raises(ValueError):
        unsubscribe_token("key", USER, "everything")
    with pytest.raises(ValueError):
        unsubscribe_token("", USER, "activity")


def test_layout_adds_the_footer_links_only_when_given_a_link():
    assert "Unsubscribe" not in email_layout("T", "<p>b</p>")
    html = email_layout("T", "<p>b</p>", unsubscribe_url='https://app.example.com/unsubscribe?u=1&c="x"',
                        unsubscribe_description="a <daily> summary")
    assert 'href="https://app.example.com/unsubscribe?u=1&amp;c=&quot;x&quot;"' in html
    assert "You get a &lt;daily&gt; summary because you have an EngageMate account." in html
    assert 'href="https://app.example.com/settings"' in html


def test_plain_text_footer_names_both_links():
    footer = text_footer("https://app.example.com/unsubscribe?t=1", "weekly reports")
    assert "Unsubscribe: https://app.example.com/unsubscribe?t=1" in footer
    assert "Manage email settings: https://app.example.com/settings" in footer
