"""Immutable aggregate-only values used to build weekly manager reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from engagemate.reports.aggregate import estimate_time_saved_seconds, trend_delta

_SIGNED_64_BIT_MAX = 2**63 - 1


@dataclass(frozen=True, slots=True, repr=False)
class ReportingPeriod:
    """Display dates and UTC filter instants for one completed local week."""

    timezone_name: str
    week_start: date
    week_end: date
    start_utc: datetime
    end_utc: datetime

    def __repr__(self) -> str:
        return "ReportingPeriod(<redacted>)"

    def __post_init__(self) -> None:
        zone = _require_timezone(self.timezone_name)
        if type(self.week_start) is not date or self.week_start.weekday() != 0:
            raise ValueError("week_start must be a Monday date")
        if type(self.week_end) is not date or self.week_end != self.week_start + timedelta(days=6):
            raise ValueError("week_end must be the following Sunday")
        _require_utc_datetime(self.start_utc, "start_utc")
        _require_utc_datetime(self.end_utc, "end_utc")
        expected_start = datetime.combine(self.week_start, time.min, tzinfo=zone).astimezone(
            timezone.utc
        )
        expected_end = datetime.combine(
            self.week_start + timedelta(days=7), time.min, tzinfo=zone
        ).astimezone(timezone.utc)
        if self.start_utc != expected_start or self.end_utc != expected_end:
            raise ValueError("UTC bounds must match the timezone's local reporting week")


@dataclass(frozen=True, slots=True, repr=False)
class AccountWeeklyReport:
    """One managed account's weekly aggregate values, with no target-level data."""

    username: str
    follower_start: int | None
    follower_end: int | None
    engagement_rate_bps_start: int | None
    engagement_rate_bps_end: int | None
    opportunities_reviewed: int
    likes_sent: int
    replies_sent: int
    dms_sent: int
    views_sent: int
    skipped: int
    gated: int
    failed: int
    follows_sent: int = 0
    unfollows_sent: int = 0

    def __repr__(self) -> str:
        return "AccountWeeklyReport(<redacted>)"

    def __post_init__(self) -> None:
        if type(self.username) is not str or not self.username.strip():
            raise ValueError("username must be a nonempty managed-account username")
        _nullable_count(self.follower_start, "follower_start")
        _nullable_count(self.follower_end, "follower_end")
        _nullable_count(self.engagement_rate_bps_start, "engagement_rate_bps_start")
        _nullable_count(self.engagement_rate_bps_end, "engagement_rate_bps_end")
        for name in (
            "opportunities_reviewed",
            "likes_sent",
            "replies_sent",
            "dms_sent",
            "views_sent",
            "skipped",
            "gated",
            "failed",
            "follows_sent",
            "unfollows_sent",
        ):
            _count(getattr(self, name), name)

    @property
    def follower_delta(self) -> int | None:
        """The observed follower difference when both snapshot boundaries exist."""

        return trend_delta(self.follower_start, self.follower_end)

    @property
    def engagement_rate_bps_delta(self) -> int | None:
        """The observed public engagement-rate difference when data is complete."""

        return trend_delta(
            self.engagement_rate_bps_start,
            self.engagement_rate_bps_end,
        )

    @property
    def estimated_time_saved_seconds(self) -> int:
        """The transparent estimate based only on review and sent composition work."""

        return estimate_time_saved_seconds(
            self.opportunities_reviewed,
            self.replies_sent,
            self.dms_sent,
        )


@dataclass(frozen=True, slots=True, repr=False)
class ManagerWeeklyReport:
    """A manager's completed-week period and account-level aggregate sections."""

    period: ReportingPeriod
    accounts: tuple[AccountWeeklyReport, ...]

    def __repr__(self) -> str:
        return "ManagerWeeklyReport(<redacted>)"

    def __post_init__(self) -> None:
        if not isinstance(self.period, ReportingPeriod):
            raise ValueError("period must be a ReportingPeriod")
        if type(self.accounts) is not tuple or not all(
            isinstance(account, AccountWeeklyReport) for account in self.accounts
        ):
            raise ValueError("accounts must be AccountWeeklyReport values")

    @property
    def estimated_time_saved_seconds(self) -> int:
        """Return the exact sum of the account-level manual-time estimates."""

        total = sum(account.estimated_time_saved_seconds for account in self.accounts)
        if total > _SIGNED_64_BIT_MAX:
            raise ValueError("estimated time exceeds signed 64-bit range")
        return total


def _count(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _SIGNED_64_BIT_MAX:
        raise ValueError(f"{name} must be a signed 64-bit nonnegative integer")
    return value


def _nullable_count(value: object, name: str) -> int | None:
    if value is None:
        return None
    return _count(value, name)


def _require_utc_datetime(value: object, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is not timezone.utc:
        raise ValueError(f"{name} must be a UTC datetime")
    return value


def _require_timezone(value: object) -> ZoneInfo:
    if type(value) is not str or not value:
        raise ValueError("timezone_name must be an IANA timezone")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ValueError("timezone_name must be an IANA timezone") from exc
