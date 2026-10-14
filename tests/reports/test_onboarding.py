"""Onboarding emails: one per milestone, only after activation, sent once."""
from uuid import uuid4

import httpx
import pytest
from engagemate.db.text import text
from engagemate.reports.layout import email_layout
from engagemate.reports.onboarding import (
    OnboardingEmailDispatcher,
    enqueue_for_user,
    render_onboarding,
)

from tests.worker import test_canary_scripts as fixtures

database_url = fixtures.database_url
world = fixtures.world


def dispatcher(world, handler):
    return OnboardingEmailDispatcher(world['pool'], sender='EngageMate <hello@example.test>',
        api_key='test', http=httpx.Client(transport=httpx.MockTransport(handler)),
        unsubscribe_key='test-internal-key')


def activate(world, when="now()-interval '1 hour'"):
    with world['pool'].connection() as c:
        c.execute(f"UPDATE onboarding_email_activation SET started_at={when}")


def kinds(world):
    with world['pool'].connection() as c:
        return sorted((text(r['kind']), text(r['recipient'])) for r in
                      c.execute('SELECT kind, recipient FROM onboarding_emails').fetchall())


def drain(d):
    results = []
    while (result := d.run_once()) != 'idle':
        results.append(result)
    return results


def test_signup_and_connected_account_each_send_once(world):
    activate(world, "now()-interval '1 hour'")
    with world['pool'].connection() as c:
        # Users and accounts in the fixture were created just now, after activation.
        c.execute("UPDATE ig_accounts SET created_at=now() WHERE id=%s", (world['account_id'],))
    calls = []
    d = dispatcher(world, lambda req: calls.append(req) or httpx.Response(200, json={'id': f'm{len(calls)}'}))
    assert drain(d) == ['sent'] * 4  # two welcomes, two account_connected
    assert drain(d) == []
    subjects = sorted(req.read().decode().split('"subject":"')[1].split('"')[0] for req in calls)
    assert subjects.count('Welcome to EngageMate: connect your Instagram account') == 2
    assert '@canary.account is connected to EngageMate' in subjects


def test_nothing_before_activation_is_emailed(world):
    activate(world, "now()+interval '1 minute'")
    d = dispatcher(world, lambda req: pytest.fail('pre-activation signup must not send'))
    assert d.run_once() == 'idle'


def test_connect_reminder_waits_a_day_and_skips_connected_users(world):
    activate(world, "now()-interval '30 days'")
    new_user = uuid4()
    with world['pool'].connection() as c:
        c.execute("INSERT INTO users (id, owner_key, email, plan, created_at) VALUES (%s,%s,%s,'starter',now()-interval '25 hours')",
                  (new_user, f'clerk-{new_user}', 'late@example.test'))
        c.execute("INSERT INTO users (id, owner_key, email, plan, created_at) VALUES (%s,%s,%s,'starter',now()-interval '8 days')",
                  (uuid4(), f'clerk-old-{new_user}', 'stale@example.test'))
        c.execute("INSERT INTO users (id, owner_key, email, plan, created_at) VALUES (%s,%s,%s,'starter',now()-interval '2 hours')",
                  (uuid4(), f'clerk-fresh-{new_user}', 'fresh@example.test'))
        c.execute("UPDATE users SET created_at=now()-interval '26 hours' WHERE id IN (%s,%s)",
                  (world['user_id'], world['other_user_id']))
    drain(dispatcher(world, lambda req: httpx.Response(200, json={'id': 'm'})))
    reminders = [r for k, r in kinds(world) if k == 'connect_reminder']
    assert reminders == ['late@example.test']


def test_launch_sends_what_to_expect_with_the_accounts_schedule(world):
    activate(world, "now()-interval '1 hour'")
    with world['pool'].connection() as c:
        c.execute("UPDATE agent_configs SET launched_at=now(), schedule_hour=9, timezone='Asia/Makassar' WHERE ig_account_id=%s",
                  (world['account_id'],))
    bodies = []
    drain(dispatcher(world, lambda req: bodies.append(req.read().decode()) or httpx.Response(200, json={'id': 'm'})))
    expect = [b for b in bodies if 'what to expect' in b]
    assert len(expect) == 1
    assert '9:00 AM Asia/Makassar' in expect[0]


def test_operator_can_queue_a_welcome_for_an_earlier_signup_once(world):
    activate(world, "now()+interval '1 minute'")
    email = f"{world['user_id']}@example.test"
    assert enqueue_for_user(world['pool'], email.upper(), 'welcome') == 'queued'
    assert enqueue_for_user(world['pool'], email, 'welcome') == 'already_queued'
    assert enqueue_for_user(world['pool'], 'nobody@example.test', 'welcome') == 'no_such_user'
    calls = []
    d = dispatcher(world, lambda req: calls.append(req) or httpx.Response(200, json={'id': 'm'}))
    assert drain(d) == ['sent']
    with pytest.raises(ValueError):
        enqueue_for_user(world['pool'], email, 'what_to_expect')


def test_setup_emails_explain_hosted_browser_sign_in():
    for kind in ('welcome', 'connect_reminder'):
        email = render_onboarding(kind, {'name': 'Ana Farias'})
        assert email.text.startswith('Hi Ana,')
        for phrase in ('No browser extension or download is needed', 'Connect Instagram',
                       'verification', 'https://app.example.com/onboarding'):
            assert phrase in email.text
        assert '—' not in email.text and '**' not in email.text


def test_rendered_html_escapes_account_names():
    email = render_onboarding('account_connected', {'name': None, 'account': '<b>x</b>'})
    assert email.text.startswith('Hi there,')
    assert '&lt;b&gt;x&lt;/b&gt;' in email.html and '<b>x</b>' not in email.html


def test_layout_defaults_stay_the_activity_update():
    html = email_layout('T', '<p>b</p>')
    assert 'Open dashboard' in html and 'Your EngageMate activity update' in html
    custom = email_layout('T', '<p>b</p>', button_label='Continue setup',
                          button_url='https://app.example.com/onboarding', footer='Getting started')
    assert 'Continue setup' in custom and 'https://app.example.com/onboarding' in custom
    assert 'Your EngageMate activity update' not in custom


def test_every_onboarding_email_links_to_its_own_unsubscribe(world):
    activate(world, "now()-interval '1 hour'")
    with world['pool'].connection() as c:
        c.execute("UPDATE ig_accounts SET created_at=now() WHERE id=%s", (world['account_id'],))
    bodies = []
    drain(dispatcher(world, lambda req: bodies.append(req.read().decode()) or httpx.Response(200, json={'id': 'm'})))
    mine = [b for b in bodies if f"{world['user_id']}@example.test" in b]
    assert mine and all('/unsubscribe?u=' + str(world['user_id']) in b and 'c=onboarding' in b for b in mine)


def test_opted_out_users_get_no_onboarding_email(world):
    activate(world, "now()-interval '1 hour'")
    with world['pool'].connection() as c:
        c.execute("INSERT INTO email_preferences (user_id, onboarding_emails) VALUES (%s, false)", (world['user_id'],))
    calls = []
    drain(dispatcher(world, lambda req: calls.append(req.read().decode()) or httpx.Response(200, json={'id': 'm'})))
    assert calls and not any(f"{world['user_id']}@example.test" in body for body in calls)


def test_a_queued_email_is_suppressed_after_opt_out(world):
    activate(world, "now()-interval '1 hour'")
    d = dispatcher(world, lambda req: httpx.Response(200, json={'id': 'm'}))
    with world['pool'].connection() as c:
        d.enqueue(c)
        c.execute("INSERT INTO email_preferences (user_id, onboarding_emails) VALUES (%s, false), (%s, false)",
                  (world['user_id'], world['other_user_id']))
    assert set(drain(d)) == {'unsubscribed'}






def test_launched_email_describes_saved_choices_instead_of_claiming_replies_are_on():
    email = render_onboarding("what_to_expect", {"account":"example", "schedule_hour":9, "timezone":"America/Los_Angeles", "daily_like_limit":11, "daily_reply_limit":0, "follow_after_engagement":False, "follow_reel_message_enabled":True})
    assert "Story likes are on" in email.text
    assert "Story replies are off" in email.text
    assert "Reel DMs are off" in email.text
    assert "Keep the extension" not in email.text
