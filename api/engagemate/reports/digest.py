"""One daily email per user summarizing the Instagram actions EngageMate confirmed.

Until 2026-09-17 every confirmed view, like, reply, DM, follow, and unfollow sent
its own email, so one scheduled run could land a dozen messages in a pilot
user's inbox. Pilot feedback asked for one summary a day instead.

A digest covers the 24 hours that end at DIGEST_HOUR in the timezone of the
user's first account, and is queued an hour after that so actions still being
recorded at the boundary make it in. Days with no confirmed action send
nothing. Each (user, local date) is one outbox row, so a restart or a late
poll never sends a day twice. A missed day is still queued for LOOKBACK_DAYS.
"""
from __future__ import annotations

import time as clock
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.json import Jsonb

from engagemate.db.text import text as dbtext
from engagemate.reports.actions import OutboxDispatcher
from engagemate.reports.layout import email_layout
from engagemate.reports.render import RenderedEmail
from engagemate.reports.unsubscribe import text_footer, unsubscribe_url

DIGEST_HOUR = 20
GRACE = timedelta(hours=1)
LOOKBACK_DAYS = 3
MAX_LISTED = 100
ENQUEUE_EVERY_SECONDS = 300
DESCRIPTION = "a daily summary of your EngageMate activity"

CANDIDATES = """
SELECT u.id user_id, u.email recipient, activation.started_at activated_at,
 coalesce((SELECT c.timezone FROM ig_accounts a
           JOIN agent_configs c ON c.ig_account_id = a.id AND c.user_id = a.user_id
           WHERE a.user_id = u.id ORDER BY a.created_at LIMIT 1), 'UTC') timezone
FROM users u, activity_digest_activation activation
WHERE u.email IS NOT NULL AND u.email <> ''
  AND EXISTS (SELECT 1 FROM ig_accounts a WHERE a.user_id = u.id)
  AND coalesce((SELECT p.activity_emails FROM email_preferences p WHERE p.user_id = u.id), true)
"""

EVENTS = """
SELECT e.action, e.happened_at, e.message, e.due_at, a.username account, t.username target
FROM (
 SELECT s.user_id, s.ig_account_id, s.target_id, s.action, s.executed_at happened_at,
        s.generated_text message, NULL::timestamptz due_at
 FROM story_engagements s WHERE s.status = 'sent'
 UNION ALL
 SELECT f.user_id, f.ig_account_id, f.target_id, 'follow', f.confirmed_follow_at, NULL, f.unfollow_due_at
 FROM relationship_follows f
 WHERE f.confirmed_follow_at IS NOT NULL AND f.evidence #>> '{follow,outcome}' = 'confirmed'
 UNION ALL
 SELECT f.user_id, f.ig_account_id, f.target_id, 'unfollow', f.unfollowed_at, NULL, NULL
 FROM relationship_follows f
 WHERE f.unfollowed_at IS NOT NULL AND f.evidence #>> '{unfollow,outcome}' = 'confirmed'
) e
JOIN ig_accounts a ON a.id = e.ig_account_id AND a.user_id = e.user_id
JOIN targets t ON t.id = e.target_id AND t.ig_account_id = a.id AND t.user_id = e.user_id
WHERE e.user_id = %(user_id)s AND e.happened_at >= %(start)s AND e.happened_at < %(end)s
ORDER BY e.happened_at
"""

ACTIONS = ("view", "like", "reply", "dm", "follow", "unfollow")
TOTAL_LABELS = {"view": "Story views", "like": "Story likes", "reply": "Story replies",
                "dm": "Direct messages", "follow": "Follows", "unfollow": "Unfollows"}
VERBS = {"view": "viewed a Story from", "like": "liked a Story from",
         "reply": "replied to a Story from", "dm": "sent a direct message to",
         "follow": "followed", "unfollow": "unfollowed"}


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def digest_windows(now: datetime, zone: ZoneInfo, activated_at: datetime) -> list[tuple[date, datetime, datetime]]:
    """(local date, start, end) for each recent day whose digest is due."""
    today = now.astimezone(zone).date()
    windows = []
    for back in range(LOOKBACK_DAYS, -1, -1):
        day = today - timedelta(days=back)
        end = datetime.combine(day, time(DIGEST_HOUR), zone)
        start = max(end - timedelta(days=1), activated_at)
        if end + GRACE <= now and start < end:
            windows.append((day, start, end))
    return windows


def build_details(events: list[dict], day: date, zone: ZoneInfo, start: datetime, end: datetime) -> dict:
    counts = {action: 0 for action in ACTIONS}
    items = []
    for event in events:
        action = dbtext(event["action"])
        if action not in counts:
            continue
        counts[action] += 1
        if len(items) < MAX_LISTED:
            items.append({
                "action": action, "account": dbtext(event["account"]), "target": dbtext(event["target"]),
                "at": event["happened_at"].isoformat(),
                "message": dbtext(event["message"]) if event["message"] is not None else None,
                "unfollow_due_at": event["due_at"].isoformat() if event["due_at"] else None,
            })
    return {"date": day.isoformat(), "timezone": zone.key, "window_start": start.isoformat(),
            "window_end": end.isoformat(), "total": sum(counts.values()), "counts": counts, "items": items}


def _clock(value: str, zone: ZoneInfo) -> str:
    moment = datetime.fromisoformat(value).astimezone(zone)
    return moment.strftime("%-I:%M %p")


def _lines(details: dict) -> tuple[list[str], list[tuple[str, str | None]]]:
    zone = _zone(details["timezone"])
    totals = [f"{TOTAL_LABELS[action]}: {count}" for action, count in details["counts"].items() if count]
    activity = []
    for item in details["items"]:
        line = f"@{item['account']} {VERBS[item['action']]} @{item['target']} at {_clock(item['at'], zone)}"
        extra = None
        if item["action"] in {"reply", "dm"} and item.get("message"):
            extra = f"Message sent: {item['message']}"
        elif item["action"] == "follow" and item.get("unfollow_due_at"):
            due = datetime.fromisoformat(item["unfollow_due_at"]).astimezone(zone)
            extra = f"Automatic unfollow scheduled for {due:%b %-d, %-I:%M %p} if auto-unfollow stays on."
        activity.append((line, extra))
    return totals, activity


def render_digest(details: dict, unsubscribe: str) -> RenderedEmail:
    zone = _zone(details["timezone"])
    day = date.fromisoformat(details["date"])
    total = details["total"]
    noun = "action" if total == 1 else "actions"
    start = datetime.fromisoformat(details["window_start"]).astimezone(zone)
    end = datetime.fromisoformat(details["window_end"]).astimezone(zone)
    span = f"{start:%b %-d, %-I:%M %p} to {end:%b %-d, %-I:%M %p} ({zone.key})"
    totals, activity = _lines(details)
    hidden = total - len(details["items"])

    text_parts = [f"EngageMate confirmed {total} {noun} on your Instagram from {span}.",
                  "Totals\n" + "\n".join(totals)]
    listing = "\n".join(line + (f"\n  {extra}" if extra else "") for line, extra in activity)
    if hidden > 0:
        listing += f"\n...and {hidden} more. See every action on your dashboard."
    text_parts.append("Activity\n" + listing)
    text = "\n\n".join(text_parts) + text_footer(unsubscribe, DESCRIPTION)

    rows = "".join(
        f'<li style="margin:0 0 10px;">{escape(line)}'
        + (f'<br><span style="color:#93c5fd;white-space:pre-wrap;overflow-wrap:anywhere;">{escape(extra)}</span>' if extra else "")
        + "</li>"
        for line, extra in activity
    )
    more = (f'<p style="color:#93c5fd;">...and {hidden} more. See every action on your dashboard.</p>'
            if hidden > 0 else "")
    body = (
        f"<p>EngageMate confirmed {total} {noun} on your Instagram from {escape(span)}.</p>"
        '<h2 style="font-size:18px;color:#eff6ff;">Totals</h2>'
        f'<p>{"<br>".join(escape(line) for line in totals)}</p>'
        '<h2 style="font-size:18px;color:#eff6ff;">Activity</h2>'
        f'<ul style="padding-left:18px;margin:0 0 20px;">{rows}</ul>{more}'
    )
    html = email_layout(f"Your daily summary for {day:%B %-d}", body,
                        footer="Your EngageMate daily summary",
                        unsubscribe_url=unsubscribe, unsubscribe_description=DESCRIPTION)
    return RenderedEmail(subject=f"EngageMate daily summary: {total} {noun} on {day:%b %-d}",
                         text=text, html=html)


class ActivityDigestDispatcher(OutboxDispatcher):
    TABLE = "activity_digest_emails"
    KEY_PREFIX = "engagemate-digest"
    PREFERENCE = "activity_emails"

    def __init__(self, *args, now=lambda: datetime.now(UTC), **kwargs):
        super().__init__(*args, **kwargs)
        self.now = now
        self._last_enqueue = None

    def enqueue(self, c) -> None:
        # Empty days leave no row, so polling every 30 seconds would re-read
        # four days of activity per user. Recheck every few minutes instead.
        moment = clock.monotonic()
        if self._last_enqueue is not None and moment - self._last_enqueue < ENQUEUE_EVERY_SECONDS:
            return
        self._last_enqueue = moment
        now = self.now()
        for user in c.execute(CANDIDATES).fetchall():
            zone = _zone(dbtext(user["timezone"]))
            for day, start, end in digest_windows(now, zone, user["activated_at"]):
                exists = c.execute(f"SELECT 1 FROM {self.TABLE} WHERE user_id = %s AND digest_date = %s",
                                   (user["user_id"], day)).fetchone()
                if exists:
                    continue
                events = c.execute(EVENTS, {"user_id": user["user_id"], "start": start, "end": end}).fetchall()
                if not events:
                    continue
                c.execute(
                    f"""INSERT INTO {self.TABLE} (user_id, digest_date, window_start, window_end, recipient, details)
                        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, digest_date) DO NOTHING""",
                    (user["user_id"], day, start, end, dbtext(user["recipient"]),
                     Jsonb(build_details(events, day, zone, start, end))))

    def render(self, row) -> RenderedEmail:
        return render_digest(row["details"], self.unsubscribe_link(row, "activity"))
