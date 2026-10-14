-- Per-user switches for the emails a user can turn off from a footer link or
-- Settings. No row means every email is on. The weekly report keeps its own
-- switch in weekly_report_preferences.enabled.
CREATE TABLE email_preferences (
    user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    activity_emails boolean NOT NULL DEFAULT true,
    onboarding_emails boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);
--> statement-breakpoint
-- One daily summary per user replaces one email per confirmed action. The
-- activation starts at the oldest receipt still unsent, so actions queued for
-- a receipt but never emailed land in the first summary instead of nowhere.
CREATE TABLE activity_digest_activation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    started_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO activity_digest_activation (singleton, started_at)
SELECT true, coalesce(min((details->>'at')::timestamptz), now())
FROM action_emails WHERE status = 'pending' AND payload IS NULL;
--> statement-breakpoint
UPDATE action_emails SET status = 'failed', error_code = 'replaced_by_daily_digest'
WHERE status = 'pending' AND payload IS NULL;
--> statement-breakpoint
CREATE TABLE activity_digest_emails (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    digest_date date NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL,
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
    CHECK (window_start < window_end),
    UNIQUE (user_id, digest_date)
);
CREATE INDEX activity_digest_emails_pending ON activity_digest_emails (next_attempt_at) WHERE status IN ('pending','sending');
