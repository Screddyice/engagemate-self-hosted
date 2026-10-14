"""Transactional reconnect reminders, deduplicated by account and saved session."""
from html import escape

from engagemate.reports.actions import OutboxDispatcher
from engagemate.reports.layout import email_layout
from engagemate.reports.render import RenderedEmail
from engagemate.web_origin import web_url

CONNECT_PATH = "/accounts/connect/instagram"
# Repeated health events for the same saved session are one incident. A new
# capture ends that incident even while the account remains paused for review.
NEEDS_LOGIN = """
a.paused_at IS NOT NULL
AND a.pause_reason IN ('session_expired', 'login_required', 'authentication_required',
                       'relationship_login_required')
AND coalesce(s.captured_at, a.created_at) <= a.paused_at
"""
CURRENT_SESSIONS = """
SELECT a.id account_id, a.user_id, a.username,
       coalesce(s.captured_at, a.created_at) session_captured_at
FROM ig_accounts a
LEFT JOIN LATERAL (
    SELECT captured_at FROM ig_sessions
    WHERE ig_account_id=a.id AND user_id=a.user_id
    ORDER BY captured_at DESC, id DESC LIMIT 1
) s ON true
WHERE """ + NEEDS_LOGIN


class ReconnectEmailDispatcher(OutboxDispatcher):
    TABLE = 'reconnect_emails'
    KEY_PREFIX = 'instagram-reconnect'
    # Required account-access notice, independent of marketing/digest preferences.

    def enqueue(self, c):
        c.execute(f"""
            WITH current_sessions AS ({CURRENT_SESSIONS})
            UPDATE reconnect_emails n SET status='failed',error_code='login_restored',lease_until=NULL
            WHERE n.status IN ('pending','sending')
              AND (n.lease_until IS NULL OR n.lease_until < now())
              AND NOT EXISTS (SELECT 1 FROM current_sessions a
                  WHERE a.account_id=n.ig_account_id AND a.user_id=n.user_id
                    AND a.session_captured_at=n.session_captured_at)
        """)
        c.execute(f"""
            WITH current_sessions AS ({CURRENT_SESSIONS})
            INSERT INTO reconnect_emails
                (user_id,ig_account_id,session_captured_at,recipient,details)
            SELECT a.user_id,a.account_id,a.session_captured_at,u.email,
                   jsonb_build_object('account',a.username)
            FROM current_sessions a JOIN users u ON u.id=a.user_id
            WHERE u.email IS NOT NULL AND btrim(u.email)<>''
              AND NOT EXISTS (SELECT 1 FROM reconnect_emails n
                  WHERE n.ig_account_id=a.account_id
                    AND n.session_captured_at=a.session_captured_at)
            ORDER BY a.session_captured_at LIMIT 100
            ON CONFLICT (ig_account_id,session_captured_at) DO NOTHING
        """)

    def render(self, row):
        account = str(row['details']['account'])
        connect_url = web_url(CONNECT_PATH)
        paragraphs = [
            f"Your Instagram account @{account} needs a new sign-in. EngageMate has paused its actions.",
            f"Open this link in your browser to reconnect: {connect_url}",
            "Sign in to EngageMate if asked, then connect the same Instagram account. "
            "Enter your Instagram password and complete any verification shown on the page. "
            "No browser extension is needed.",
            "After reconnecting, open Account Health in EngageMate to review and resume the account.",
            "Never send your password or security code by email. Enter them only in the sign-in flow.",
        ]
        return RenderedEmail(
            subject=f"Reconnect @{account} to EngageMate",
            text='\n\n'.join(paragraphs),
            html=email_layout('Reconnect Instagram', ''.join(f'<p>{escape(p)}</p>' for p in paragraphs),
                              button_label='Reconnect Instagram', button_url=connect_url,
                              footer='An account-access notice from EngageMate'),
        )
