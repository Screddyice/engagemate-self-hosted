-- Convert legacy SaaS metadata to a local owner workspace. Instagram data is retained.
ALTER TABLE users RENAME COLUMN clerk_id TO owner_key;
--> statement-breakpoint
ALTER TABLE users ALTER COLUMN plan SET DEFAULT 'self_hosted';
UPDATE users SET plan = 'self_hosted';
--> statement-breakpoint
ALTER TABLE users DROP COLUMN stripe_customer_id, DROP COLUMN stripe_subscription_id,
 DROP COLUMN subscription_status, DROP COLUMN stripe_current_period_start,
 DROP COLUMN stripe_current_period_end, DROP COLUMN overage_enabled,
 DROP COLUMN overage_cap_cents, DROP COLUMN purchased_accounts;
--> statement-breakpoint
ALTER TABLE engagement_usage_events DROP COLUMN stripe_event_id, DROP COLUMN stripe_reported_at;
--> statement-breakpoint
DROP TABLE capacity_waitlist;
