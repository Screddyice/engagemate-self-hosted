"""Independent notification copies for account-managed additional recipients."""
import re
from psycopg.types.json import Jsonb
from engagemate.reports.actions import OutboxDispatcher
from engagemate.reports.render import RenderedEmail
from engagemate.reports.reconnect import CURRENT_SESSIONS
from engagemate.web_origin import web_url

# Copies must not distribute bearer links that change the owner's preferences.
# Match the path on any host, so a link minted under an earlier origin is
# stripped too.
UNSUBSCRIBE = re.compile(r'https?://[^\s<>"\'/]+/unsubscribe\?[^\s<>"\']+')
SOURCES = frozenset({'reconnect_emails', 'onboarding_emails', 'activity_digest_emails', 'weekly_report'})


def queue_copies(c, *, user_id, source_type, source_id, recipient, rendered):
    if source_type not in SOURCES:
        return
    note = 'You receive this copy because the account owner added your email in EngageMate Settings. Ask them to remove your address to stop copies.'
    settings_url = web_url('/settings')
    details = {
        'subject': rendered.subject,
        'text': UNSUBSCRIBE.sub(settings_url, rendered.text) + '\n\n' + note,
        'html': UNSUBSCRIBE.sub(settings_url, rendered.html).replace('</body>', f'<p>{note}</p></body>'),
    }
    c.execute("""INSERT INTO notification_email_copies
        (user_id,source_type,source_id,recipient,details)
        SELECT r.user_id,%s,%s,r.email,%s FROM notification_recipients r
        WHERE r.user_id=%s AND lower(r.email)<>lower(%s)
        ON CONFLICT (source_type,source_id,recipient) DO NOTHING""",
        (source_type,source_id,Jsonb(details),user_id,recipient))


def queue_weekly_copies(pool, user_id, source_id, recipient, rendered):
    with pool.connection() as c:
        queue_copies(c,user_id=user_id,source_type='weekly_report',source_id=source_id,
                     recipient=recipient,rendered=rendered)


class NotificationCopyDispatcher(OutboxDispatcher):
    TABLE = 'notification_email_copies'
    KEY_PREFIX = 'notification-copy'

    def enqueue(self, c):
        c.execute(f"""WITH current_sessions AS ({CURRENT_SESSIONS})
            UPDATE notification_email_copies n SET status='failed',error_code='recipient_or_notice_disabled',lease_until=NULL
            WHERE n.status IN ('pending','sending')
              AND (n.lease_until IS NULL OR n.lease_until < now())
              AND (
                NOT EXISTS (SELECT 1 FROM notification_recipients r WHERE r.user_id=n.user_id AND r.email=n.recipient)
                OR (n.source_type='activity_digest_emails' AND EXISTS
                    (SELECT 1 FROM email_preferences p WHERE p.user_id=n.user_id AND NOT p.activity_emails))
                OR (n.source_type='onboarding_emails' AND EXISTS
                    (SELECT 1 FROM email_preferences p WHERE p.user_id=n.user_id AND NOT p.onboarding_emails))
                OR (n.source_type='weekly_report' AND EXISTS
                    (SELECT 1 FROM weekly_report_preferences p WHERE p.user_id=n.user_id AND NOT p.enabled))
                OR (n.source_type='reconnect_emails' AND NOT EXISTS
                    (SELECT 1 FROM reconnect_emails e JOIN current_sessions a
                       ON a.account_id=e.ig_account_id AND a.user_id=e.user_id
                       AND a.session_captured_at=e.session_captured_at
                     WHERE e.id=n.source_id AND e.user_id=n.user_id))
              )""")

    def render(self, row):
        details = row['details']
        return RenderedEmail(subject=details['subject'],text=details['text'],html=details['html'])
