ALTER TABLE agent_configs
    ADD COLUMN follow_reel_message_enabled boolean NOT NULL DEFAULT false,
    ADD COLUMN follow_reel_message_enabled_at timestamptz,
    ADD COLUMN follow_reel_message_template text NOT NULL DEFAULT 'Hey, hope you''re doing well! Wanted to ask for your support on my latest reel. A like or comment would go a long way as I''m growing my account. Appreciate you!',
    ADD CONSTRAINT follow_reel_template_length CHECK (char_length(follow_reel_message_template) BETWEEN 1 AND 500);
--> statement-breakpoint
CREATE TABLE follow_reel_rotation (
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    next_slot bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id,ig_account_id),
    CONSTRAINT follow_reel_rotation_account_fk FOREIGN KEY (ig_account_id,user_id)
        REFERENCES ig_accounts(id,user_id) ON DELETE CASCADE,
    CONSTRAINT follow_reel_rotation_slot_check CHECK (next_slot >= 0)
);
--> statement-breakpoint
CREATE TABLE follow_reel_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    relationship_follow_id uuid NOT NULL UNIQUE REFERENCES relationship_follows(id) ON DELETE CASCADE,
    user_id uuid NOT NULL,
    ig_account_id uuid NOT NULL,
    target_id uuid NOT NULL,
    recipient_id text NOT NULL,
    rotation_slot bigint,
    reel_url text,
    body text,
    status text NOT NULL DEFAULT 'pending',
    claimed_by text,
    lease_token uuid,
    lease_expires_at timestamptz,
    reserved_at timestamptz,
    claim_count integer NOT NULL DEFAULT 0 CHECK (claim_count >= 0),
    attempted_at timestamptz,
    sent_at timestamptz,
    next_attempt_at timestamptz,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT follow_reel_messages_account_fk FOREIGN KEY (ig_account_id,user_id)
        REFERENCES ig_accounts(id,user_id) ON DELETE CASCADE,
    CONSTRAINT follow_reel_messages_target_fk FOREIGN KEY (target_id,ig_account_id,user_id)
        REFERENCES targets(id,ig_account_id,user_id) ON DELETE CASCADE,
    CONSTRAINT follow_reel_messages_recipient_check CHECK (recipient_id ~ '^[0-9]{1,32}$'),
    CONSTRAINT follow_reel_messages_status_check CHECK (status IN ('pending','running','sent','uncertain','cancelled')),
    CONSTRAINT follow_reel_messages_slot_check CHECK (rotation_slot IS NULL OR rotation_slot >= 0),
    CONSTRAINT follow_reel_messages_ready_check CHECK (status <> 'sent' OR (rotation_slot IS NOT NULL AND reel_url IS NOT NULL AND body IS NOT NULL)),
    CONSTRAINT follow_reel_messages_receipt_check CHECK (status <> 'sent' OR (attempted_at IS NOT NULL AND sent_at IS NOT NULL)),
    CONSTRAINT follow_reel_messages_lease_check CHECK (
        (status='running' AND claimed_by IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status<>'running' AND claimed_by IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
    ),
    CONSTRAINT follow_reel_messages_evidence_check CHECK (jsonb_typeof(evidence)='object')
);
--> statement-breakpoint
CREATE INDEX follow_reel_messages_pending_idx ON follow_reel_messages (created_at,id)
    WHERE status='pending';
--> statement-breakpoint
CREATE INDEX follow_reel_messages_account_reserved_idx ON follow_reel_messages (user_id,ig_account_id,reserved_at)
    WHERE reserved_at IS NOT NULL;
