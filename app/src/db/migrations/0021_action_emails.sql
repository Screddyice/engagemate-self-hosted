CREATE TABLE action_email_activation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    started_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO action_email_activation (singleton) VALUES (true);
--> statement-breakpoint
CREATE TABLE action_emails (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_id uuid NOT NULL,
    action text NOT NULL CHECK (action IN ('view','like','reply','dm','follow','unfollow')),
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
    UNIQUE (source_id, action)
);
CREATE INDEX action_emails_pending ON action_emails (next_attempt_at) WHERE status IN ('pending','sending');
--> statement-breakpoint
CREATE TABLE email_provider_requests (
    request_key text PRIMARY KEY,
    payload_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('sending','sent','uncertain','failed')),
    provider_id text,
    created_at timestamptz NOT NULL DEFAULT now()
);
