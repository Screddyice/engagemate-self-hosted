ALTER TABLE agent_configs
    ADD COLUMN follow_after_engagement boolean NOT NULL DEFAULT false,
    ADD COLUMN auto_unfollow_enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN daily_follow_limit integer NOT NULL DEFAULT 3,
    ADD COLUMN follow_enabled_at timestamptz,
    ADD CONSTRAINT agent_configs_daily_follow_limit_check CHECK (daily_follow_limit BETWEEN 0 AND 20);
--> statement-breakpoint
CREATE TABLE relationship_follows (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ig_account_id uuid NOT NULL,
    target_id uuid NOT NULL,
    owner_id text NOT NULL,
    prerequisite_engagement_id uuid NOT NULL,
    status text NOT NULL DEFAULT 'follow_pending',
    confirmed_follow_at timestamptz,
    unfollow_due_at timestamptz,
    unfollowed_at timestamptz,
    claimed_by text,
    lease_token uuid,
    lease_expires_at timestamptz,
    attempted_at timestamptz,
    follow_attempted_at timestamptz,
    unfollow_attempted_at timestamptz,
    follow_reserved_at timestamptz,
    unfollow_reserved_at timestamptz,
    next_attempt_at timestamptz,
    last_error text,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT relationship_follows_account_user_fk FOREIGN KEY (ig_account_id,user_id)
        REFERENCES ig_accounts(id,user_id) ON DELETE CASCADE,
    CONSTRAINT relationship_follows_target_scope_fk FOREIGN KEY (target_id,ig_account_id,user_id)
        REFERENCES targets(id,ig_account_id,user_id) ON DELETE CASCADE,
    CONSTRAINT relationship_follows_engagement_scope_fk FOREIGN KEY (prerequisite_engagement_id,ig_account_id,user_id)
        REFERENCES story_engagements(id,ig_account_id,user_id) ON DELETE CASCADE,
    CONSTRAINT relationship_follows_account_owner_unique UNIQUE (ig_account_id,owner_id),
    CONSTRAINT relationship_follows_owner_id_check CHECK (owner_id ~ '^[0-9]{1,32}$'),
    CONSTRAINT relationship_follows_status_check CHECK (status IN (
        'follow_pending','follow_running','following','unfollow_running','unfollowed','skipped','uncertain','cancelled'
    )),
    CONSTRAINT relationship_follows_due_check CHECK (
        (confirmed_follow_at IS NULL AND unfollow_due_at IS NULL)
        OR (confirmed_follow_at IS NOT NULL AND unfollow_due_at IS NOT NULL
            AND unfollow_due_at = confirmed_follow_at + interval '120 hours')
    ),
    CONSTRAINT relationship_follows_owned_check CHECK (
        status NOT IN ('following','unfollow_running','unfollowed') OR confirmed_follow_at IS NOT NULL
    ),
    CONSTRAINT relationship_follows_lease_check CHECK (
        (status IN ('follow_running','unfollow_running') AND claimed_by IS NOT NULL
            AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status NOT IN ('follow_running','unfollow_running') AND claimed_by IS NULL
            AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CONSTRAINT relationship_follows_evidence_check CHECK (jsonb_typeof(evidence) = 'object')
);
--> statement-breakpoint
CREATE INDEX relationship_follows_due_idx ON relationship_follows (unfollow_due_at,next_attempt_at,created_at)
    WHERE status IN ('follow_pending','following');
--> statement-breakpoint
CREATE INDEX relationship_follows_account_status_idx ON relationship_follows (user_id,ig_account_id,status);
--> statement-breakpoint
ALTER TABLE instagram_execution_controls DROP CONSTRAINT instagram_execution_controls_action_check,
    ADD CONSTRAINT instagram_execution_controls_action_check
    CHECK (action IS NULL OR action IN ('view','like','reply','dm','follow','unfollow'));
