CREATE TABLE "brand_profiles" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"reference_url" text NOT NULL,
	"reference_username" text NOT NULL,
	"onboarding_answers" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"analysis" jsonb,
	"status" text DEFAULT 'complete' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "brand_profiles_status_check" CHECK ("brand_profiles"."status" in ('pending', 'complete', 'failed'))
);
--> statement-breakpoint
CREATE TABLE "brand_reference_accounts" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"brand_profile_id" uuid NOT NULL,
	"username" text NOT NULL,
	"profile_url" text NOT NULL,
	"position" integer NOT NULL,
	"scraped" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "brand_reference_accounts_position_check" CHECK ("brand_reference_accounts"."position" between 0 and 4)
);
--> statement-breakpoint
CREATE TABLE "discovery_candidates" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"brand_profile_id" uuid NOT NULL,
	"username" text NOT NULL,
	"ig_user_id" text,
	"profile_url" text NOT NULL,
	"follower_count" integer,
	"score" integer NOT NULL,
	"engagement_rate_bps" integer DEFAULT 0 NOT NULL,
	"us_signal" boolean DEFAULT false NOT NULL,
	"evidence" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"status" text DEFAULT 'suggested' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "discovery_candidates_score_check" CHECK ("discovery_candidates"."score" between 0 and 100),
	CONSTRAINT "discovery_candidates_rate_check" CHECK ("discovery_candidates"."engagement_rate_bps" >= 0),
	CONSTRAINT "discovery_candidates_status_check" CHECK ("discovery_candidates"."status" in ('suggested', 'approved', 'rejected', 'imported'))
);
--> statement-breakpoint
ALTER TABLE "brand_profiles" ADD CONSTRAINT "brand_profiles_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "brand_reference_accounts" ADD CONSTRAINT "brand_reference_accounts_brand_profile_id_brand_profiles_id_fk" FOREIGN KEY ("brand_profile_id") REFERENCES "public"."brand_profiles"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "discovery_candidates" ADD CONSTRAINT "discovery_candidates_brand_profile_id_brand_profiles_id_fk" FOREIGN KEY ("brand_profile_id") REFERENCES "public"."brand_profiles"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "brand_profiles_user_idx" ON "brand_profiles" USING btree ("user_id");--> statement-breakpoint
CREATE UNIQUE INDEX "brand_reference_accounts_profile_username_idx" ON "brand_reference_accounts" USING btree ("brand_profile_id","username");--> statement-breakpoint
CREATE UNIQUE INDEX "brand_reference_accounts_profile_position_idx" ON "brand_reference_accounts" USING btree ("brand_profile_id","position");--> statement-breakpoint
CREATE UNIQUE INDEX "discovery_candidates_profile_username_idx" ON "discovery_candidates" USING btree ("brand_profile_id","username");--> statement-breakpoint
CREATE INDEX "discovery_candidates_profile_status_idx" ON "discovery_candidates" USING btree ("brand_profile_id","status","score");