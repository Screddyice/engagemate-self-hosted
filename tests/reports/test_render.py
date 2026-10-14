"""Plain-text and HTML weekly report rendering contracts."""

from __future__ import annotations

from datetime import datetime, timezone

from engagemate.reports.aggregate import previous_week
from engagemate.reports.models import AccountWeeklyReport, ManagerWeeklyReport
from engagemate.reports.render import RenderedEmail, render_weekly_report


def _account(
    username: str,
    *,
    follower_start: int | None,
    follower_end: int | None,
    engagement_start: int | None,
    engagement_end: int | None,
    reviewed: int = 0,
    likes: int = 0,
    replies: int = 0,
    dms: int = 0,
    views: int = 0,
    skipped: int = 0,
    gated: int = 0,
    failed: int = 0,
) -> AccountWeeklyReport:
    return AccountWeeklyReport(
        username=username,
        follower_start=follower_start,
        follower_end=follower_end,
        engagement_rate_bps_start=engagement_start,
        engagement_rate_bps_end=engagement_end,
        opportunities_reviewed=reviewed,
        likes_sent=likes,
        replies_sent=replies,
        dms_sent=dms,
        views_sent=views,
        skipped=skipped,
        gated=gated,
        failed=failed,
    )


def _report(*accounts: AccountWeeklyReport) -> ManagerWeeklyReport:
    return ManagerWeeklyReport(
        period=previous_week(
            datetime(2026, 8, 10, 9, tzinfo=timezone.utc),
            "UTC",
        ),
        accounts=tuple(accounts),
    )


def test_renderer_shows_every_signed_and_unavailable_trend() -> None:
    report = _report(
        _account(
            "positive.account",
            follower_start=100,
            follower_end=112,
            engagement_start=200,
            engagement_end=245,
        ),
        _account(
            "negative.account",
            follower_start=112,
            follower_end=107,
            engagement_start=245,
            engagement_end=200,
        ),
        _account(
            "steady.account",
            follower_start=107,
            follower_end=107,
            engagement_start=200,
            engagement_end=200,
        ),
        _account(
            "unavailable.account",
            follower_start=None,
            follower_end=107,
            engagement_start=200,
            engagement_end=None,
        ),
    )

    rendered = render_weekly_report(report)

    assert isinstance(rendered, RenderedEmail)
    assert "4 managed accounts" in rendered.text
    assert "Follower change: +12" in rendered.text
    assert "Follower change: -5" in rendered.text
    assert "Follower change: 0" in rendered.text
    assert "Engagement-rate change: +0.45 percentage points" in rendered.text
    assert "Engagement-rate change: -0.45 percentage points" in rendered.text
    assert "Engagement-rate change: 0.00 percentage points" in rendered.text
    assert rendered.text.count("Not available") == 2
    for username in (
        "positive.account",
        "negative.account",
        "steady.account",
        "unavailable.account",
    ):
        assert f"@{username}" in rendered.text
        assert f"@{username}" in rendered.html


def test_renderer_preserves_large_engagement_delta_precision() -> None:
    rendered = render_weekly_report(
        _report(
            _account(
                "large.account",
                follower_start=0,
                follower_end=0,
                engagement_start=0,
                engagement_end=9_007_199_254_740_993,
            )
        )
    )

    assert (
        "Engagement-rate change: +90071992547409.93 percentage points"
        in rendered.text
    )


def test_renderer_pluralizes_one_account_and_lists_zero_activity() -> None:
    rendered = render_weekly_report(
        _report(
            _account(
                "quiet.account",
                follower_start=20,
                follower_end=20,
                engagement_start=125,
                engagement_end=125,
            )
        )
    )

    assert "1 managed account" in rendered.text
    assert "Opportunities reviewed: 0" in rendered.text
    assert "Likes sent: 0" in rendered.text
    assert "Replies sent: 0" in rendered.text
    assert "DMs sent: 0" in rendered.text
    assert "Views sent: 0" in rendered.text
    assert "Skipped: 0" in rendered.text
    assert "Gated: 0" in rendered.text
    assert "Failed: 0" in rendered.text


def test_renderer_explains_total_time_formula_and_observational_limits() -> None:
    rendered = render_weekly_report(
        _report(
            _account(
                "active.account",
                follower_start=10,
                follower_end=13,
                engagement_start=300,
                engagement_end=325,
                reviewed=4,
                likes=3,
                replies=1,
                dms=2,
                views=5,
                skipped=6,
                gated=7,
                failed=8,
            )
        )
    )

    assert "Estimated manual time saved: 5 minutes" in rendered.text
    assert "30 seconds per Story opportunity reviewed" in rendered.text
    assert "60 seconds per reply or DM sent" in rendered.text
    assert "Account trends are observational" in rendered.text
    assert "Estimated manual time saved" in rendered.html
    assert "Account trends are observational" in rendered.html


def test_renderer_uses_generic_greeting_and_escapes_managed_username() -> None:
    username = "owner<Manager & Co>"
    rendered = render_weekly_report(
        _report(
            _account(
                username,
                follower_start=10,
                follower_end=12,
                engagement_start=100,
                engagement_end=120,
            )
        )
    )

    assert "Hello," in rendered.text
    assert "Hello," in rendered.html
    assert "@owner&lt;Manager &amp; Co&gt;" in rendered.html
    assert "@owner<Manager & Co>" not in rendered.html
    assert "Manager name" not in rendered.text


def test_renderer_and_repr_exclude_nonaggregate_fixture_values() -> None:
    rendered = render_weekly_report(
        _report(
            _account(
                "safe.managed.account",
                follower_start=10,
                follower_end=12,
                engagement_start=100,
                engagement_end=120,
            )
        )
    )

    combined = "\n".join((rendered.subject, rendered.text, rendered.html, repr(rendered)))
    assert repr(rendered) == "RenderedEmail(<redacted>)"
    for forbidden in (
        "private.target.username",
        "private generated reply",
        "recipient@example.test",
        "captured-cookie-secret",
        "resend-api-secret",
        "proxy-user:proxy-password",
        "raw-provider-payload",
        "private model prompt",
    ):
        assert forbidden not in combined


def test_weekly_report_links_to_its_own_unsubscribe() -> None:
    report = _report(_account("sender", follower_start=1, follower_end=2, engagement_start=None, engagement_end=None))
    link = "https://app.example.com/unsubscribe?u=1&c=weekly_report&t=abc"
    email = render_weekly_report(report, unsubscribe_url=link)
    assert f"Unsubscribe: {link}" in email.text
    assert "https://app.example.com/unsubscribe?u=1&amp;c=weekly_report&amp;t=abc" in email.html
    assert "Unsubscribe" not in render_weekly_report(report).html
