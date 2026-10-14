ALTER TABLE agent_configs ALTER COLUMN auto_unfollow_enabled SET DEFAULT false;
--> statement-breakpoint
ALTER TABLE agent_configs ALTER COLUMN daily_dm_limit SET DEFAULT 0;
--> statement-breakpoint
UPDATE agent_configs SET daily_dm_limit = 0 WHERE daily_dm_limit <> 0;
--> statement-breakpoint
ALTER TABLE agent_configs ALTER COLUMN daily_reply_limit SET DEFAULT 10;
