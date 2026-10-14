"""Composio Gmail transport with durable at-most-once submission guards."""
from engagemate.db.text import text
from engagemate.reports.resend import ResendError, request_payload_hash, _address


class ComposioEmailClient:
    def __init__(self, *, pool, http, api_key, account_id, sender, entity_id=""):
        self.pool, self.http, self.api_key = pool, http, api_key
        self.account_id = account_id
        self.entity_id = entity_id
        self.sender = _address(sender, allow_display_name=True)

    def send(self, recipient, rendered, idempotency_key):
        digest = request_payload_hash(self.sender, recipient, rendered)
        with self.pool.connection() as c:
            inserted = c.execute("""INSERT INTO email_provider_requests (request_key,payload_hash,status)
                VALUES (%s,%s,'sending') ON CONFLICT DO NOTHING RETURNING request_key""",
                (idempotency_key,digest)).fetchone()
            if not inserted:
                row = c.execute('SELECT * FROM email_provider_requests WHERE request_key=%s', (idempotency_key,)).fetchone()
                if text(row['payload_hash']) != digest:
                    raise ResendError('idempotency_payload_mismatch',retryable=False)
                if text(row['status']) == 'sent':
                    return text(row['provider_id'])
                # Gmail offers no send-idempotency guarantee. An uncertain request
                # needs reconciliation, never a blind retry through another surface.
                raise ResendError('provider_outcome_unknown',retryable=False)
        try:
            response = self.http.post('https://backend.composio.dev/api/v3/tools/execute/GMAIL_SEND_EMAIL',
                headers={'x-api-key':self.api_key}, json={
                    'connected_account_id':self.account_id,'user_id':self.entity_id,
                    'arguments':{'user_id':'me','recipient_email':recipient,
                                 'subject':rendered.subject,'body':rendered.html,'is_html':True}}, timeout=30)
            data = response.json()
            result = data.get('data', {})
            result = result.get('response_data', result)
            provider_id = result.get('id') or result.get('messageId')
            if not response.is_success or not data.get('successful') or not isinstance(provider_id,str) or not provider_id:
                raise ValueError('unconfirmed provider outcome')
        except Exception:
            with self.pool.connection() as c:
                c.execute("UPDATE email_provider_requests SET status='uncertain' WHERE request_key=%s",(idempotency_key,))
            raise ResendError('provider_outcome_unknown',retryable=False) from None
        with self.pool.connection() as c:
            c.execute("UPDATE email_provider_requests SET status='sent',provider_id=%s WHERE request_key=%s",(provider_id,idempotency_key))
        return provider_id
