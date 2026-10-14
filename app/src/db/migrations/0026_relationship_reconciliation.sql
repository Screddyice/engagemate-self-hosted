ALTER TABLE relationship_follows ADD CONSTRAINT relationship_follows_scope_unique UNIQUE (id,ig_account_id,user_id);
--> statement-breakpoint
CREATE TABLE relationship_reconciliation_attempts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL,
 ig_account_id uuid NOT NULL,
 relationship_id uuid NOT NULL,
 worker_id text NOT NULL,
 reserved_at timestamptz NOT NULL,
 lease_expires_at timestamptz NOT NULL,
 finished_at timestamptz,
 next_check_at timestamptz,
 outcome text NOT NULL DEFAULT 'pending',
 failure_code text,
 CONSTRAINT relationship_reconciliation_scope_fk FOREIGN KEY (relationship_id,ig_account_id,user_id)
   REFERENCES relationship_follows(id,ig_account_id,user_id) ON DELETE CASCADE,
 CONSTRAINT relationship_reconciliation_outcome_check CHECK (outcome IN ('pending','follow_back','not_following_back','unknown','deferred')),
 CONSTRAINT relationship_reconciliation_lease_check CHECK (lease_expires_at > reserved_at),
 CONSTRAINT relationship_reconciliation_finished_check CHECK ((outcome = 'pending' AND finished_at IS NULL) OR (outcome <> 'pending' AND finished_at >= reserved_at))
);
--> statement-breakpoint
CREATE INDEX relationship_reconciliation_budget_idx ON relationship_reconciliation_attempts(user_id,ig_account_id,reserved_at);
--> statement-breakpoint
CREATE INDEX relationship_reconciliation_due_idx ON relationship_reconciliation_attempts(relationship_id,reserved_at);
