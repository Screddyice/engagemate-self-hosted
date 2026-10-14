CREATE TABLE "account_growth_snapshots" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"ig_account_id" uuid NOT NULL,
	"snapshot_date" date NOT NULL,
	"captured_at" timestamp with time zone DEFAULT now() NOT NULL,
	"follower_count" integer,
	"following_count" integer,
	"media_count" integer,
	"engagement_rate_bps" integer,
	"posts_sampled" integer DEFAULT 0 NOT NULL,
	"source" text DEFAULT 'hypercrawl' NOT NULL,
	"status" text NOT NULL,
	"failure_code" text,
	CONSTRAINT "account_growth_snapshots_status_check" CHECK ("account_growth_snapshots"."status" in ('complete', 'partial', 'unavailable')),
	CONSTRAINT "account_growth_snapshots_source_check" CHECK ("account_growth_snapshots"."source" = 'hypercrawl'),
	CONSTRAINT "account_growth_snapshots_counts_check" CHECK (("account_growth_snapshots"."follower_count" is null or "account_growth_snapshots"."follower_count" >= 0)
        and ("account_growth_snapshots"."following_count" is null or "account_growth_snapshots"."following_count" >= 0)
        and ("account_growth_snapshots"."media_count" is null or "account_growth_snapshots"."media_count" >= 0)
        and ("account_growth_snapshots"."engagement_rate_bps" is null or "account_growth_snapshots"."engagement_rate_bps" >= 0)
        and "account_growth_snapshots"."posts_sampled" >= 0),
	CONSTRAINT "account_growth_snapshots_follower_status_check" CHECK (("account_growth_snapshots"."status" = 'unavailable' and "account_growth_snapshots"."follower_count" is null)
        or ("account_growth_snapshots"."status" in ('complete', 'partial') and "account_growth_snapshots"."follower_count" is not null)),
	CONSTRAINT "account_growth_snapshots_failure_code_check" CHECK ("account_growth_snapshots"."failure_code" is null or char_length("account_growth_snapshots"."failure_code") <= 64)
);
--> statement-breakpoint
CREATE TABLE "weekly_report_deliveries" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL,
	"week_start" date NOT NULL,
	"status" text DEFAULT 'pending' NOT NULL,
	"attempts" integer DEFAULT 0 NOT NULL,
	"recipient_email" text NOT NULL,
	"provider_message_id" text,
	"last_error_code" text,
	"lease_token" uuid,
	"lease_expires_at" timestamp with time zone,
	"payload_hash" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	"sent_at" timestamp with time zone,
	CONSTRAINT "weekly_report_deliveries_status_check" CHECK ("weekly_report_deliveries"."status" in ('pending', 'sending', 'sent', 'failed')),
	CONSTRAINT "weekly_report_deliveries_attempts_check" CHECK ("weekly_report_deliveries"."attempts" >= 0),
	CONSTRAINT "weekly_report_deliveries_lease_check" CHECK (("weekly_report_deliveries"."status" = 'sending'
          and "weekly_report_deliveries"."lease_token" is not null
          and "weekly_report_deliveries"."lease_expires_at" is not null)
        or ("weekly_report_deliveries"."status" <> 'sending'
          and "weekly_report_deliveries"."lease_token" is null
          and "weekly_report_deliveries"."lease_expires_at" is null)),
	CONSTRAINT "weekly_report_deliveries_payload_hash_check" CHECK ("weekly_report_deliveries"."payload_hash" is null or "weekly_report_deliveries"."payload_hash" ~ '^[0-9a-f]{64}$'),
	CONSTRAINT "weekly_report_deliveries_last_error_code_check" CHECK ("weekly_report_deliveries"."last_error_code" is null or char_length("weekly_report_deliveries"."last_error_code") <= 64)
);
--> statement-breakpoint
CREATE TABLE "weekly_report_preferences" (
	"user_id" uuid PRIMARY KEY NOT NULL,
	"enabled" boolean DEFAULT true NOT NULL,
	"timezone" text DEFAULT 'UTC' NOT NULL,
	"send_hour" integer DEFAULT 9 NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "weekly_report_preferences_send_hour_check" CHECK ("weekly_report_preferences"."send_hour" between 0 and 23)
);
--> statement-breakpoint
ALTER TABLE "account_growth_snapshots" ADD CONSTRAINT "account_growth_snapshots_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "account_growth_snapshots" ADD CONSTRAINT "account_growth_snapshots_account_user_fk" FOREIGN KEY ("ig_account_id","user_id") REFERENCES "public"."ig_accounts"("id","user_id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "weekly_report_deliveries" ADD CONSTRAINT "weekly_report_deliveries_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "weekly_report_preferences" ADD CONSTRAINT "weekly_report_preferences_user_id_users_id_fk" FOREIGN KEY ("user_id") REFERENCES "public"."users"("id") ON DELETE cascade ON UPDATE no action;--> statement-breakpoint
INSERT INTO "weekly_report_preferences" (
	"user_id",
	"enabled",
	"timezone",
	"send_hour",
	"created_at",
	"updated_at"
)
SELECT
	"users"."id",
	true,
	COALESCE(
		(
			SELECT "agent_configs"."timezone"
			FROM "ig_accounts"
			INNER JOIN "agent_configs"
				ON "agent_configs"."ig_account_id" = "ig_accounts"."id"
				AND "agent_configs"."user_id" = "ig_accounts"."user_id"
			WHERE "ig_accounts"."user_id" = "users"."id"
			ORDER BY "ig_accounts"."created_at", "ig_accounts"."id"
			LIMIT 1
		),
		'UTC'
	),
	9,
	now(),
	now()
FROM "users"
ON CONFLICT ("user_id") DO NOTHING;--> statement-breakpoint
CREATE UNIQUE INDEX "account_growth_snapshots_account_date_idx" ON "account_growth_snapshots" USING btree ("ig_account_id","snapshot_date");--> statement-breakpoint
CREATE INDEX "account_growth_snapshots_user_date_status_idx" ON "account_growth_snapshots" USING btree ("user_id","snapshot_date","status");--> statement-breakpoint
CREATE UNIQUE INDEX "weekly_report_deliveries_user_week_idx" ON "weekly_report_deliveries" USING btree ("user_id","week_start");--> statement-breakpoint
CREATE INDEX "weekly_report_deliveries_retryable_idx" ON "weekly_report_deliveries" USING btree ("status","lease_expires_at","created_at") WHERE "weekly_report_deliveries"."status" in ('pending', 'sending', 'failed');--> statement-breakpoint
CREATE INDEX "weekly_report_preferences_enabled_idx" ON "weekly_report_preferences" USING btree ("send_hour","timezone") WHERE "weekly_report_preferences"."enabled" = true;
