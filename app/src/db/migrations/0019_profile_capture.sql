CREATE TABLE onboarding_profile_cache (
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    username text NOT NULL,
    profile jsonb NOT NULL CHECK (jsonb_typeof(profile) = 'object'),
    captured_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, ig_account_id, username),
    FOREIGN KEY (ig_account_id, user_id) REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
--> statement-breakpoint
CREATE TABLE profile_check_state (
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    next_allowed_at timestamptz NOT NULL,
    failure_reason text,
    PRIMARY KEY (user_id, ig_account_id),
    FOREIGN KEY (ig_account_id, user_id) REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
