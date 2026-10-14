"""Daily activity summary: one email per user per day, from confirmed actions only."""
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest

from engagemate.db.text import text
from engagemate.reports import digest
from engagemate.reports.digest import ActivityDigestDispatcher, digest_windows, render_digest
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401

DAY = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
DUE = datetime(2026, 9, 10, 21, 30, tzinfo=UTC)
KEY = "test-internal-key"


@pytest.fixture(autouse=True)
def no_throttle(monkeypatch):
    monkeypatch.setattr(digest, "ENQUEUE_EVERY_SECONDS", 0)


def activate(world, when=datetime(2026, 9, 1, tzinfo=UTC)):
    with world['pool'].connection() as c:
        c.execute("UPDATE activity_digest_activation SET started_at=%s", (when,))
        c.execute("UPDATE agent_configs SET timezone='UTC'")


def seed(world, status='sent', action='reply', at=DAY, user='user_id', account='account_id', target='target_id'):
    with world['pool'].connection() as c:
        c.execute("INSERT INTO story_engagements (id,user_id,ig_account_id,target_id,story_fingerprint,action,status,executed_at,generated_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (uuid4(), world[user], world[account], world[target], uuid4().hex, action, status, at, 'Nice <script> & launch'))


def dispatcher(world, handler, now=DUE):
    return ActivityDigestDispatcher(world['pool'], sender='EngageMate <hello@example.test>',
        api_key='test', http=httpx.Client(transport=httpx.MockTransport(handler)),
        unsubscribe_key=KEY, now=lambda: now)


def drain(d):
    results = []
    while (result := d.run_once()) != 'idle':
        results.append(result)
    return results


def details(**changes):
    base = {"date": "2026-09-10", "timezone": "UTC",
            "window_start": "2026-09-09T20:00:00+00:00", "window_end": "2026-09-10T20:00:00+00:00",
            "total": 3, "counts": {"view": 1, "like": 1, "reply": 1, "dm": 0, "follow": 0, "unfollow": 0},
            "items": [
                {"action": "view", "account": "sender", "target": "one", "at": "2026-09-10T09:00:00+00:00", "message": None, "unfollow_due_at": None},
                {"action": "like", "account": "sender", "target": "two", "at": "2026-09-10T09:05:00+00:00", "message": None, "unfollow_due_at": None},
                {"action": "reply", "account": "sender", "target": "three", "at": "2026-09-10T09:10:00+00:00", "message": "<script>&hello", "unfollow_due_at": None},
            ]}
    return base | changes


def test_render_lists_totals_exact_messages_and_the_unsubscribe_link():
    link = "https://app.example.com/unsubscribe?u=x&c=activity&t=y"
    email = render_digest(details(), link)
    assert email.subject == "EngageMate daily summary: 3 actions on Sep 10"
    for phrase in ("Story views: 1", "Story likes: 1", "Story replies: 1",
                   "@sender replied to a Story from @three at 9:10 AM", "Message sent: <script>&hello",
                   f"Unsubscribe: {link}"):
        assert phrase in email.text
    assert "Direct messages" not in email.text
    assert "&lt;script&gt;&amp;hello" in email.html and "<script>" not in email.html
    assert "https://app.example.com/unsubscribe?u=x&amp;c=activity&amp;t=y" in email.html
    assert "Manage email settings" in email.html


def test_render_counts_actions_beyond_the_listed_ones():
    email = render_digest(details(total=140), "https://app.example.com/unsubscribe")
    assert "...and 137 more." in email.text


def test_one_email_per_day_covers_every_confirmed_action_once(world):
    activate(world)
    seed(world, action='like'); seed(world, action='reply', at=DAY + timedelta(hours=1))
    seed(world, status='failed', action='view')
    seed(world, action='like', user='other_user_id', account='other_account_id', target='other_target_id')
    bodies = []
    d = dispatcher(world, lambda req: bodies.append(req.read().decode()) or httpx.Response(200, json={'id': f'm{len(bodies)}'}))
    assert drain(d) == ['sent', 'sent']
    assert drain(d) == []
    mine = [b for b in bodies if str(world['user_id']) in b]
    assert len(mine) == 1
    assert 'Story likes: 1' in mine[0] and 'Story replies: 1' in mine[0] and 'Story views' not in mine[0]
    assert 'EngageMate daily summary: 2 actions on Sep 10' in mine[0]


def test_nothing_sends_before_the_day_ends_plus_grace(world):
    activate(world); seed(world)
    early = dispatcher(world, lambda req: pytest.fail('digest sent before it was due'),
                       now=datetime(2026, 9, 10, 20, 30, tzinfo=UTC))
    assert early.run_once() == 'idle'
    assert drain(dispatcher(world, lambda req: httpx.Response(200, json={'id': 'm'}))) == ['sent']


def test_days_without_activity_and_actions_before_activation_send_nothing(world):
    activate(world, when=datetime(2026, 9, 10, 12, tzinfo=UTC))
    seed(world)  # 10:00, before the activation
    assert dispatcher(world, lambda req: pytest.fail('nothing to summarize')).run_once() == 'idle'


def test_an_opted_out_user_gets_no_summary_even_if_already_queued(world):
    activate(world); seed(world)
    d = dispatcher(world, lambda req: pytest.fail('unsubscribed user emailed'))
    with world['pool'].connection() as c:
        d.enqueue(c)
        assert c.execute('SELECT count(*) n FROM activity_digest_emails').fetchone()['n'] == 1
        c.execute("INSERT INTO email_preferences (user_id, activity_emails) VALUES (%s, false)", (world['user_id'],))
    assert d.run_once() == 'unsubscribed'
    with world['pool'].connection() as c:
        row = c.execute('SELECT status, error_code FROM activity_digest_emails').fetchone()
        assert (text(row['status']), text(row['error_code'])) == ('failed', 'unsubscribed')
        c.execute('DELETE FROM activity_digest_emails')
    assert d.run_once() == 'idle'


def test_timeout_retry_preserves_payload_and_key(world):
    activate(world); seed(world)
    calls = []
    def send(req):
        calls.append(req)
        if len(calls) == 1:
            raise httpx.ReadTimeout('timeout')
        return httpx.Response(200, json={'id': 'msg-2'})
    d = dispatcher(world, send)
    assert d.run_once() == 'pending'
    with world['pool'].connection() as c:
        c.execute("UPDATE activity_digest_emails SET next_attempt_at=now()")
        c.execute("UPDATE users SET email='changed@example.test' WHERE id=%s", (world['user_id'],))
    d.sender = 'different@example.test'
    assert d.run_once() == 'sent'
    assert calls[0].content == calls[1].content
    assert calls[0].headers['Idempotency-Key'] == calls[1].headers['Idempotency-Key']


def test_expired_retry_never_resends(world):
    activate(world); seed(world)
    d = dispatcher(world, lambda req: httpx.Response(503))
    assert d.run_once() == 'pending'
    with world['pool'].connection() as c:
        c.execute("UPDATE activity_digest_emails SET first_attempt_at=now()-interval '24 hours',next_attempt_at=now()")
    assert d.run_once() == 'idle'
    with world['pool'].connection() as c:
        assert text(c.execute('SELECT status FROM activity_digest_emails').fetchone()['status']) == 'uncertain'


def test_windows_follow_the_account_timezone():
    zone = ZoneInfo("Asia/Makassar")  # UTC+8, so 20:00 local is 12:00 UTC
    now = datetime(2026, 9, 10, 13, 30, tzinfo=UTC)
    windows = digest_windows(now, zone, datetime(2026, 9, 9, 0, tzinfo=UTC))
    assert [(day.isoformat(), start.astimezone(UTC).hour, end.astimezone(UTC).hour) for day, start, end in windows] == [
        ("2026-09-09", 0, 12), ("2026-09-10", 12, 12)]


def test_composio_unknown_submission_is_not_replayed(world):
    from engagemate.reports.composio import ComposioEmailClient
    from engagemate.reports.resend import ResendError
    from engagemate.reports.render import RenderedEmail
    calls = []
    def timeout(req):
        calls.append(req)
        raise httpx.ReadTimeout('unknown')
    client = ComposioEmailClient(pool=world['pool'], http=httpx.Client(transport=httpx.MockTransport(timeout)), api_key='test', account_id='account-test', sender='hello@example.test')
    for _ in range(2):
        with pytest.raises(ResendError, match='provider_outcome_unknown'):
            client.send('owner@example.test', RenderedEmail('subject', 'text', '<p>text</p>'), 'test-key')
    assert len(calls) == 1


def test_composio_nested_receipt_replays_without_resending(world):
    from engagemate.reports.composio import ComposioEmailClient
    from engagemate.reports.render import RenderedEmail
    calls = []
    client = ComposioEmailClient(pool=world['pool'], http=httpx.Client(transport=httpx.MockTransport(
        lambda req: calls.append(req) or httpx.Response(200, json={'successful': True, 'data': {'response_data': {'id': 'gmail-message'}}}))),
        api_key='test', account_id='account-test', sender='hello@example.test')
    for _ in range(2):
        assert client.send('owner@example.test', RenderedEmail('subject', 'text', '<p>text</p>'), 'success-key') == 'gmail-message'
    assert len(calls) == 1


@pytest.mark.parametrize('outcome,expected', [('confirmed', 1), ('already_absent', 0)])
def test_unfollows_count_only_when_confirmed(world, outcome, expected):
    from psycopg.types.json import Jsonb
    activate(world); seed(world, action='like')
    with world['pool'].connection() as c:
        e = c.execute('SELECT id FROM story_engagements LIMIT 1').fetchone()['id']
        c.execute("""INSERT INTO relationship_follows
            (user_id,ig_account_id,target_id,owner_id,prerequisite_engagement_id,status,
             confirmed_follow_at,unfollow_due_at,unfollowed_at,evidence)
            VALUES (%s,%s,%s,'123456',%s,'unfollowed',%s,%s,%s,%s)""",
            (world['user_id'], world['account_id'], world['target_id'], e, DAY - timedelta(days=5),
             DAY, DAY, Jsonb({'unfollow': {'outcome': outcome}})))
    bodies = []
    drain(dispatcher(world, lambda req: bodies.append(req.read().decode()) or httpx.Response(200, json={'id': 'm'})))
    mine = [b for b in bodies if str(world['user_id']) in b]
    assert ('Unfollows: 1' in mine[0]) is bool(expected)
