CREATE TABLE "agent_configs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"social_account_id" uuid NOT NULL,
	"voice" text,
	"bio_context" text,
	"tone" text,
	"comment_style" text,
	"do_not" text[],
	"content_mix" jsonb,
	"daily_posts_limit" integer DEFAULT 2 NOT NULL,
	"daily_comments_limit" integer DEFAULT 3 NOT NULL,
	"daily_likes_limit" integer DEFAULT 10 NOT NULL,
	"daily_replies_limit" integer DEFAULT 5 NOT NULL,
	"posting_times" jsonb,
	"timezone" text DEFAULT 'America/New_York' NOT NULL,
	"auto_mode" boolean DEFAULT false NOT NULL,
	"posting_enabled" boolean DEFAULT true NOT NULL,
	"commenting_enabled" boolean DEFAULT true NOT NULL,
	"replying_enabled" boolean DEFAULT true NOT NULL,
	"engaging_enabled" boolean DEFAULT true NOT NULL,
	"created_at" timestamp with time zone DEFAULT now(),
	"updated_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "agent_runs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"social_account_id" uuid NOT NULL,
	"run_type" text NOT NULL,
	"status" text NOT NULL,
	"started_at" timestamp with time zone DEFAULT now(),
	"completed_at" timestamp with time zone,
	"posts_created" integer DEFAULT 0 NOT NULL,
	"comments_created" integer DEFAULT 0 NOT NULL,
	"engagements" integer DEFAULT 0 NOT NULL,
	"errors" jsonb,
	"summary" text
);
--> statement-breakpoint
CREATE TABLE "ai_insights" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"org_id" uuid NOT NULL,
	"insight_type" text NOT NULL,
	"content" text NOT NULL,
	"platforms" text[],
	"period_start" date,
	"period_end" date,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "analytics_snapshots" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"social_account_id" uuid NOT NULL,
	"platform" text NOT NULL,
	"snapshot_date" date NOT NULL,
	"followers" integer,
	"following" integer,
	"impressions" integer,
	"engagement_rate" numeric(5, 2),
	"likes_received" integer,
	"comments_received" integer,
	"shares" integer,
	"top_post_id" text,
	"top_post_engagement" integer,
	"raw_data" jsonb,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "content_queue" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"social_account_id" uuid NOT NULL,
	"content_type" text NOT NULL,
	"platform" text NOT NULL,
	"text" text NOT NULL,
	"thread_parts" jsonb,
	"target_url" text,
	"target_post_id" text,
	"category" text,
	"news_ref" text,
	"status" text DEFAULT 'pending' NOT NULL,
	"scheduled_for" timestamp with time zone,
	"posted_at" timestamp with time zone,
	"platform_post_id" text,
	"rejection_reason" text,
	"reviewed_by" uuid,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "engagement_log" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"social_account_id" uuid NOT NULL,
	"action_type" text NOT NULL,
	"platform" text NOT NULL,
	"target_post_id" text,
	"target_user" text,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "organizations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"name" text NOT NULL,
	"slug" text NOT NULL,
	"plan" text DEFAULT 'free' NOT NULL,
	"stripe_subscription_id" text,
	"created_at" timestamp with time zone DEFAULT now(),
	CONSTRAINT "organizations_slug_unique" UNIQUE("slug")
);
--> statement-breakpoint
CREATE TABLE "repurpose_destinations" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"workflow_id" uuid NOT NULL,
	"dest_account_id" uuid NOT NULL,
	"dest_platform" text NOT NULL,
	"delay_minutes" integer DEFAULT 0 NOT NULL,
	"tone_override" text,
	"format_instructions" text,
	"include_media" boolean DEFAULT true NOT NULL,
	"hashtag_override" text[],
	"max_length" integer,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "repurpose_workflows" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"org_id" uuid NOT NULL,
	"name" text NOT NULL,
	"description" text,
	"source_account_id" uuid NOT NULL,
	"source_platform" text NOT NULL,
	"trigger_type" text DEFAULT 'auto' NOT NULL,
	"is_active" boolean DEFAULT true NOT NULL,
	"approval_required" boolean DEFAULT true NOT NULL,
	"created_at" timestamp with time zone DEFAULT now(),
	"updated_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "repurposed_content" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"workflow_id" uuid NOT NULL,
	"source_content_id" uuid,
	"source_platform_post_id" text,
	"source_text" text,
	"source_media_urls" jsonb,
	"dest_account_id" uuid NOT NULL,
	"dest_platform" text NOT NULL,
	"transformed_text" text,
	"transformed_media_urls" jsonb,
	"status" text DEFAULT 'pending' NOT NULL,
	"scheduled_for" timestamp with time zone,
	"posted_at" timestamp with time zone,
	"platform_post_id" text,
	"reviewed_by" uuid,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "social_accounts" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"org_id" uuid NOT NULL,
	"platform" text NOT NULL,
	"platform_user_id" text,
	"username" text NOT NULL,
	"display_name" text,
	"auth_type" text NOT NULL,
	"credentials" jsonb NOT NULL,
	"status" text DEFAULT 'active' NOT NULL,
	"last_auth_check" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "targeting_config" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"agent_config_id" uuid NOT NULL,
	"subreddits" text[],
	"hashtags" text[],
	"keywords" text[],
	"target_accounts" text[],
	"competitor_accounts" text[],
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "usage_records" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"org_id" uuid NOT NULL,
	"metric" text NOT NULL,
	"quantity" integer NOT NULL,
	"period_start" date NOT NULL,
	"period_end" date NOT NULL,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "users" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"clerk_id" text NOT NULL,
	"email" text NOT NULL,
	"name" text,
	"org_id" uuid,
	"role" text DEFAULT 'owner' NOT NULL,
	"plan" text DEFAULT 'free' NOT NULL,
	"stripe_customer_id" text,
	"created_at" timestamp with time zone DEFAULT now(),
	"updated_at" timestamp with time zone DEFAULT now(),
	CONSTRAINT "users_clerk_id_unique" UNIQUE("clerk_id")
);
--> statement-breakpoint
CREATE TABLE "video_clips" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"org_id" uuid NOT NULL,
	"source_url" text NOT NULL,
	"source_platform" text NOT NULL,
	"original_duration_sec" integer,
	"status" text DEFAULT 'processing' NOT NULL,
	"clips" jsonb,
	"captions_url" text,
	"watermark_removed" boolean DEFAULT false NOT NULL,
	"backup_url" text,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
ALTER TABLE "agent_configs" ADD CONSTRAINT "agent_configs_social_account_id_social_accounts_id_fk" FOREIGN KEY ("social_account_id") REFERENCES "public"."social_accounts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "agent_runs" ADD CONSTRAINT "agent_runs_social_account_id_social_accounts_id_fk" FOREIGN KEY ("social_account_id") REFERENCES "public"."social_accounts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "ai_insights" ADD CONSTRAINT "ai_insights_org_id_organizations_id_fk" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "analytics_snapshots" ADD CONSTRAINT "analytics_snapshots_social_account_id_social_accounts_id_fk" FOREIGN KEY ("social_account_id") REFERENCES "public"."social_accounts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "content_queue" ADD CONSTRAINT "content_queue_social_account_id_social_accounts_id_fk" FOREIGN KEY ("social_account_id") REFERENCES "public"."social_accounts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "content_queue" ADD CONSTRAINT "content_queue_reviewed_by_users_id_fk" FOREIGN KEY ("reviewed_by") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "engagement_log" ADD CONSTRAINT "engagement_log_social_account_id_social_accounts_id_fk" FOREIGN KEY ("social_account_id") REFERENCES "public"."social_accounts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "repurpose_destinations" ADD CONSTRAINT "repurpose_destinations_workflow_id_repurpose_workflows_id_fk" FOREIGN KEY ("workflow_id") REFERENCES "public"."repurpose_workflows"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "repurpose_destinations" ADD CONSTRAINT "repurpose_destinations_dest_account_id_social_accounts_id_fk" FOREIGN KEY ("dest_account_id") REFERENCES "public"."social_accounts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "repurpose_workflows" ADD CONSTRAINT "repurpose_workflows_org_id_organizations_id_fk" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "repurpose_workflows" ADD CONSTRAINT "repurpose_workflows_source_account_id_social_accounts_id_fk" FOREIGN KEY ("source_account_id") REFERENCES "public"."social_accounts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "repurposed_content" ADD CONSTRAINT "repurposed_content_workflow_id_repurpose_workflows_id_fk" FOREIGN KEY ("workflow_id") REFERENCES "public"."repurpose_workflows"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "repurposed_content" ADD CONSTRAINT "repurposed_content_source_content_id_content_queue_id_fk" FOREIGN KEY ("source_content_id") REFERENCES "public"."content_queue"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "repurposed_content" ADD CONSTRAINT "repurposed_content_dest_account_id_social_accounts_id_fk" FOREIGN KEY ("dest_account_id") REFERENCES "public"."social_accounts"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "repurposed_content" ADD CONSTRAINT "repurposed_content_reviewed_by_users_id_fk" FOREIGN KEY ("reviewed_by") REFERENCES "public"."users"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "social_accounts" ADD CONSTRAINT "social_accounts_org_id_organizations_id_fk" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "targeting_config" ADD CONSTRAINT "targeting_config_agent_config_id_agent_configs_id_fk" FOREIGN KEY ("agent_config_id") REFERENCES "public"."agent_configs"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "usage_records" ADD CONSTRAINT "usage_records_org_id_organizations_id_fk" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "users" ADD CONSTRAINT "users_org_id_organizations_id_fk" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "video_clips" ADD CONSTRAINT "video_clips_org_id_organizations_id_fk" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "analytics_account_date_idx" ON "analytics_snapshots" USING btree ("social_account_id","snapshot_date");