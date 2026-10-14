"""Exercise migrated email queues using a mock provider, never real sends."""
import json
from uuid import uuid4

import httpx

from engagemate.db.text import text
from engagemate.reports.reconnect import ReconnectEmailDispatcher
from engagemate.reports.recipients import NotificationCopyDispatcher, queue_copies, queue_weekly_copies
from engagemate.reports.render import RenderedEmail
from tests.worker.test_canary_scripts import database_url, world  # noqa: F401


def dispatcher(kind, world, calls):
    def send(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'id': f'mail-{len(calls)}'})
    return kind(world['pool'], sender='EngageMate <test@example.test>', api_key='test',
                http=httpx.Client(transport=httpx.MockTransport(send)), unsubscribe_key='test-key')


def pause(world):
    with world['pool'].connection() as c:
        c.execute("UPDATE ig_accounts SET paused_at=now(),health_status='paused',pause_reason='session_expired' WHERE id=%s", (world['account_id'],))


def recipient(world, email='extra@example.test', user=None):
    with world['pool'].connection() as c:
        c.execute('INSERT INTO notification_recipients (user_id,email) VALUES (%s,%s) ON CONFLICT DO NOTHING', (user or world['user_id'], email))


def test_reconnect_notice_sends_once_and_links_to_hosted_login(world):
    pause(world); calls=[]; d=dispatcher(ReconnectEmailDispatcher,world,calls)
    assert d.run_once()=='sent'
    assert d.run_once()=='idle'
    pause(world)
    assert d.run_once()=='idle'
    assert len(calls)==1
    assert '/accounts/connect/instagram' in calls[0]['text']
    assert 'No browser extension is needed' in calls[0]['text']


def test_legacy_relationship_login_pause_sends_reconnect_notice(world):
    pause(world)
    with world['pool'].connection() as c:
        c.execute("UPDATE ig_accounts SET pause_reason='relationship_login_required' WHERE id=%s", (world['account_id'],))
    calls=[]
    assert dispatcher(ReconnectEmailDispatcher,world,calls).run_once()=='sent'
    assert len(calls)==1


def test_primary_and_extra_are_independent_and_tenant_scoped(world):
    pause(world);recipient(world);recipient(world,'foreign@example.test',world['other_user_id'])
    calls=[];d=dispatcher(ReconnectEmailDispatcher,world,calls);copies=dispatcher(NotificationCopyDispatcher,world,calls)
    assert d.run_once()=='sent'
    assert copies.run_once()=='sent'
    assert copies.run_once()=='idle'
    destinations=[str(x['to']) for x in calls]
    assert any('extra@example.test' in x for x in destinations)
    assert not any('foreign@example.test' in x for x in destinations)
    with world['pool'].connection() as c:
        assert c.execute('SELECT count(*) n FROM notification_email_copies').fetchone()['n']==1


def test_removed_recipient_is_not_sent_a_queued_copy(world):
    pause(world);recipient(world);calls=[]
    assert dispatcher(ReconnectEmailDispatcher,world,calls).run_once()=='sent'
    with world['pool'].connection() as c:
        c.execute('DELETE FROM notification_recipients WHERE user_id=%s',(world['user_id'],))
    assert dispatcher(NotificationCopyDispatcher,world,calls).run_once()=='idle'
    assert len(calls)==1


def test_restored_account_suppresses_pending_primary_and_copies(world):
    pause(world);recipient(world);calls=[];d=dispatcher(ReconnectEmailDispatcher,world,calls)
    with world['pool'].connection() as c:d.enqueue(c)
    with world['pool'].connection() as c:
        c.execute("UPDATE ig_accounts SET paused_at=NULL,health_status='active',pause_reason=NULL WHERE id=%s",(world['account_id'],))
    assert d.run_once()=='idle'
    assert not calls


def test_fresh_capture_suppresses_old_pause_without_resuming(world):
    pause(world);calls=[];d=dispatcher(ReconnectEmailDispatcher,world,calls)
    with world['pool'].connection() as c:
        d.enqueue(c)
        c.execute("INSERT INTO ig_sessions (user_id,ig_account_id,encrypted_cookie_blob,encryption_version,captured_user_agent,captured_at) VALUES (%s,%s,'test-cipher',1,'test-agent',now()+interval '1 second')",(world['user_id'],world['account_id']))
    assert d.run_once()=='idle'
    assert not calls


def test_copies_strip_owner_tokens_and_deduplicate(world):
    recipient(world);source=uuid4()
    rendered=RenderedEmail(subject='Summary',text='Unsubscribe: https://app.example.com/unsubscribe?u=owner&t=secret',html='<body><a href="https://app.example.com/unsubscribe?u=owner&amp;t=secret">Unsubscribe</a></body>')
    with world['pool'].connection() as c:
        for _ in range(2):queue_copies(c,user_id=world['user_id'],source_type='activity_digest_emails',source_id=source,recipient='primary@example.test',rendered=rendered)
        rows=c.execute('SELECT details FROM notification_email_copies').fetchall()
    assert len(rows)==1
    assert 'secret' not in str(rows[0]['details'])
    assert '/settings' in rows[0]['details']['html']


def test_disabled_optional_preferences_suppress_copies(world):
    recipient(world);rendered=RenderedEmail(subject='Summary',text='hello',html='<body>hello</body>')
    with world['pool'].connection() as c:
        queue_copies(c,user_id=world['user_id'],source_type='activity_digest_emails',source_id=uuid4(),recipient='primary@example.test',rendered=rendered)
        c.execute('INSERT INTO email_preferences (user_id,activity_emails) VALUES (%s,false)',(world['user_id'],))
    calls=[]
    assert dispatcher(NotificationCopyDispatcher,world,calls).run_once()=='idle'
    assert not calls


def test_weekly_copies_queue_once(world):
    recipient(world);source=uuid4();rendered=RenderedEmail(subject='Weekly',text='hello',html='<body>hello</body>')
    for _ in range(2):queue_weekly_copies(world['pool'],world['user_id'],source,'primary@example.test',rendered)
    calls=[];d=dispatcher(NotificationCopyDispatcher,world,calls)
    assert d.run_once()=='sent'
    assert d.run_once()=='idle'
    assert len(calls)==1


def test_copy_retry_does_not_resend_primary(world):
    pause(world);recipient(world);primary=[]
    assert dispatcher(ReconnectEmailDispatcher,world,primary).run_once()=='sent'
    attempts=[]
    def provider(request):
        attempts.append((request.headers['idempotency-key'],request.content))
        return httpx.Response(429,json={'name':'rate_limit_exceeded'}) if len(attempts)==1 else httpx.Response(200,json={'id':'copy-retry'})
    d=NotificationCopyDispatcher(world['pool'],sender='EngageMate <test@example.test>',api_key='test',http=httpx.Client(transport=httpx.MockTransport(provider)))
    assert d.run_once()=='pending'
    with world['pool'].connection() as c:c.execute("UPDATE notification_email_copies SET next_attempt_at=now()")
    assert d.run_once()=='sent'
    assert attempts[0]==attempts[1]
    assert len(primary)==1


def test_fresh_session_suppresses_pending_extra_copy(world):
    pause(world);recipient(world);calls=[]
    assert dispatcher(ReconnectEmailDispatcher,world,calls).run_once()=='sent'
    with world['pool'].connection() as c:
        c.execute("INSERT INTO ig_sessions (user_id,ig_account_id,encrypted_cookie_blob,encryption_version,captured_user_agent,captured_at) VALUES (%s,%s,'test-cipher',1,'test-agent',now()+interval '1 second')",(world['user_id'],world['account_id']))
    assert dispatcher(NotificationCopyDispatcher,world,calls).run_once()=='idle'
    assert len(calls)==1


def test_next_expired_session_can_send_a_new_notice(world):
    pause(world);calls=[];d=dispatcher(ReconnectEmailDispatcher,world,calls)
    assert d.run_once()=='sent'
    with world['pool'].connection() as c:
        c.execute("INSERT INTO ig_sessions (user_id,ig_account_id,encrypted_cookie_blob,encryption_version,captured_user_agent,captured_at) VALUES (%s,%s,'test-cipher',1,'test-agent',now())",(world['user_id'],world['account_id']))
        c.execute("UPDATE ig_accounts SET paused_at=now()+interval '1 second' WHERE id=%s",(world['account_id'],))
    assert d.run_once()=='sent'
    assert d.run_once()=='idle'
    assert len(calls)==2
