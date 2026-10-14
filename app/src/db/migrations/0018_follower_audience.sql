ALTER TABLE brand_profiles ADD COLUMN follower_audience_approved_at timestamptz;
--> statement-breakpoint
CREATE TABLE follower_source_states (
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    source_id uuid NOT NULL REFERENCES brand_reference_accounts(id) ON DELETE CASCADE,
    cursor text,
    exhausted boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, ig_account_id, source_id),
    FOREIGN KEY (ig_account_id, user_id) REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE
);
--> statement-breakpoint
CREATE TABLE follower_candidates (
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    owner_id text NOT NULL,
    handle text NOT NULL,
    status text NOT NULL DEFAULT 'new',
    last_checked_at timestamptz,
    next_check_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, ig_account_id, owner_id),
    FOREIGN KEY (ig_account_id, user_id) REFERENCES ig_accounts(id, user_id) ON DELETE CASCADE,
    CONSTRAINT follower_candidates_status_check CHECK (status IN ('new', 'excluded', 'no_story', 'unknown', 'available'))
);
--> statement-breakpoint
CREATE INDEX follower_candidates_due_idx ON follower_candidates(user_id, ig_account_id, next_check_at, created_at);
--> statement-breakpoint
CREATE TABLE follower_candidate_sources (
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    owner_id text NOT NULL,
    source_id uuid NOT NULL,
    PRIMARY KEY (user_id, ig_account_id, owner_id, source_id),
    FOREIGN KEY (user_id, ig_account_id, owner_id) REFERENCES follower_candidates(user_id, ig_account_id, owner_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id, ig_account_id, source_id) REFERENCES follower_source_states(user_id, ig_account_id, source_id) ON DELETE CASCADE
);
--> statement-breakpoint
ALTER TABLE hermes_execution_runs
    ADD COLUMN discovery_report jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD CONSTRAINT hermes_execution_runs_discovery_report_check CHECK (jsonb_typeof(discovery_report) = 'object');
