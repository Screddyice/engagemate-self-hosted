from datetime import UTC, date, datetime

import pytest

from engagemate.reports.periodic import Period, due_period, display_delta, render, PeriodicDispatcher
from engagemate.reports.resend import ResendError
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401


def test_fortnight_is_fourteen_days_not_twice_a_month():
    activation=datetime(2026,9,15,14,tzinfo=UTC)
    assert due_period('fortnightly',datetime(2026,9,29,15,59,tzinfo=UTC),activation,'America/Los_Angeles') is None
    p=due_period('fortnightly',datetime(2026,9,29,16,tzinfo=UTC),activation,'America/Los_Angeles')
    assert (p.start,p.end)==(date(2026,9,15),date(2026,9,29))
    assert due_period('fortnightly',datetime(2026,9,16,tzinfo=UTC),activation,'America/Los_Angeles') is None


def test_monthly_leap_year_and_pacific_dst():
    p=due_period('monthly',datetime(2028,3,1,17,tzinfo=UTC),datetime(2028,2,15,tzinfo=UTC),'America/Los_Angeles')
    assert (p.start,p.end)==(date(2028,2,1),date(2028,3,1))
    assert p.bounds[0].hour==8
    assert due_period('monthly',datetime(2028,3,1,16,59,tzinfo=UTC),datetime(2028,2,15,tzinfo=UTC),'America/Los_Angeles') is None
    assert due_period('monthly',datetime(2028,3,15,tzinfo=UTC),datetime(2028,3,5,tzinfo=UTC),'America/Los_Angeles') is None


def test_rates_are_percentage_points_and_missing_is_not_zero():
    assert '+0.50 percentage points' in display_delta(200,250,rate=True)
    assert 'Not available' in display_delta(None,250)
    assert '-10' in display_delta(100,90)


def account():
    return dict(username='<script>',counts={'like':4},relationships={'follows':3,'unfollows':1,'eligible':3,'follow_backs':1},snapshots=[None,None])


def test_outcomes_do_not_invent_responses_or_claim_complete_attribution():
    result=render(Period('fortnightly',date(2026,9,1),date(2026,9,15),'America/Los_Angeles'),[account()])
    assert 'Confirmed EngageMate actions: 8' in result.text
    assert 'Incoming responses: not available' in result.text
    assert 'coverage: partial' in result.text
    assert '<script>' not in result.html
    assert '&lt;script&gt;' in result.html


def test_missing_monthly_snapshots_are_explicit():
    result=render(Period('monthly',date(2026,9,1),date(2026,10,1),'America/Los_Angeles'),[account()])
    assert result.text.count('Not available:')==2


class Client:
    def __init__(self, fail=False):
        self.calls=[]; self.fail=fail
    def send(self, recipient, rendered, key):
        self.calls.append((recipient,rendered,key))
        if self.fail:
            raise ResendError('provider_outcome_unknown',retryable=False)
        return 'message-123'


def ready(world):
    with world['pool'].connection() as c:
        c.execute("INSERT INTO periodic_report_preferences(user_id,fortnightly_enabled,enabled_at) VALUES (%s,true,'2026-09-01T12:00:00Z') ON CONFLICT(user_id) DO UPDATE SET enabled_at=excluded.enabled_at",(world['user_id'],))


def test_owner_isolation_period_deduplication_and_immutable_recipient(world):
    ready(world); client=Client(); d=PeriodicDispatcher(world['pool'],client)
    now=datetime(2026,9,15,17,tzinfo=UTC)
    d.enqueue(now); d.enqueue(now)
    with world['pool'].connection() as c:
        rows=c.execute('SELECT user_id,recipient FROM periodic_report_deliveries').fetchall()
        assert len(rows)==1 and rows[0]['user_id']==world['user_id']
        recipient=rows[0]['recipient']
        c.execute("UPDATE users SET email='changed@example.test' WHERE id=%s",(world['user_id'],))
    assert d.send_one()=='sent'
    assert d.send_one()=='idle'
    from engagemate.db.text import text
    assert client.calls[0][0]==text(recipient)


def test_uncertain_delivery_is_not_retried(world):
    ready(world); client=Client(True); d=PeriodicDispatcher(world['pool'],client)
    d.enqueue(datetime(2026,9,15,17,tzinfo=UTC))
    assert d.send_one()=='uncertain'
    assert d.send_one()=='idle'
    assert len(client.calls)==1


def test_stale_sending_delivery_is_held_for_reconciliation(world):
    ready(world); client=Client(); d=PeriodicDispatcher(world['pool'],client)
    d.enqueue(datetime(2026,9,15,17,tzinfo=UTC))
    with world['pool'].connection() as c:
        c.execute("UPDATE periodic_report_deliveries SET status='sending',attempted_at=now()-interval '3 minutes'")
    assert d.send_one()=='idle'
    assert client.calls==[]
    with world['pool'].connection() as c:
        row=c.execute('SELECT status,error_code FROM periodic_report_deliveries').fetchone()
    from engagemate.db.text import text
    assert (text(row['status']),text(row['error_code']))==('uncertain','provider_outcome_unknown')


def test_growth_uses_capture_instants_and_rejects_stale_boundaries(world):
    from engagemate.reports.periodic import collect_account
    with world['pool'].connection() as c:
        for captured, snapshot_date, followers in [
            ('2026-09-01T06:59:00Z','2026-09-01',100),
            ('2026-09-10T07:00:00Z','2026-09-10',999),
        ]:
            c.execute("""INSERT INTO account_growth_snapshots
                (user_id,ig_account_id,snapshot_date,captured_at,follower_count,status)
                VALUES (%s,%s,%s,%s,%s,'partial')""",
                (world['user_id'],world['account_id'],snapshot_date,captured,followers))
        a={'id':world['account_id'],'username':'account'}
        result=collect_account(c,world['user_id'],a,Period('monthly',date(2026,9,1),date(2026,10,1),'America/Los_Angeles'))
        assert result['snapshots'][0]['follower_count']==100
        assert result['snapshots'][1] is None
        other=collect_account(c,world['other_user_id'],a,Period('monthly',date(2026,9,1),date(2026,10,1),'America/Los_Angeles'))
        assert other['snapshots']==[None,None]
