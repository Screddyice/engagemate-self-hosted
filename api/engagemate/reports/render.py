"""Deterministic plain-text and HTML rendering for aggregate weekly reports."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from engagemate.reports.layout import email_layout
from engagemate.reports.unsubscribe import text_footer

WEEKLY_DESCRIPTION = "a weekly EngageMate report"
from engagemate.reports.models import AccountWeeklyReport, ManagerWeeklyReport


@dataclass(frozen=True, slots=True, repr=False)
class RenderedEmail:
    """A complete provider-ready email whose representation reveals no content."""

    subject: str
    text: str
    html: str

    def __post_init__(self) -> None:
        for value in (self.subject, self.text, self.html):
            if type(value) is not str or not value.strip():
                raise ValueError("rendered email fields must be nonempty strings")

    def __repr__(self) -> str:
        return "RenderedEmail(<redacted>)"


def _signed_count(value: int | None) -> str:
    if value is None:
        return "Not available"
    if value > 0:
        return f"+{value}"
    return str(value)


def _engagement_delta(value: int | None) -> str:
    if value is None:
        return "Not available"
    whole, fraction = divmod(abs(value), 100)
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}{whole}.{fraction:02d} percentage points"


def _duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3_600)
    minutes, remaining_seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} {'hour' if hours == 1 else 'hours'}")
    if minutes:
        parts.append(f"{minutes} {'minute' if minutes == 1 else 'minutes'}")
    if remaining_seconds or not parts:
        parts.append(
            f"{remaining_seconds} "
            f"{'second' if remaining_seconds == 1 else 'seconds'}"
        )
    return " ".join(parts)


def _activity_lines(account: AccountWeeklyReport) -> tuple[str, ...]:
    return (
        f"Total confirmed actions: {account.likes_sent + account.replies_sent + account.dms_sent + account.views_sent + account.follows_sent + account.unfollows_sent}",
        f"Follows: {account.follows_sent}",
        f"Unfollows: {account.unfollows_sent}",
        f"Estimated manual time saved: {_duration(account.estimated_time_saved_seconds)}",
        f"Opportunities reviewed: {account.opportunities_reviewed}",
        f"Likes sent: {account.likes_sent}",
        f"Replies sent: {account.replies_sent}",
        f"DMs sent: {account.dms_sent}",
        f"Views sent: {account.views_sent}",
        f"Skipped: {account.skipped}",
        f"Gated: {account.gated}",
        f"Failed: {account.failed}",
    )


def _account_text(account: AccountWeeklyReport) -> str:
    lines = (
        f"@{account.username}",
        f"Follower change: {_signed_count(account.follower_delta)}",
        "Engagement-rate change: "
        f"{_engagement_delta(account.engagement_rate_bps_delta)}",
        *_activity_lines(account),
    )
    return "\n".join(lines)


def _account_html(account: AccountWeeklyReport) -> str:
    rows = (
        ("Follower change", _signed_count(account.follower_delta)),
        (
            "Engagement-rate change",
            _engagement_delta(account.engagement_rate_bps_delta),
        ),
        *(
            tuple(line.split(": ", 1))
            for line in _activity_lines(account)
        ),
    )
    rendered_rows = "".join(
        "<tr>"
        '<td style="padding:6px 12px 6px 0;color:#93c5fd;vertical-align:top;">'
        f"{escape(label)}</td>"
        '<td style="padding:6px 0;color:#eff6ff;text-align:right;vertical-align:top;">'
        f"{escape(value)}</td>"
        "</tr>"
        for label, value in rows
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;margin:0 0 20px;border:1px solid #1f2b46;'
        'border-radius:8px;border-collapse:separate;">'
        "<tr><td style=\"padding:18px;\">"
        '<div style="font-size:18px;font-weight:700;color:#eff6ff;margin-bottom:8px;">'
        f"@{escape(account.username)}</div>"
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="width:100%;border-collapse:collapse;">'
        f"{rendered_rows}</table></td></tr></table>"
    )


def render_weekly_report(report: ManagerWeeklyReport, *, unsubscribe_url: str | None = None) -> RenderedEmail:
    """Render one aggregate-only manager report into deterministic email bodies."""

    if not isinstance(report, ManagerWeeklyReport):
        raise ValueError("report must be a ManagerWeeklyReport")

    period = report.period
    account_count = len(report.accounts)
    account_label = "managed account" if account_count == 1 else "managed accounts"
    date_range = (
        f"{period.week_start.strftime('%B %-d')} to "
        f"{period.week_end.strftime('%B %-d, %Y')}"
    )
    subject = f"EngageMate weekly report: {period.week_start.isoformat()}"
    text_sections = "\n\n".join(_account_text(account) for account in report.accounts)
    text = (
        "EngageMate weekly report\n"
        f"{date_range}\n\n"
        "Hello,\n\n"
        f"Your weekly summary covers {account_count} {account_label}.\n\n"
        f"{text_sections}\n\n"
        f"Estimated manual time saved: {_duration(report.estimated_time_saved_seconds)}\n"
        "Estimate formula: 30 seconds per Story opportunity reviewed plus "
        "60 seconds per reply or DM sent. Likes, views, follows, and unfollows add no estimated time.\n\n"
        "Account trends are observational and do not prove that EngageMate caused "
        "the changes."
    )
    if unsubscribe_url:
        text += text_footer(unsubscribe_url, WEEKLY_DESCRIPTION)

    account_tables = "".join(_account_html(account) for account in report.accounts)
    html = email_layout("Your weekly activity summary",
        f'<p>{escape(date_range)}</p><p>Hello,</p><p>Your weekly summary covers {account_count} {account_label}.</p>'
        + account_tables
        + f'<h2 style="font-size:18px;color:#eff6ff;">Estimated manual time saved</h2><p>{escape(_duration(report.estimated_time_saved_seconds))}</p>'
        + '<p style="color:#93c5fd;">Estimate formula: 30 seconds per Story opportunity reviewed plus 60 seconds per reply or DM sent. Likes, views, follows, and unfollows add no estimated time.</p>'
        + '<p style="color:#93c5fd;">Account trends are observational and do not prove that EngageMate caused the changes.</p>',
        unsubscribe_url=unsubscribe_url, unsubscribe_description=WEEKLY_DESCRIPTION)
    return RenderedEmail(subject=subject, text=text, html=html)
