CREATE TABLE notification_recipients (
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    email text NOT NULL CHECK (email=lower(btrim(email)) AND length(email)<=254),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id,email)
);
--> statement-breakpoint
CREATE TABLE notification_email_copies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type text NOT NULL,
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
    UNIQUE (source_type, source_id, recipient)
);
CREATE INDEX notification_email_copies_pending ON notification_email_copies (next_attempt_at) WHERE status IN ('pending','sending');
