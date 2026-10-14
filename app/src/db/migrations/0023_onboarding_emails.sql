CREATE TABLE onboarding_email_activation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    started_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO onboarding_email_activation (singleton) VALUES (true);
--> statement-breakpoint
CREATE TABLE onboarding_emails (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('welcome','connect_reminder','account_connected','what_to_expect')),
    source_id uuid NOT NULL,
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
    UNIQUE (kind, source_id)
);
CREATE INDEX onboarding_emails_pending ON onboarding_emails (next_attempt_at) WHERE status IN ('pending','sending');
