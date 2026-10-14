ALTER TABLE "hermes_tool_calls" ADD COLUMN "target_id" uuid;--> statement-breakpoint
ALTER TABLE "hermes_tool_calls" ADD COLUMN "story_fingerprint" text;--> statement-breakpoint
ALTER TABLE "hermes_tool_calls" ADD COLUMN "generated_text_hash" text;--> statement-breakpoint
ALTER TABLE "hermes_tool_calls" ADD COLUMN "proposed_text" text;--> statement-breakpoint
ALTER TABLE "hermes_tool_calls" ADD CONSTRAINT "hermes_tool_calls_fingerprint_check" CHECK ("hermes_tool_calls"."story_fingerprint" is null or "hermes_tool_calls"."story_fingerprint" ~ '^[0-9a-f]{64}$');--> statement-breakpoint
ALTER TABLE "hermes_tool_calls" ADD CONSTRAINT "hermes_tool_calls_text_hash_check" CHECK ("hermes_tool_calls"."generated_text_hash" is null or "hermes_tool_calls"."generated_text_hash" ~ '^[0-9a-f]{64}$');