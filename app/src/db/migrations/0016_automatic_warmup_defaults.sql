ALTER TABLE "agent_configs" ALTER COLUMN "daily_like_limit" SET DEFAULT 20;--> statement-breakpoint
ALTER TABLE "agent_configs" ALTER COLUMN "daily_reply_limit" SET DEFAULT 5;--> statement-breakpoint
ALTER TABLE "agent_configs" ALTER COLUMN "daily_dm_limit" SET DEFAULT 2;--> statement-breakpoint
ALTER TABLE "agent_configs" ALTER COLUMN "daily_view_limit" SET DEFAULT 1000;