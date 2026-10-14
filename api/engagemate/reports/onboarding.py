"""Onboarding emails: welcome, connect reminder, account connected, what to expect.

Each email is one row in `onboarding_emails`, keyed by (kind, source_id), and
goes through the same lease-and-send outbox as the daily activity summary. The activation
boundary keeps a deployment from emailing people who signed up before it; an
operator can still queue a welcome for one of them with the `enqueue` command.
"""
from __future__ import annotations

import argparse
import os
from html import escape
from urllib.parse import urlsplit

from engagemate.db.text import text as dbtext
from engagemate.domain.budgets import BudgetPolicy
from engagemate.reports.actions import OutboxDispatcher
from engagemate.reports.layout import email_layout
from engagemate.reports.render import RenderedEmail
from engagemate.reports.unsubscribe import text_footer
from engagemate.web_origin import web_url

ONBOARDING_PATH = "/onboarding"
DASHBOARD_PATH = "/dashboard"
CONNECT_REMINDER_AFTER = "24 hours"
CONNECT_REMINDER_UNTIL = "7 days"

ENQUEUE = f"""
WITH activation AS (SELECT started_at FROM onboarding_email_activation),
events AS (
 SELECT u.id user_id, 'welcome' kind, u.id source_id, u.email recipient,
        jsonb_build_object('name', u.name) details
 FROM users u, activation WHERE u.created_at >= activation.started_at
 UNION ALL
 SELECT u.id, 'connect_reminder', u.id, u.email, jsonb_build_object('name', u.name)
 FROM users u, activation
 WHERE u.created_at >= activation.started_at
   AND u.created_at <= now() - interval '{CONNECT_REMINDER_AFTER}'
   AND u.created_at > now() - interval '{CONNECT_REMINDER_UNTIL}'
   AND NOT EXISTS (SELECT 1 FROM ig_accounts a WHERE a.user_id = u.id)
 UNION ALL
 SELECT u.id, 'account_connected', a.id, u.email,
        jsonb_build_object('name', u.name, 'account', a.username)
 FROM ig_accounts a JOIN users u ON u.id = a.user_id, activation
 WHERE a.created_at >= activation.started_at
 UNION ALL
 SELECT u.id, 'what_to_expect', c.id, u.email,
        jsonb_build_object('name', u.name, 'account', a.username, 'plan', u.plan,
          'timezone', coalesce(c.timezone, 'UTC'), 'schedule_hour', c.schedule_hour,
          'daily_like_limit', c.daily_like_limit, 'daily_reply_limit', c.daily_reply_limit,
          'daily_dm_limit', c.daily_dm_limit, 'daily_view_limit', c.daily_view_limit,
          'daily_reel_message_limit', c.daily_reel_message_limit,
          'follow_reel_message_enabled', c.follow_reel_message_enabled,
          'follow_after_engagement', c.follow_after_engagement,
          'daily_follow_limit', c.daily_follow_limit,
          'auto_unfollow_enabled', c.auto_unfollow_enabled)
 FROM agent_configs c
 JOIN ig_accounts a ON a.id = c.ig_account_id AND a.user_id = c.user_id
 JOIN users u ON u.id = c.user_id, activation
 WHERE c.enabled AND c.launched_at >= activation.started_at
)
INSERT INTO onboarding_emails (user_id, kind, source_id, recipient, details)
SELECT e.user_id, e.kind, e.source_id, e.recipient, e.details FROM events e
WHERE e.recipient IS NOT NULL AND e.recipient <> ''
  AND NOT EXISTS (SELECT 1 FROM onboarding_emails n WHERE n.kind = e.kind AND n.source_id = e.source_id)
  AND NOT EXISTS (SELECT 1 FROM email_preferences p WHERE p.user_id = e.user_id AND NOT p.onboarding_emails)
LIMIT 100
ON CONFLICT (kind, source_id) DO NOTHING
"""

# Shared by the welcome and reminder emails, so the setup steps cannot drift.
BROWSER_NOTE = (
    "Open EngageMate in your browser to connect Instagram. No browser extension or download is needed."
)
SETUP_STEPS = (
    "Open {onboarding_url} and sign in to EngageMate.",
    "On the Accounts step, choose Connect Instagram and enter your Instagram username.",
    "Enter your Instagram password in the sign-in form and complete any verification shown there.",
    "After the connection is confirmed, finish Brand and Voice. On Launch, review and save your behavior choices before launching.",
)
PASSWORD_NOTE = (
    "EngageMate passes your sign-in details to Instagram through a private temporary browser. "
    "Your connected session is stored encrypted. Do not reply to this email with a password or security code."
)


BEHAVIOR_NOTE = (
    "On the Launch step, choose what EngageMate does. Likes are the only behavior on by default. "
    "You can turn Reply to Stories, Follow after engagement, and Send a reel after following "
    "on or off, then choose Save behavior choices. Story replies require a confirmed new follow. "
    "Reel DMs also require following to be on; "
    "you can set their daily target and edit the message. Extra view-only Story actions and "
    "automatic unfollows start off too. Likes still require opening a Story. "
    "You can change these choices later in Settings."
)


def _greeting(details: dict) -> str:
    name = str(details.get("name") or "").strip().split()
    return f"Hi {name[0]}," if name else "Hi there,"


def _steps_text() -> str:
    onboarding_url = web_url(ONBOARDING_PATH)
    return "\n".join(
        f"{number}. {step.format(onboarding_url=onboarding_url)}"
        for number, step in enumerate(SETUP_STEPS, 1)
    )


def _html(paragraphs: list[str]) -> str:
    """Plain paragraphs, escaped; numbered step blocks keep their line breaks."""
    return "".join(
        f'<p style="margin:0 0 14px;white-space:pre-wrap;overflow-wrap:anywhere;">{escape(p)}</p>'
        for p in paragraphs
    )


DESCRIPTION = "EngageMate setup emails"


def _email(subject: str, title: str, paragraphs: list[str], *, button: str, url: str,
           unsubscribe: str | None = None) -> RenderedEmail:
    text = "\n\n".join(paragraphs)
    if unsubscribe:
        text += text_footer(unsubscribe, DESCRIPTION)
    html = email_layout(title, _html(paragraphs), button_label=button, button_url=url,
                        footer="Getting started with EngageMate",
                        unsubscribe_url=unsubscribe, unsubscribe_description=DESCRIPTION)
    return RenderedEmail(subject=subject, text=text, html=html)


def _hour(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{(hour % 12) or 12}:00 {suffix}"


def render_onboarding(kind: str, details: dict, unsubscribe: str | None = None) -> RenderedEmail:
    if kind == "invite":
        raise ValueError("Invitations are unavailable in owner-run installations")
    greeting = _greeting(details)
    if kind == "welcome":
        return _email(
            "Welcome to EngageMate: connect your Instagram account",
            "Welcome to EngageMate",
            [
                greeting,
                ("Thanks for signing up. Your next step is connecting the Instagram account "
                "you want EngageMate to engage from, which takes about five minutes."),
                BROWSER_NOTE,
                "How to connect Instagram:\n" + _steps_text(),
                PASSWORD_NOTE,
                BEHAVIOR_NOTE,
                ("After that, the setup walks you through your brand, your voice, and "
                "launch. Questions or stuck on a step? Reply to this email."),
                "The EngageMate team",
            ],
            button="Continue setup", url=web_url(ONBOARDING_PATH), unsubscribe=unsubscribe,
        )
    if kind == "connect_reminder":
        return _email(
            "Finish connecting your Instagram account to EngageMate",
            "Connect your Instagram account",
            [
                greeting,
                ("Your EngageMate account is ready, but no Instagram account is connected yet. "
                "EngageMate cannot start until one is."),
                BROWSER_NOTE,
                "The steps again:\n" + _steps_text(),
                ("If Instagram asks for a code or security check, follow the instructions in the sign-in form. "
                "The page tells you which verification method Instagram is requesting."),
                BEHAVIOR_NOTE,
                "Reply to this email if anything gets in the way.",
                "The EngageMate team",
            ],
            button="Connect Instagram", url=web_url(ONBOARDING_PATH), unsubscribe=unsubscribe,
        )
    account = str(details["account"])
    if kind == "account_connected":
        return _email(
            f"@{account} is connected to EngageMate",
            "Instagram connected",
            [
                greeting,
                f"@{account} is connected. Nice work.",
                ("Next, finish the remaining setup steps: your brand, your voice, and launch. "
                "EngageMate does nothing on Instagram until you launch."),
                BEHAVIOR_NOTE,
                ("If Instagram signs you out or asks for a security check, EngageMate pauses "
                "and asks you to reconnect from Accounts."),
                "The EngageMate team",
            ],
            button="Finish setup", url=web_url(ONBOARDING_PATH), unsubscribe=unsubscribe,
        )
    if kind == "what_to_expect":
        plan = str(details.get("plan") or "starter")
        engagements = BudgetPolicy._PLAN_TOTALS.get(plan)
        views = BudgetPolicy._PLAN_VIEW_TOTALS.get(plan)
        _, (likes, _replies, _dms, first_views, _private) = BudgetPolicy._WARMUP_BRACKETS[0]
        first_days = BudgetPolicy._WARMUP_BRACKETS[0][0]
        schedule = f"{_hour(int(details['schedule_hour']))} {details['timezone']}"
        ramp = (
            f"EngageMate warms the account up slowly. For the first {first_days} days it "
            f"stays under {likes} likes and {first_views} Story views a day, with no replies. "
            "It ramps up over the first month"
        )
        if engagements and views:
            ramp += (f" until your {plan.capitalize()} plan's daily limit applies: "
                     f"{engagements} engagements and {views} Story views")
        ramp += ". It never goes above the limits you set in Settings."
        follows = (
            f"It follows up to {details['daily_follow_limit']} eligible people a day before Story replies and after Likes"
            + (", and unfollows them automatically after five days." if details.get("auto_unfollow_enabled") else ".")
            if details.get("follow_after_engagement") else
            "Following is off. You can turn it on in Settings."
        )
        return _email(
            f"EngageMate is live for @{account}: what to expect",
            "What to expect",
            [
                greeting,
                f"EngageMate is live for @{account}. Here is how it works from here.",
                (f"When it runs: once a day, starting around {schedule}. Each session finds "
                "Stories from the audience you approved and performs only the behaviors you enabled."),
                ramp,
                (f"Story likes are {'on' if details.get('daily_like_limit', 0) > 0 else 'off'}. "
                f"Story replies are {'on' if details.get('daily_reply_limit', 0) > 0 else 'off'}. "
                f"Reel DMs are {'on' if details.get('follow_reel_message_enabled') and details.get('follow_after_engagement') else 'off'}. "
                f"{follows} If Instagram shows a warning, a challenge, or a rate limit, "
                "EngageMate pauses the account instead of pushing through."),
                ("What you will get: one email each evening summarizing that day's confirmed "
                "actions, and a weekly report every Monday at 9:00 AM your time. You can turn "
                "either off from the link at the bottom of the email or in Settings."),
                ("What you control: pause EngageMate, change limits, adjust your audience, or "
                f"edit your voice anytime at {web_url(DASHBOARD_PATH)}."),
                ("If your account needs reconnecting, open Accounts and choose Connect Instagram. "
                "Reply to this email with any questions."),
                "The EngageMate team",
            ],
            button="Open dashboard", url=web_url(DASHBOARD_PATH), unsubscribe=unsubscribe,
        )
    raise ValueError("unknown onboarding email kind")


class OnboardingEmailDispatcher(OutboxDispatcher):
    TABLE = "onboarding_emails"
    ENQUEUE = ENQUEUE
    KEY_PREFIX = "engagemate-onboarding"
    PREFERENCE = "onboarding_emails"

    def render(self, row) -> RenderedEmail:
        return render_onboarding(dbtext(row["kind"]), row["details"], self.unsubscribe_link(row, "onboarding"))


def enqueue_for_user(pool, email: str, kind: str) -> str:
    """Queue a welcome or reminder for one existing user, ignoring the activation date.

    Returns 'queued', 'already_queued', or 'no_such_user'. The outbox key still
    applies, so running this twice never sends twice.
    """
    if kind not in {"welcome", "connect_reminder"}:
        raise ValueError("only welcome and connect_reminder can be queued by hand")
    with pool.connection() as c:
        user = c.execute("SELECT id, email, name FROM users WHERE lower(email) = lower(%s)",
                         (email,)).fetchone()
        if user is None:
            return "no_such_user"
        from psycopg.types.json import Jsonb

        inserted = c.execute(
            """INSERT INTO onboarding_emails (user_id, kind, source_id, recipient, details)
               VALUES (%s, %s, %s, %s, %s) ON CONFLICT (kind, source_id) DO NOTHING RETURNING id""",
            (user["id"], kind, user["id"], dbtext(user["email"]),
             Jsonb({"name": dbtext(user["name"]) if user["name"] is not None else None})),
        ).fetchone()
        return "queued" if inserted else "already_queued"


def main(argv: list[str] | None = None) -> int:
    from engagemate.config import Settings
    from engagemate.db.pool import create_pool

    parser = argparse.ArgumentParser(description="Queue an onboarding email for one user.")
    sub = parser.add_subparsers(dest="command", required=True)
    queue = sub.add_parser("enqueue")
    queue.add_argument("--email", required=True)
    queue.add_argument("--kind", choices=("welcome", "connect_reminder"), default="welcome")
    arguments = parser.parse_args(argv)
    with create_pool(Settings().database_url, min_size=1, max_size=1) as pool:
        print(enqueue_for_user(pool, arguments.email, arguments.kind))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
