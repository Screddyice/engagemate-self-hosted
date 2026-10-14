"""Safe, aggregate-only weekly reporting value objects and calculations."""

from engagemate.reports.aggregate import (
    estimate_time_saved_seconds,
    previous_week,
    trend_delta,
)
from engagemate.reports.models import (
    AccountWeeklyReport,
    ManagerWeeklyReport,
    ReportingPeriod,
)
from engagemate.reports.render import RenderedEmail, render_weekly_report
from engagemate.reports.resend import ResendClient, ResendError
from engagemate.reports.dispatcher import CanaryReportRepository

__all__ = (
    "AccountWeeklyReport",
    "CanaryReportRepository",
    "ManagerWeeklyReport",
    "ReportingPeriod",
    "RenderedEmail",
    "ResendClient",
    "ResendError",
    "estimate_time_saved_seconds",
    "previous_week",
    "render_weekly_report",
    "trend_delta",
)
