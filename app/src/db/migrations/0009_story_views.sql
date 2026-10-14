ALTER TABLE "story_engagements" DROP CONSTRAINT "story_engagements_action_check";--> statement-breakpoint
ALTER TABLE "agent_configs" ADD COLUMN "daily_view_limit" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "story_engagements" ADD CONSTRAINT "story_engagements_action_check" CHECK ("story_engagements"."action" in ('like', 'reply', 'dm', 'view'));