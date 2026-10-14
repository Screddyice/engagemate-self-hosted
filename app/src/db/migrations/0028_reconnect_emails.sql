CREATE TABLE reconnect_emails (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    session_captured_at timestamptz NOT NULL,
    recipient text NOT NULL,
    details jsonb NOT NULL,
    payload jsonb,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','failed','uncertain')),
    created_at timestamptz NOT NULL DEFAULT now(),
    first_attempt_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    provider_id text,
    error_code text,
    sent_at timestamptz,
    UNIQUE (ig_account_id, session_captured_at),
    FOREIGN KEY (ig_account_id, user_id) REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
CREATE INDEX reconnect_emails_pending ON reconnect_emails (next_attempt_at) WHERE status IN ('pending','sending');
