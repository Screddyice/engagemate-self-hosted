"""Pure calculations shared by weekly report queries and rendering."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SIGNED_64_BIT_MAX = 2**63 - 1


def previous_week(now: datetime, timezone_name: str) -> ReportingPeriod:
    """Return the completed local Monday-through-Sunday reporting period."""

    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    try:
        if now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("now must be a timezone-aware datetime") from exc

    if type(timezone_name) is not str or not timezone_name:
        raise ValueError("timezone_name must be an IANA timezone")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
        raise ValueError("timezone_name must be an IANA timezone") from exc

    local_today = now.astimezone(zone).date()
    week_start = local_today - timedelta(days=local_today.weekday() + 7)
    week_end = week_start + timedelta(days=6)
    local_start = datetime.combine(week_start, time.min, tzinfo=zone)
    local_end = datetime.combine(week_start + timedelta(days=7), time.min, tzinfo=zone)

    from engagemate.reports.models import ReportingPeriod

    return ReportingPeriod(
        timezone_name=timezone_name,
        week_start=week_start,
        week_end=week_end,
        start_utc=local_start.astimezone(timezone.utc),
        end_utc=local_end.astimezone(timezone.utc),
    )


def estimate_time_saved_seconds(
    opportunities_reviewed: int, replies_sent: int, dms_sent: int
) -> int:
    """Estimate manual review and composition time using the published method."""

    reviewed = _bounded_count(opportunities_reviewed, "opportunities_reviewed")
    replies = _bounded_count(replies_sent, "replies_sent")
    dms = _bounded_count(dms_sent, "dms_sent")
    seconds = reviewed * 30 + (replies + dms) * 60
    if seconds > _SIGNED_64_BIT_MAX:
        raise ValueError("estimated time exceeds signed 64-bit range")
    return seconds


def trend_delta(start: int | None, end: int | None) -> int | None:
    """Return a signed snapshot delta without inventing missing observations."""

    if start is not None:
        _bounded_count(start, "start")
    if end is not None:
        _bounded_count(end, "end")
    if start is None or end is None:
        return None
    return end - start


def _bounded_count(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= _SIGNED_64_BIT_MAX:
        raise ValueError(f"{name} must be a signed 64-bit nonnegative integer")
    return value
