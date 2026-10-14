from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timezone

import pytest

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


@pytest.mark.parametrize(
    ("reviewed", "replies", "dms", "seconds"),
    [(0, 0, 0, 0), (1, 0, 0, 30), (4, 1, 0, 180), (4, 1, 2, 300)],
)
def test_time_saved_formula(reviewed: int, replies: int, dms: int, seconds: int) -> None:
    assert estimate_time_saved_seconds(reviewed, replies, dms) == seconds


def test_previous_week_is_monday_through_sunday_across_dst() -> None:
    period = previous_week(datetime(2026, 3, 9, 14, tzinfo=timezone.utc), "America/New_York")
    assert period.week_start.isoformat() == "2026-03-02"
    assert period.week_end.isoformat() == "2026-03-08"
    assert period.start_utc < period.end_utc


def test_missing_snapshot_delta_stays_unavailable() -> None:
    assert trend_delta(None, 100) is None
    assert trend_delta(100, None) is None


def test_previous_week_stores_half_open_utc_filter_boundaries() -> None:
    period = previous_week(datetime(2026, 11, 2, 14, tzinfo=timezone.utc), "America/New_York")

    assert period.week_start == date(2026, 10, 26)
    assert period.week_end == date(2026, 11, 1)
    assert period.start_utc == datetime(2026, 10, 26, 4, tzinfo=timezone.utc)
    assert period.end_utc == datetime(2026, 11, 2, 5, tzinfo=timezone.utc)
    assert period.start_utc.tzinfo is timezone.utc
    assert period.end_utc.tzinfo is timezone.utc


@pytest.mark.parametrize("value", [True, False, -1, 2**63])
def test_time_saved_rejects_invalid_counts(value: int) -> None:
    with pytest.raises(ValueError):
        estimate_time_saved_seconds(value, 0, 0)


@pytest.mark.parametrize(
    ("reviewed", "replies", "dms"),
    [(2**63 // 30, 1, 0), (0, 2**63 // 60, 1), (0, 0, 2**63 // 60 + 1)],
)
def test_time_saved_rejects_signed_64_bit_overflow(
    reviewed: int, replies: int, dms: int
) -> None:
    with pytest.raises(ValueError):
        estimate_time_saved_seconds(reviewed, replies, dms)


@pytest.mark.parametrize(
    "now",
    [datetime(2026, 8, 10, 9), "2026-08-10T09:00:00Z"],
)
def test_previous_week_rejects_naive_or_non_datetime_now(now: object) -> None:
    with pytest.raises(ValueError):
        previous_week(now, "UTC")


@pytest.mark.parametrize("timezone_name", ["Not/AZone", "", True])
def test_previous_week_rejects_unknown_timezone(timezone_name: object) -> None:
    with pytest.raises(ValueError):
        previous_week(datetime(2026, 8, 10, 9, tzinfo=timezone.utc), timezone_name)


@pytest.mark.parametrize(
    "start, end",
    [(True, 2), (1, False), (-1, 2), (1, -2), (0, 2**63), (None, True)],
)
def test_trend_delta_rejects_invalid_snapshot_values(start: object, end: object) -> None:
    with pytest.raises(ValueError):
        trend_delta(start, end)


def test_reporting_period_rejects_non_monday_dates_and_non_utc_boundaries() -> None:
    with pytest.raises(ValueError):
        ReportingPeriod(
            timezone_name="UTC",
            week_start=date(2026, 8, 11),
            week_end=date(2026, 8, 17),
            start_utc=datetime(2026, 8, 11, tzinfo=timezone.utc),
            end_utc=datetime(2026, 8, 18, tzinfo=timezone.utc),
        )

    with pytest.raises(ValueError):
        ReportingPeriod(
            timezone_name="UTC",
            week_start=date(2026, 8, 10),
            week_end=date(2026, 8, 16),
            start_utc=datetime(2026, 8, 10),
            end_utc=datetime(2026, 8, 17, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    ("timezone_name", "week_start", "week_end", "start_utc", "end_utc"),
    [
        (
            "UTC",
            date(2026, 8, 10),
            date(2026, 8, 16),
            datetime(2026, 8, 17, tzinfo=timezone.utc),
            datetime(2026, 8, 24, tzinfo=timezone.utc),
        ),
        (
            "America/New_York",
            date(2026, 3, 2),
            date(2026, 3, 8),
            datetime(2026, 3, 2, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 9, 5, tzinfo=timezone.utc),
        ),
    ],
)
def test_reporting_period_rejects_utc_bounds_inconsistent_with_local_week(
    timezone_name: str,
    week_start: date,
    week_end: date,
    start_utc: datetime,
    end_utc: datetime,
) -> None:
    with pytest.raises(ValueError):
        ReportingPeriod(
            timezone_name=timezone_name,
            week_start=week_start,
            week_end=week_end,
            start_utc=start_utc,
            end_utc=end_utc,
        )


def test_account_and_manager_reports_are_immutable_aggregate_only_values() -> None:
    account = AccountWeeklyReport(
        username="managed_account",
        follower_start=100,
        follower_end=125,
        engagement_rate_bps_start=250,
        engagement_rate_bps_end=275,
        opportunities_reviewed=4,
        likes_sent=1,
        replies_sent=1,
        dms_sent=2,
        views_sent=3,
        skipped=1,
        gated=2,
        failed=3,
    )
    period = previous_week(datetime(2026, 8, 10, 9, tzinfo=timezone.utc), "UTC")
    report = ManagerWeeklyReport(
        period=period,
        accounts=(account,),
    )

    assert account.follower_delta == 25
    assert account.engagement_rate_bps_delta == 25
    assert account.estimated_time_saved_seconds == 300
    assert report.estimated_time_saved_seconds == 300
    with pytest.raises(FrozenInstanceError):
        account.username = "other"  # type: ignore[misc]

    forbidden = {
        "target_username",
        "generated_text",
        "provider_body",
        "credential",
        "proxy",
        "cookie",
        "prompt",
        "manager_name",
        "manager_email",
        "recipient_email",
    }
    assert forbidden.isdisjoint(field.name for field in fields(AccountWeeklyReport))
    assert forbidden.isdisjoint(field.name for field in fields(ManagerWeeklyReport))
    assert tuple(field.name for field in fields(ManagerWeeklyReport)) == (
        "period",
        "accounts",
    )


def test_report_value_reprs_redact_all_report_data() -> None:
    period = previous_week(
        datetime(2026, 8, 10, 9, tzinfo=timezone.utc),
        "America/Los_Angeles",
    )
    account = AccountWeeklyReport(
        username="private.managed.account",
        follower_start=10_001,
        follower_end=10_099,
        engagement_rate_bps_start=411,
        engagement_rate_bps_end=477,
        opportunities_reviewed=37,
        likes_sent=13,
        replies_sent=7,
        dms_sent=5,
        views_sent=29,
        skipped=17,
        gated=19,
        failed=23,
    )
    report = ManagerWeeklyReport(period=period, accounts=(account,))

    assert repr(period) == "ReportingPeriod(<redacted>)"
    assert repr(account) == "AccountWeeklyReport(<redacted>)"
    assert repr(report) == "ManagerWeeklyReport(<redacted>)"
    rendered = " ".join((repr(period), repr(account), repr(report)))
    for private_value in (
        "private.managed.account",
        "America/Los_Angeles",
        "2026-08-03",
        "2026-08-09",
        "10001",
        "10099",
        "411",
        "477",
        "37",
        "13",
        "7",
        "5",
        "29",
        "17",
        "19",
        "23",
        "recipient@example.test",
    ):
        assert private_value not in rendered


def test_account_report_preserves_missing_snapshot_trends_and_checks_time_saved() -> None:
    account = AccountWeeklyReport(
        username="managed_account",
        follower_start=None,
        follower_end=125,
        engagement_rate_bps_start=250,
        engagement_rate_bps_end=None,
        opportunities_reviewed=0,
        likes_sent=2,
        replies_sent=0,
        dms_sent=0,
        views_sent=3,
        skipped=0,
        gated=0,
        failed=0,
    )

    assert account.follower_delta is None
    assert account.engagement_rate_bps_delta is None
    assert account.estimated_time_saved_seconds == 0

    with pytest.raises(ValueError):
        AccountWeeklyReport(
            username="managed_account",
            follower_start=100,
            follower_end=125,
            engagement_rate_bps_start=250,
            engagement_rate_bps_end=275,
            opportunities_reviewed=True,
            likes_sent=0,
            replies_sent=1,
            dms_sent=2,
            views_sent=0,
            skipped=0,
            gated=0,
            failed=0,
        )
