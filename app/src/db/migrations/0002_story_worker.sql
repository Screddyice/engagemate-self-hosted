DO $migration$
DECLARE
	guarded_table text;
has_rows boolean;
BEGIN
	LOCK TABLE
		"agent_runs",
		"ai_insights",
		"analytics_snapshots",
		"content_pillars",
		"content_queue",
		"engagement_log",
		"organizations",
		"repurpose_destinations",
		"repurpose_workflows",
		"repurposed_content",
		"social_accounts",
		"targeting_config",
		"video_clips",
		"agent_configs",
		"usage_records",
		"users"
	IN ACCESS EXCLUSIVE MODE;
FOREACH guarded_table IN ARRAY ARRAY[
		'agent_runs',
		'ai_insights',
		'analytics_snapshots',
		'content_pillars',
		'content_queue',
		'engagement_log',
		'organizations',
		'repurpose_destinations',
		'repurpose_workflows',
		'repurposed_content',
		'social_accounts',
		'targeting_config',
		'video_clips',
		'agent_configs',
		'usage_records'
	]
	LOOP
		EXECUTE format(
			'SELECT EXISTS (SELECT 1 FROM %I)',
			guarded_table
		) INTO has_rows;
IF has_rows THEN
			RAISE EXCEPTION
				'0002_story_worker requires empty legacy table "%";
back up and migrate its rows before retrying',
				guarded_table;
END IF;
END LOOP;
CREATE TABLE "account_health_events" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"event_type" text NOT NULL,
	"severity" text NOT NULL,
	"evidence" jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE "fingerprints" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"user_agent" text NOT NULL,
	"viewport_width" integer NOT NULL,
	"viewport_height" integer NOT NULL,
	"timezone" text NOT NULL,
	"locale" text NOT NULL,
	"color_scheme" text NOT NULL,
	"canvas_seed" text NOT NULL,
	"webgl_seed" text NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE "ig_accounts" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"username" text NOT NULL,
	"ig_user_id" text,
	"login_country" text,
	"login_region" text,
	"login_city" text,
	"follower_count" integer,
	"warmup_started_at" timestamp with time zone,
	"health_status" text DEFAULT 'active' NOT NULL,
	"last_healthy_at" timestamp with time zone,
	"paused_at" timestamp with time zone,
	"pause_reason" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "ig_accounts_id_user_unique" UNIQUE("id","user_id"),
	CONSTRAINT "ig_accounts_health_status_check" CHECK ("ig_accounts"."health_status" in ('active', 'degraded', 'paused', 'challenged', 'banned'))
);
CREATE TABLE "ig_sessions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"encrypted_cookie_blob" text NOT NULL,
	"encryption_version" integer NOT NULL,
	"captured_user_agent" text NOT NULL,
	"captured_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_validated_at" timestamp with time zone,
	"expires_at" timestamp with time zone
);
CREATE TABLE "jobs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"schedule_key" text NOT NULL,
	"scheduled_for" timestamp with time zone NOT NULL,
	"status" text DEFAULT 'queued' NOT NULL,
	"claimed_by" text,
	"lease_expires_at" timestamp with time zone,
	"heartbeat_at" timestamp with time zone,
	"attempts" integer DEFAULT 0 NOT NULL,
	"last_error" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "jobs_schedule_key_unique" UNIQUE("schedule_key"),
	CONSTRAINT "jobs_identity_unique" UNIQUE("id","ig_account_id","user_id"),
	CONSTRAINT "jobs_status_check" CHECK ("jobs"."status" in ('queued', 'running', 'completed', 'failed', 'cancelled'))
);
CREATE TABLE "proxies" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"provider" text NOT NULL,
	"provider_reference" text NOT NULL,
	"proxy_type" text NOT NULL,
	"country" text NOT NULL,
	"region" text,
	"city" text,
	"host" text NOT NULL,
	"port" integer NOT NULL,
	"encrypted_username" text NOT NULL,
	"encrypted_password" text NOT NULL,
	"health_status" text DEFAULT 'active' NOT NULL,
	"last_checked_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "proxies_health_status_check" CHECK ("proxies"."health_status" in ('active', 'degraded', 'paused', 'challenged', 'banned'))
);
CREATE TABLE "proxy_bindings" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"proxy_id" uuid NOT NULL,
	"bound_at" timestamp with time zone DEFAULT now() NOT NULL,
	"released_at" timestamp with time zone
);
CREATE TABLE "run_summaries" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"job_id" uuid NOT NULL,
	"opportunities_found" integer DEFAULT 0 NOT NULL,
	"likes_sent" integer DEFAULT 0 NOT NULL,
	"replies_sent" integer DEFAULT 0 NOT NULL,
	"dms_sent" integer DEFAULT 0 NOT NULL,
	"skipped" integer DEFAULT 0 NOT NULL,
	"gated" integer DEFAULT 0 NOT NULL,
	"failed" integer DEFAULT 0 NOT NULL,
	"follower_count_before" integer,
	"follower_count_after" integer,
	"started_at" timestamp with time zone DEFAULT now() NOT NULL,
	"completed_at" timestamp with time zone
);
CREATE TABLE "story_engagements" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"target_id" uuid NOT NULL,
	"story_fingerprint" text NOT NULL,
	"action" text NOT NULL,
	"generated_text" text,
	"model_used" text,
	"rationale" text,
	"confidence" numeric(5, 4),
	"status" text NOT NULL,
	"failure_code" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"executed_at" timestamp with time zone,
	CONSTRAINT "story_engagements_action_check" CHECK ("story_engagements"."action" in ('like', 'reply', 'dm')),
	CONSTRAINT "story_engagements_status_check" CHECK ("story_engagements"."status" in ('sent', 'skipped', 'gated', 'failed'))
);
CREATE TABLE "targets" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"username" text NOT NULL,
	"ig_user_id" text,
	"tier" text NOT NULL,
	"source" text NOT NULL,
	"enabled" boolean DEFAULT true NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "targets_identity_unique" UNIQUE("id","ig_account_id","user_id"),
	CONSTRAINT "targets_tier_check" CHECK ("targets"."tier" in ('vip', 'broad', 'discovery'))
);
ALTER TABLE "agent_runs" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "ai_insights" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "analytics_snapshots" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "content_pillars" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "content_queue" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "engagement_log" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "organizations" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "repurpose_destinations" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "repurpose_workflows" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "repurposed_content" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "social_accounts" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "targeting_config" DISABLE ROW LEVEL SECURITY;
ALTER TABLE "video_clips" DISABLE ROW LEVEL SECURITY;
DROP TABLE "agent_runs" CASCADE;
DROP TABLE "ai_insights" CASCADE;
DROP TABLE "analytics_snapshots" CASCADE;
DROP TABLE "content_pillars" CASCADE;
DROP TABLE "content_queue" CASCADE;
DROP TABLE "engagement_log" CASCADE;
DROP TABLE "organizations" CASCADE;
DROP TABLE "repurpose_destinations" CASCADE;
DROP TABLE "repurpose_workflows" CASCADE;
DROP TABLE "repurposed_content" CASCADE;
DROP TABLE "social_accounts" CASCADE;
DROP TABLE "targeting_config" CASCADE;
DROP TABLE "video_clips" CASCADE;
ALTER TABLE "agent_configs" DROP CONSTRAINT IF EXISTS "agent_configs_social_account_id_social_accounts_id_fk";
ALTER TABLE "usage_records" DROP CONSTRAINT IF EXISTS "usage_records_org_id_organizations_id_fk";
ALTER TABLE "users" DROP CONSTRAINT IF EXISTS "users_org_id_organizations_id_fk";
ALTER TABLE "agent_configs" ALTER COLUMN "timezone" SET DEFAULT 'UTC';
ALTER TABLE "agent_configs" ALTER COLUMN "created_at" SET NOT NULL;
ALTER TABLE "agent_configs" ALTER COLUMN "updated_at" SET NOT NULL;
ALTER TABLE "usage_records" ALTER COLUMN "period_start" SET DATA TYPE timestamp with time zone;
ALTER TABLE "usage_records" ALTER COLUMN "period_end" SET DATA TYPE timestamp with time zone;
ALTER TABLE "usage_records" ALTER COLUMN "created_at" SET NOT NULL;
ALTER TABLE "users" ALTER COLUMN "plan" SET DEFAULT 'starter';
UPDATE "users"
SET
	"created_at" = COALESCE("created_at", now()),
	"updated_at" = COALESCE("updated_at", now())
WHERE "created_at" IS NULL OR "updated_at" IS NULL;
ALTER TABLE "users" ALTER COLUMN "created_at" SET NOT NULL;
ALTER TABLE "users" ALTER COLUMN "updated_at" SET NOT NULL;
ALTER TABLE "agent_configs" ADD COLUMN "user_id" uuid NOT NULL;
ALTER TABLE "agent_configs" ADD COLUMN "ig_account_id" uuid NOT NULL;
ALTER TABLE "agent_configs" ADD COLUMN "enabled" boolean DEFAULT false NOT NULL;
ALTER TABLE "agent_configs" ADD COLUMN "schedule_hour" integer DEFAULT 9 NOT NULL;
ALTER TABLE "agent_configs" ADD COLUMN "daily_like_limit" integer DEFAULT 0 NOT NULL;
ALTER TABLE "agent_configs" ADD COLUMN "daily_reply_limit" integer DEFAULT 0 NOT NULL;
ALTER TABLE "agent_configs" ADD COLUMN "daily_dm_limit" integer DEFAULT 0 NOT NULL;
ALTER TABLE "agent_configs" ADD COLUMN "growth_goal" integer;
ALTER TABLE "agent_configs" ADD COLUMN "persona" text;
ALTER TABLE "agent_configs" ADD COLUMN "vip_escalation_enabled" boolean DEFAULT false NOT NULL;
ALTER TABLE "usage_records" ADD COLUMN "user_id" uuid NOT NULL;
ALTER TABLE "users" ADD COLUMN "stripe_subscription_id" text;
ALTER TABLE "users" ADD COLUMN "subscription_status" text;
ALTER TABLE "account_health_events" ADD CONSTRAINT "account_health_events_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "account_health_events" ADD CONSTRAINT "account_health_events_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "fingerprints" ADD CONSTRAINT "fingerprints_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "fingerprints" ADD CONSTRAINT "fingerprints_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "ig_accounts" ADD CONSTRAINT "ig_accounts_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "ig_sessions" ADD CONSTRAINT "ig_sessions_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "ig_sessions" ADD CONSTRAINT "ig_sessions_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "jobs" ADD CONSTRAINT "jobs_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "jobs" ADD CONSTRAINT "jobs_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "proxy_bindings" ADD CONSTRAINT "proxy_bindings_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "proxy_bindings" ADD CONSTRAINT "proxy_bindings_proxy_id_proxies_id_fk" FOREIGN KEY ("proxy_id") REFERENCES "public"."proxies"("id") ON DELETE no action ON UPDATE no action;
ALTER TABLE "proxy_bindings" ADD CONSTRAINT "proxy_bindings_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "run_summaries" ADD CONSTRAINT "run_summaries_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "run_summaries" ADD CONSTRAINT "run_summaries_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "run_summaries" ADD CONSTRAINT "run_summaries_job_scope_fk" FOREIGN KEY ("job_id","ig_account_id","user_id") REFERENCES "public"."jobs"("id","ig_account_id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "story_engagements" ADD CONSTRAINT "story_engagements_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "story_engagements" ADD CONSTRAINT "story_engagements_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "story_engagements" ADD CONSTRAINT "story_engagements_target_scope_fk" FOREIGN KEY ("target_id","ig_account_id","user_id") REFERENCES "public"."targets"("id","ig_account_id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "targets" ADD CONSTRAINT "targets_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "targets" ADD CONSTRAINT "targets_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
CREATE UNIQUE INDEX "fingerprints_ig_account_idx" ON "fingerprints" USING btree ("ig_account_id");
CREATE UNIQUE INDEX "ig_accounts_user_username_idx" ON "ig_accounts" USING btree ("user_id","username");
CREATE UNIQUE INDEX "ig_accounts_user_ig_user_id_idx" ON "ig_accounts" USING btree ("user_id","ig_user_id") WHERE "ig_accounts"."ig_user_id" is not null;
CREATE INDEX "jobs_status_scheduled_for_idx" ON "jobs" USING btree ("status","scheduled_for");
CREATE UNIQUE INDEX "proxy_bindings_active_account_idx" ON "proxy_bindings" USING btree ("ig_account_id") WHERE "proxy_bindings"."released_at" is null;
CREATE UNIQUE INDEX "proxy_bindings_active_proxy_idx" ON "proxy_bindings" USING btree ("proxy_id") WHERE "proxy_bindings"."released_at" is null;
CREATE UNIQUE INDEX "story_engagements_dedupe_idx" ON "story_engagements" USING btree ("ig_account_id","story_fingerprint","action");
CREATE UNIQUE INDEX "targets_account_username_idx" ON "targets" USING btree ("ig_account_id","username");
ALTER TABLE "agent_configs" ADD CONSTRAINT "agent_configs_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "agent_configs" ADD CONSTRAINT "agent_configs_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;
ALTER TABLE "usage_records" ADD CONSTRAINT "usage_records_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;
CREATE UNIQUE INDEX "agent_configs_ig_account_idx" ON "agent_configs" USING btree ("ig_account_id");
ALTER TABLE "agent_configs" DROP COLUMN "social_account_id";
ALTER TABLE "agent_configs" DROP COLUMN "voice";
ALTER TABLE "agent_configs" DROP COLUMN "bio_context";
ALTER TABLE "agent_configs" DROP COLUMN "tone";
ALTER TABLE "agent_configs" DROP COLUMN "comment_style";
ALTER TABLE "agent_configs" DROP COLUMN "do_not";
ALTER TABLE "agent_configs" DROP COLUMN "content_mix";
ALTER TABLE "agent_configs" DROP COLUMN "daily_posts_limit";
ALTER TABLE "agent_configs" DROP COLUMN "daily_comments_limit";
ALTER TABLE "agent_configs" DROP COLUMN "daily_likes_limit";
ALTER TABLE "agent_configs" DROP COLUMN "daily_replies_limit";
ALTER TABLE "agent_configs" DROP COLUMN "posting_times";
ALTER TABLE "agent_configs" DROP COLUMN "auto_mode";
ALTER TABLE "agent_configs" DROP COLUMN "posting_enabled";
ALTER TABLE "agent_configs" DROP COLUMN "commenting_enabled";
ALTER TABLE "agent_configs" DROP COLUMN "replying_enabled";
ALTER TABLE "agent_configs" DROP COLUMN "engaging_enabled";
ALTER TABLE "usage_records" DROP COLUMN "org_id";
ALTER TABLE "users" DROP COLUMN "org_id";
ALTER TABLE "users" DROP COLUMN "role";
END
$migration$;
