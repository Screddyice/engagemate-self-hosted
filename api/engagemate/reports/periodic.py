"""Periodic owner reports with durable claims."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo
import logging

import httpx
from psycopg.types.json import Jsonb

from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.db.text import text
from engagemate.reports.composio import ComposioEmailClient
from engagemate.reports.layout import email_layout
from engagemate.reports.periodic_layout import paragraph, section, data_table, comparison_table, metric_cards, note
from engagemate.reports.render import RenderedEmail
from engagemate.reports.resend import ResendError, validate_recipient


@dataclass(frozen=True)
class Period:
    kind: str
    start: date
    end: date  # exclusive
    timezone: str

    @property
    def bounds(self):
        zone = ZoneInfo(self.timezone)
        return tuple(datetime.combine(d, time.min, zone).astimezone(UTC)
                     for d in (self.start, self.end))


def due_period(kind: str, now: datetime, enabled_at: datetime, timezone: str) -> Period | None:
    if kind not in {'fortnightly', 'monthly'} or now.tzinfo is None or enabled_at.tzinfo is None:
        raise ValueError('invalid reporting schedule')
    zone = ZoneInfo(timezone)
    local = now.astimezone(zone)
    activated = enabled_at.astimezone(zone)
    if kind == 'monthly':
        end = local.date().replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        if activated.date() >= end:
            return None
    else:
        periods = (local.date() - activated.date()).days // 14
        if periods < 1:
            return None
        end = activated.date() + timedelta(days=periods * 14)
        start = end - timedelta(days=14)
    if local < datetime.combine(end, time(9), zone):
        return None
    return Period(kind, start, end, timezone)


def display_delta(start, end, *, rate=False):
    if start is None or end is None:
        latest = '' if end is None else (f' Latest observed: {end / 100:.2f}%.' if rate else f' Latest observed: {end:,}.')
        return 'Not available: comparable observations are missing.' + latest
    delta = end - start
    if rate:
        return f'{start / 100:.2f}% → {end / 100:.2f}% ({delta / 100:+.2f} percentage points)'
    return f'{start:,} → {end:,} ({delta:+,})'


def collect_account(c, user_id, account, period, *, as_of=None):
    start, end = period.bounds
    if as_of is not None:
        if as_of.tzinfo is None or as_of <= start:
            raise ValueError("as_of must be an aware instant after the period start")
        end = min(end, as_of)
    actions = c.execute("""SELECT action,count(*) n FROM story_engagements
        WHERE user_id=%s AND ig_account_id=%s AND status='sent'
        AND executed_at >= %s AND executed_at < %s GROUP BY action""",
        (user_id, account['id'], start, end)).fetchall()
    counts = {text(r['action']): r['n'] for r in actions}
    relationships = c.execute("""SELECT
        count(*) FILTER (WHERE confirmed_follow_at >= %s AND confirmed_follow_at < %s
            AND evidence #>> '{follow,outcome}'='confirmed') follows,
        count(*) FILTER (WHERE unfollowed_at >= %s AND unfollowed_at < %s
            AND evidence #>> '{unfollow,outcome}'='confirmed') unfollows,
        count(*) FILTER (WHERE confirmed_follow_at >= %s AND confirmed_follow_at < %s
            AND evidence #>> '{follow,outcome}'='confirmed') eligible,
        count(*) FILTER (WHERE confirmed_follow_at >= %s AND confirmed_follow_at < %s
            AND evidence #>> '{follow,outcome}'='confirmed'
            AND EXISTS (SELECT 1 FROM relationship_followback_observations o
                WHERE o.relationship_id=relationship_follows.id AND o.observed_at < %s)) follow_backs
        FROM relationship_follows WHERE user_id=%s AND ig_account_id=%s""",
        (start,end,start,end,start,end,start,end,end,user_id,account['id'])).fetchone()
    snapshots = []
    for boundary in (start, end):
        snapshots.append(c.execute("""SELECT snapshot_date,captured_at,follower_count,engagement_rate_bps,posts_sampled
            FROM account_growth_snapshots WHERE user_id=%s AND ig_account_id=%s
            AND status IN ('complete','partial') AND captured_at <= %s
            AND captured_at >= %s::timestamptz - interval '2 days'
            ORDER BY captured_at DESC LIMIT 1""",
            (user_id,account['id'],boundary,boundary)).fetchone())
    return {'username':text(account['username']), 'counts':counts,
            'relationships':dict(relationships), 'snapshots':snapshots}


def render(period: Period, accounts: list[dict]) -> RenderedEmail:
    title = 'Your two-week engagement outcomes' if period.kind == 'fortnightly' else 'Your monthly Instagram growth'
    lines = [title, f'{period.start} to {period.end - timedelta(days=1)} ({period.timezone})']
    for account in accounts:
        lines.extend(['', '@' + account['username']])
        counts, relationships = account['counts'], account['relationships']
        total = sum(counts.values()) + relationships['follows'] + relationships['unfollows']
        lines.append(f'Confirmed EngageMate actions: {total}')
        if period.kind == 'fortnightly':
            lines.extend([
                f'Follow-backs observed after EngageMate follows: {relationships["follow_backs"] if relationships["follow_backs"] else "Not available: no verified follow-back observations recorded"}',
                f'Confirmed follows in this period: {relationships["eligible"]}',
                'Follow-back coverage: partial. These are verified observations from relationship checks, not a complete follower audit.',
                'Incoming responses: not available. Inbox response tracking is not connected.',
                'A follow-back after an action is an observed outcome, not proof that the action caused it.',
            ])
        else:
            first, last = account['snapshots']
            follower = lambda row: None if row is None else row['follower_count']
            rate = lambda row: None if row is None or not row['posts_sampled'] else row['engagement_rate_bps']
            lines.extend([
                'Followers: ' + display_delta(follower(first), follower(last)),
                'Post engagement rate: ' + display_delta(rate(first), rate(last), rate=True),
                'Observation dates: ' + ' → '.join(str(r['snapshot_date']) if r else 'missing' for r in (first,last)),
                'Engagement rate is average likes plus comments on sampled public posts divided by followers. It is not reach or impressions.',
                'Public-post samples may change between observations. Growth is observational and is not attributed solely to EngageMate.',
            ])
    body = '\n'.join(lines)
    html_body=''.join(account_html(period,account) for account in accounts)
    zone_label = "Pacific time" if period.timezone == "America/Los_Angeles" else period.timezone
    subtitle=f"{period.start:%b %-d} to {period.end - timedelta(days=1):%b %-d, %Y} | {zone_label}"
    return RenderedEmail(subject=f'EngageMate {period.kind} report: {period.start}', text=body,
        html=email_layout(title, paragraph(subtitle, muted=True) + html_body))


def account_html(period, account):
    counts, relationships = account['counts'], account['relationships']
    total=sum(counts.values())+relationships['follows']+relationships['unfollows']
    heading=paragraph('@'+account['username'])
    if period.kind == 'fortnightly':
        result=heading+metric_cards([
            ('Actions completed',f'{total:,}','Confirmed by EngageMate'),
            ('Accounts followed',f'{relationships["follows"]:,}','During this reporting period'),
        ])
        result+=section('Your action breakdown',data_table([
            ('Story views',counts.get('view',0)),('Story likes',counts.get('like',0)),
            ('Story replies',counts.get('reply',0)),('Direct messages',counts.get('dm',0)),
            ('Follows',relationships['follows']),('Unfollows',relationships['unfollows']),
        ]))
        result+=section('Responses and follow-backs',data_table([
            ('Responses to messages','Not measured yet'),
            ('Verified follow-backs',relationships['follow_backs'] or 'Not measured yet'),
        ]))
        result+=note('About these results',[
            'Incoming reply tracking is not connected yet.',
            'We count follow-backs only after a later profile check confirms them. Checks cover part of your audience.',
            'A follow-back after an action is an observed outcome; other activity can also influence follows.',
        ])
        return result
    first,last=account['snapshots']
    def count(row):
        return 'Not collected' if row is None else f'{row["follower_count"]:,}'
    def rate(row):
        return None if row is None or not row['posts_sampled'] else row['engagement_rate_bps']
    def rate_text(row):
        value=rate(row)
        return 'Not collected' if value is None else f'{value/100:.2f}%'
    def observed(row):
        return 'Not collected' if row is None else row['snapshot_date'].strftime('%b %-d')
    result=heading+metric_cards([
        ('Current followers',count(last) if last else 'Pending','Latest verified count'),
        ('Post engagement rate',rate_text(last) if rate(last) is not None else 'Pending',
         f'{last["posts_sampled"]} posts sampled' if last else 'Waiting for a verified sample'),
    ])
    result+=section('Your growth at a glance',comparison_table([
        ('Followers',count(first),count(last)),
        ('Engagement rate',rate_text(first),rate_text(last)),
        ('Observed on',observed(first),observed(last)),
    ]))
    if first and last:
        changes=[('Follower change',f'{last["follower_count"]-first["follower_count"]:+,}')]
        if rate(first) is not None and rate(last) is not None:
            changes.append(('Engagement-rate change',f'{(rate(last)-rate(first))/100:+.2f} percentage points'))
        result+=section('Change this period',data_table(changes))
    else:
        result+=note('Growth comparison pending',[
            'We need verified observations at the start and end of the period to calculate growth.',
            'Your latest observation becomes a baseline for future reports.' if last else 'We are waiting for the first verified account observation.',
        ])
    result+=section('EngageMate activity',data_table([('Confirmed actions this period',total)]))
    result+=note('How to read engagement',[
        'We divide average likes plus comments on the sampled public posts by your follower count.',
        'The post sample may change between observations. Reach and impressions are not included.',
        'Growth is observed across your account; other activity can influence the results.',
    ])
    return result


class PeriodicDispatcher:
    def __init__(self, pool, client):
        self.pool, self.client = pool, client

    def enqueue(self, now):
        with self.pool.connection() as c:
            c.execute('INSERT INTO periodic_report_preferences(user_id) SELECT id FROM users ON CONFLICT DO NOTHING')
            owners = c.execute("""SELECT p.*,u.email FROM periodic_report_preferences p
                JOIN users u ON u.id=p.user_id WHERE EXISTS
                (SELECT 1 FROM ig_accounts a WHERE a.user_id=p.user_id)""").fetchall()
            for owner in owners:
                for kind in ('fortnightly','monthly'):
                    if not owner[kind+'_enabled']:
                        continue
                    period = due_period(kind,now,owner['enabled_at'],text(owner['timezone']))
                    if period is None:
                        continue
                    if c.execute('SELECT 1 FROM periodic_report_deliveries WHERE user_id=%s AND kind=%s AND period_start=%s',
                                 (owner['user_id'],kind,period.start)).fetchone():
                        continue
                    accounts = c.execute('SELECT id,username FROM ig_accounts WHERE user_id=%s ORDER BY id', (owner['user_id'],)).fetchall()
                    rendered = render(period,[collect_account(c,owner['user_id'],a,period) for a in accounts])
                    payload = {'subject':rendered.subject,'text':rendered.text,'html':rendered.html}
                    c.execute("""INSERT INTO periodic_report_deliveries(user_id,kind,period_start,period_end,recipient,payload)
                        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (owner['user_id'],kind,period.start,period.end,validate_recipient(text(owner['email'])),Jsonb(payload)))

    def send_one(self):
        with self.pool.connection() as c:
            c.execute("""UPDATE periodic_report_deliveries
                SET status='uncertain',error_code='provider_outcome_unknown'
                WHERE status='sending' AND attempted_at < now()-interval '2 minutes'""")
            row = c.execute("""SELECT * FROM periodic_report_deliveries WHERE status='pending'
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
            if not row:
                return 'idle'
            c.execute("UPDATE periodic_report_deliveries SET status='sending',attempted_at=now() WHERE id=%s",(row['id'],))
        try:
            provider_id = self.client.send(text(row['recipient']),RenderedEmail(**row['payload']),f'engagemate-periodic/{row["id"]}')
        except ResendError as error:
            with self.pool.connection() as c:
                c.execute("UPDATE periodic_report_deliveries SET status='uncertain',error_code=%s WHERE id=%s",(error.code,row['id']))
            return 'uncertain'
        with self.pool.connection() as c:
            c.execute("UPDATE periodic_report_deliveries SET status='sent',provider_id=%s,sent_at=now() WHERE id=%s",(provider_id,row['id']))
        return 'sent'


def main():
    settings = Settings()
    if not settings.composio_api_key or not settings.composio_email_account_id or not settings.weekly_report_from:
        raise SystemExit('Composio email configuration required')
    with create_pool(settings.database_url,min_size=1,max_size=2) as pool, httpx.Client() as http:
        client = ComposioEmailClient(pool=pool,http=http,api_key=settings.composio_api_key.get_secret_value(),
            account_id=settings.composio_email_account_id,entity_id=settings.composio_email_entity_id,sender=settings.weekly_report_from)
        dispatcher = PeriodicDispatcher(pool,client)
        dispatcher.enqueue(datetime.now(UTC))
        for _ in range(25):
            result = dispatcher.send_one()
            if result == 'idle':
                break
            logging.info('periodic_email status=%s',result)
            if result == 'uncertain':
                return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
