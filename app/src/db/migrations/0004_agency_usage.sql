CREATE TABLE "engagement_usage_events" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"engagement_id" uuid NOT NULL,
	"surface" text DEFAULT 'story' NOT NULL,
	"action" text NOT NULL,
	"quantity" integer DEFAULT 1 NOT NULL,
	"status" text DEFAULT 'reserved' NOT NULL,
	"reserved_at" timestamp with time zone DEFAULT now() NOT NULL,
	"finalized_at" timestamp with time zone,
	"occurred_at" timestamp with time zone,
	"stripe_event_id" text,
	"stripe_reported_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "engagement_usage_events_engagement_id_unique" UNIQUE("engagement_id"),
	CONSTRAINT "engagement_usage_events_surface_check" CHECK ("engagement_usage_events"."surface" in ('story', 'post')),
	CONSTRAINT "engagement_usage_events_action_check" CHECK ("engagement_usage_events"."action" in ('like', 'reply', 'dm')),
	CONSTRAINT "engagement_usage_events_status_check" CHECK ("engagement_usage_events"."status" in ('reserved', 'billable', 'released')),
	CONSTRAINT "engagement_usage_events_quantity_check" CHECK ("engagement_usage_events"."quantity" = 1)
);
--> statement-breakpoint
ALTER TABLE "run_summaries" ADD COLUMN "input_tokens" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "run_summaries" ADD COLUMN "output_tokens" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "run_summaries" ADD COLUMN "images_analyzed" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "run_summaries" ADD COLUMN "video_frames_analyzed" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "run_summaries" ADD COLUMN "retries" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "run_summaries" ADD COLUMN "runtime_ms" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "users" ADD COLUMN "stripe_current_period_start" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "users" ADD COLUMN "stripe_current_period_end" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "users" ADD COLUMN "overage_enabled" boolean DEFAULT false NOT NULL;--> statement-breakpoint
ALTER TABLE "users" ADD COLUMN "overage_cap_cents" integer DEFAULT 0 NOT NULL;--> statement-breakpoint
ALTER TABLE "engagement_usage_events" ADD CONSTRAINT "engagement_usage_events_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "engagement_usage_events" ADD CONSTRAINT "engagement_usage_events_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "story_engagements" ADD CONSTRAINT "story_engagements_identity_unique" UNIQUE("id","ig_account_id","user_id");--> statement-breakpoint
ALTER TABLE "engagement_usage_events" ADD CONSTRAINT "engagement_usage_events_engagement_scope_fk" FOREIGN KEY ("engagement_id","ig_account_id","user_id") REFERENCES "public"."story_engagements"("id","ig_account_id","user_id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "engagement_usage_events_user_occurred_status_idx" ON "engagement_usage_events" USING btree ("user_id","occurred_at","status");--> statement-breakpoint
CREATE UNIQUE INDEX "run_summaries_job_idx" ON "run_summaries" USING btree ("job_id");--> statement-breakpoint
ALTER TABLE "run_summaries" ADD CONSTRAINT "run_summaries_cost_counters_check" CHECK ("run_summaries"."input_tokens" >= 0
      and "run_summaries"."output_tokens" >= 0
      and "run_summaries"."images_analyzed" >= 0
      and "run_summaries"."video_frames_analyzed" >= 0
      and "run_summaries"."retries" >= 0
      and "run_summaries"."runtime_ms" >= 0);--> statement-breakpoint
ALTER TABLE "users" ADD CONSTRAINT "users_overage_cap_cents_check" CHECK ("users"."overage_cap_cents" between 0 and 100000);
