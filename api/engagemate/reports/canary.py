"""Scoped report preview and canary."""
from __future__ import annotations

import argparse
import re
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.db.text import text
from engagemate.reports.composio import ComposioEmailClient
from engagemate.reports.periodic_layout import note
from engagemate.reports.periodic import Period, collect_account, render
from engagemate.reports.render import RenderedEmail
from engagemate.reports.resend import validate_recipient


def prepare(c, user_id, account_id, kind, now):
    if kind not in {'fortnightly', 'monthly'} or now.tzinfo is None:
        raise ValueError('invalid report canary')
    account = c.execute('''SELECT a.id,a.username,u.email FROM ig_accounts a
        JOIN users u ON u.id=a.user_id WHERE a.user_id=%s AND a.id=%s''',
        (user_id,account_id)).fetchone()
    if account is None:
        raise ValueError('owned account required')
    today = now.astimezone(ZoneInfo('America/Los_Angeles')).date()
    end = today + timedelta(days=1)
    start = today.replace(day=1) if kind == 'monthly' else end - timedelta(days=14)
    period = Period(kind,start,end,'America/Los_Angeles')
    values = collect_account(c,user_id,account,period,as_of=now)
    report = render(period,[values])
    body = ('TEST EMAIL: live account data\n'
            f'Data through {now.astimezone(ZoneInfo(period.timezone)):%Y-%m-%d %H:%M %Z}. '
            'This is a partial-period test, not a completed scheduled report. '
            'No recurring schedule was activated.\n\n' + report.text)
    title = f'TEST: EngageMate {kind} report'
    rendered = RenderedEmail(subject=title,text=body,
        html=report.html.replace('<!--email-content-->',
            note('Test email: live account data',[
                f'Data through {now.astimezone(ZoneInfo(period.timezone)):%b %-d, %Y at %-I:%M %p %Z}.',
                'This is a partial-period preview. Your recurring schedules have not changed.',
            ])+'<div style="height:22px;line-height:22px;">&nbsp;</div><!--email-content-->',1))
    return validate_recipient(text(account['email'])), rendered


def run(pool, client, *, user_id, account_id, kind, now, send=False, revision="v1"):
    if not re.fullmatch(r"[a-z0-9-]{1,32}", revision):
        raise ValueError("invalid canary revision")
    today=now.astimezone(ZoneInfo('America/Los_Angeles')).date()
    key=f'engagemate-report-canary/{user_id}/{account_id}/{kind}/{today}'
    if revision != 'v1':
        key += '/' + revision
    with pool.connection() as c:
        if c.execute('SELECT 1 FROM ig_accounts WHERE user_id=%s AND id=%s',
                     (user_id,account_id)).fetchone() is None:
            raise ValueError('owned account required')
        existing=c.execute('SELECT status,provider_id FROM email_provider_requests WHERE request_key=%s',(key,)).fetchone()
        if existing and send:
            if text(existing['status']) == 'sent':
                return {'status':'already_sent','provider_id':text(existing['provider_id'])}
            raise ValueError('prior canary outcome requires reconciliation')
        recipient, rendered=prepare(c,user_id,account_id,kind,now)
    if not send:
        return {'status':'preview','text':rendered.text,'html':rendered.html}
    if client is None:
        raise ValueError('email client required')
    return {'status':'sent','provider_id':client.send(recipient,rendered,key)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id',required=True,type=UUID)
    parser.add_argument('--account-id',required=True,type=UUID)
    parser.add_argument('--kind',required=True,choices=('fortnightly','monthly'))
    parser.add_argument('--send',action='store_true')
    parser.add_argument('--revision',default='v1')
    args=parser.parse_args()
    settings=Settings()
    with create_pool(settings.database_url,min_size=1,max_size=2) as pool, httpx.Client() as http:
        client=None
        if args.send:
            if not settings.composio_api_key or not settings.composio_email_account_id or not settings.weekly_report_from:
                raise ValueError('Composio email configuration required')
            client=ComposioEmailClient(pool=pool,http=http,api_key=settings.composio_api_key.get_secret_value(),
                account_id=settings.composio_email_account_id,entity_id=settings.composio_email_entity_id,
                sender=settings.weekly_report_from)
        result=run(pool,client,user_id=args.user_id,account_id=args.account_id,kind=args.kind,
                   now=datetime.now(UTC),send=args.send,revision=args.revision)
        if result['status']=='preview':
            print(result['text'])
        else:
            print(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
