"""Durable outbox delivery for user emails, and the action-emails service loop."""
from __future__ import annotations

import logging
import time
from uuid import uuid4

import httpx
from psycopg.types.json import Jsonb

from engagemate.config import Settings
from engagemate.db.pool import create_pool
from engagemate.db.text import text as dbtext
from engagemate.reports.render import RenderedEmail
from engagemate.reports.resend import ResendClient, ResendError, validate_recipient
from engagemate.reports.unsubscribe import unsubscribe_url


class OutboxDispatcher:
    """One durable email per outbox row: enqueue, lease, freeze payload, send once.

    Subclasses name the table, the enqueue statement, the idempotency prefix, and
    the renderer. Table and prefix are class constants, never caller input.
    """

    TABLE = ''
    ENQUEUE = ''
    KEY_PREFIX = ''
    # email_preferences column that lets a user turn this email off.
    PREFERENCE = ''

    def __init__(self, pool, *, sender: str, api_key: str, http: httpx.Client, composio_account_id=None,
                 composio_entity_id="", unsubscribe_key: str | None = None):
        self.pool, self.sender, self.api_key, self.http = pool, sender, api_key, http
        self.composio_account_id = composio_account_id
        self.composio_entity_id = composio_entity_id
        self.unsubscribe_key = unsubscribe_key

    def render(self, row) -> RenderedEmail:
        raise NotImplementedError

    def enqueue(self, c) -> None:
        c.execute(self.ENQUEUE)

    def unsubscribe_link(self, row, category: str) -> str:
        if not self.unsubscribe_key:
            raise ValueError('unsubscribe signing key is required')
        return unsubscribe_url(self.unsubscribe_key, row['user_id'], category)

    def run_once(self) -> str:
        token = uuid4()
        table = self.TABLE
        with self.pool.connection() as c:
            self.enqueue(c)
            # Resend's dedupe retention is bounded. Never retry beyond that window.
            c.execute(f"""UPDATE {table} SET status='uncertain',error_code='retry_window_expired'
                         WHERE status IN ('pending','sending') AND first_attempt_at < now()-interval '23 hours'""")
            row = c.execute(f"""SELECT * FROM {table}
                WHERE (status='pending' AND next_attempt_at<=now())
                   OR (status='sending' AND lease_until<now())
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""").fetchone()
            if row is None:
                return 'idle'
            if self.PREFERENCE:
                # A row queued before the user opted out must not go out after.
                opted_in = c.execute(
                    f'SELECT coalesce((SELECT {self.PREFERENCE} FROM email_preferences WHERE user_id=%s), true) ok',
                    (row['user_id'],)).fetchone()['ok']
                if not opted_in:
                    c.execute(f"UPDATE {table} SET status='failed',error_code='unsubscribed',lease_until=NULL WHERE id=%s",
                              (row['id'],))
                    return 'unsubscribed'
            try:
                payload = row['payload']
                if payload is None:
                    rendered = self.render(row)
                    payload = {'sender': self.sender, 'recipient': validate_recipient(dbtext(row['recipient'])),
                               'subject': rendered.subject, 'text': rendered.text, 'html': rendered.html}
            except (ValueError, KeyError, TypeError):
                c.execute(f"UPDATE {table} SET status='failed',error_code='payload_invalid' WHERE id=%s", (row['id'],))
                return 'failed'
            if row['payload'] is None and table != 'notification_email_copies':
                from engagemate.reports.recipients import queue_copies
                queue_copies(c, user_id=row['user_id'], source_type=table,
                             source_id=row['id'], recipient=payload['recipient'],
                             rendered=RenderedEmail(subject=payload['subject'], text=payload['text'], html=payload['html']))
            c.execute(f"""UPDATE {table} SET status='sending',payload=%s,lease_token=%s,
                         lease_until=now()+interval '60 seconds',attempts=attempts+1,
                         first_attempt_at=coalesce(first_attempt_at,now()) WHERE id=%s""",
                      (Jsonb(payload), token, row['id']))
        # Payload, recipient, and key are immutable across retries, including restarts.
        try:
            if self.composio_account_id:
                from engagemate.reports.composio import ComposioEmailClient
                client = ComposioEmailClient(pool=self.pool, http=self.http, api_key=self.api_key,
                    account_id=self.composio_account_id, sender=payload['sender'], entity_id=self.composio_entity_id)
            else:
                client = ResendClient(api_key=self.api_key, sender=payload['sender'], http=self.http)
            provider_id = client.send(payload['recipient'], RenderedEmail(
                subject=payload['subject'], text=payload['text'], html=payload['html']),
                f"{self.KEY_PREFIX}/{row['id']}")
        except ResendError as error:
            status = 'uncertain' if error.code == 'provider_outcome_unknown' else ('pending' if error.retryable else 'failed')
            with self.pool.connection() as c:
                c.execute(f"""UPDATE {table} SET status=%s,error_code=%s,
                    next_attempt_at=now()+interval '5 minutes',lease_until=NULL
                    WHERE id=%s AND lease_token=%s""", (status,error.code,row['id'],token))
            return status
        # An unexpected crash leaves the lease for recovery with the same request key.
        with self.pool.connection() as c:
            c.execute(f"""UPDATE {table} SET status='sent',provider_id=%s,sent_at=now(),
                error_code=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s""",
                      (provider_id,row['id'],token))
        return 'sent'


def main():
    from engagemate.reports.recipients import NotificationCopyDispatcher
    from engagemate.reports.reconnect import ReconnectEmailDispatcher
    from engagemate.reports.digest import ActivityDigestDispatcher
    from engagemate.reports.onboarding import OnboardingEmailDispatcher

    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    key = settings.composio_api_key if settings.composio_email_account_id else settings.resend_api_key
    if not key or not settings.action_email_from:
        raise SystemExit('Action email sender and Resend key must be configured')
    with create_pool(settings.database_url, min_size=1, max_size=2) as pool, httpx.Client() as http:
        options = dict(sender=settings.action_email_from, api_key=key.get_secret_value(), http=http,
                       composio_account_id=settings.composio_email_account_id,
                       composio_entity_id=settings.composio_email_entity_id,
                       unsubscribe_key=settings.internal_api_key.get_secret_value())
        # Reconnect, daily activity, and onboarding share one process, one
        # transport, and one at-most-once provider ledger; each keeps its own
        # outbox table. Per-action receipts (action_emails) stopped on 2026-09-17.
        dispatchers = (('reconnect_email', ReconnectEmailDispatcher(pool, **options)),
                       ('activity_digest', ActivityDigestDispatcher(pool, **options)),
                       ('onboarding_email', OnboardingEmailDispatcher(pool, **options)),
                       ('notification_copy', NotificationCopyDispatcher(pool, **options)))
        while True:
            sent = False
            for label, dispatcher in dispatchers:
                try:
                    result = dispatcher.run_once()
                    if result != 'idle':
                        logging.info('%s status=%s', label, result)
                    sent = sent or result == 'sent'
                except Exception:  # No recipient, message, or provider response in logs.
                    logging.error('%s dispatch_failed', label)
            time.sleep(1 if sent else 30)


if __name__ == '__main__':
    main()
