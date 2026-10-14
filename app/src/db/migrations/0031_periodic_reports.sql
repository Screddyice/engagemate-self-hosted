CREATE TABLE IF NOT EXISTS periodic_report_preferences (
 user_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
 timezone text NOT NULL DEFAULT 'America/Los_Angeles',
 enabled_at timestamptz NOT NULL DEFAULT now(),
 fortnightly_enabled boolean NOT NULL DEFAULT false,
 monthly_enabled boolean NOT NULL DEFAULT true
);
INSERT INTO periodic_report_preferences(user_id) SELECT id FROM users ON CONFLICT DO NOTHING;
--> statement-breakpoint
CREATE TABLE IF NOT EXISTS periodic_report_deliveries (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 kind text NOT NULL CHECK (kind IN ('fortnightly','monthly')),
 period_start date NOT NULL,
 period_end date NOT NULL CHECK (period_end > period_start),
 recipient text NOT NULL,
 payload jsonb NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','sending','sent','uncertain','failed')),
 provider_id text,
 error_code text,
 created_at timestamptz NOT NULL DEFAULT now(),
 attempted_at timestamptz,
 sent_at timestamptz,
 UNIQUE(user_id,kind,period_start)
);

--> statement-breakpoint
CREATE TABLE IF NOT EXISTS relationship_followback_observations (
 relationship_id uuid PRIMARY KEY REFERENCES relationship_follows(id) ON DELETE CASCADE,
 observed_at timestamptz NOT NULL
);

--> statement-breakpoint
ALTER TABLE account_growth_snapshots DROP CONSTRAINT IF EXISTS account_growth_snapshots_source_check;
ALTER TABLE account_growth_snapshots ADD CONSTRAINT account_growth_snapshots_source_check
 CHECK (source IN ('hypercrawl','account_session'));
