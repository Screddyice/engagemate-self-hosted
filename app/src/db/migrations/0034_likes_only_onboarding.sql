-- New configurations require explicit opt-in for every behavior except likes.
-- Preserve all saved choices, including established-account exemptions.
ALTER TABLE agent_configs ALTER COLUMN daily_reply_limit SET DEFAULT 0;
--> statement-breakpoint
ALTER TABLE agent_configs ALTER COLUMN daily_view_limit SET DEFAULT 0;
--> statement-breakpoint
ALTER TABLE agent_configs ALTER COLUMN daily_dm_limit SET DEFAULT 0;
--> statement-breakpoint
ALTER TABLE agent_configs ALTER COLUMN follow_after_engagement SET DEFAULT false;
--> statement-breakpoint
ALTER TABLE agent_configs ALTER COLUMN follow_reel_message_enabled SET DEFAULT false;
--> statement-breakpoint
ALTER TABLE agent_configs ALTER COLUMN auto_unfollow_enabled SET DEFAULT false;
