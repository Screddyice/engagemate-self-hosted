CREATE TABLE "hermes_execution_runs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"job_id" uuid NOT NULL UNIQUE,
	"user_id" uuid NOT NULL REFERENCES "users"("id") ON DELETE CASCADE,
	"ig_account_id" uuid NOT NULL,
	"hermes_session_id" text NOT NULL UNIQUE,
	"config_version" integer NOT NULL,
	"config_snapshot" jsonb NOT NULL,
	"status" text NOT NULL,
	"stop_reason" text,
	"started_at" timestamp with time zone DEFAULT now() NOT NULL,
	"completed_at" timestamp with time zone,
	CONSTRAINT "hermes_execution_runs_job_scope_fk"
		FOREIGN KEY ("job_id", "ig_account_id", "user_id")
		REFERENCES "jobs"("id", "ig_account_id", "user_id") ON DELETE CASCADE,
	CONSTRAINT "hermes_execution_runs_account_user_fk"
		FOREIGN KEY ("ig_account_id", "user_id")
		REFERENCES "ig_accounts"("id", "user_id") ON DELETE CASCADE,
	CONSTRAINT "hermes_execution_runs_status_check"
		CHECK ("status" IN ('starting', 'running', 'completed', 'failed', 'cancelled')),
	CONSTRAINT "hermes_execution_runs_snapshot_check"
		CHECK (jsonb_typeof("config_snapshot") = 'object')
);
--> statement-breakpoint
CREATE INDEX "hermes_execution_runs_active_idx"
	ON "hermes_execution_runs" ("status", "started_at")
	WHERE "status" IN ('starting', 'running');
--> statement-breakpoint
CREATE TABLE "hermes_tool_calls" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"run_id" uuid NOT NULL REFERENCES "hermes_execution_runs"("id") ON DELETE CASCADE,
	"tool_name" text NOT NULL,
	"opportunity_id" uuid,
	"idempotency_key" text NOT NULL,
	"status" text NOT NULL,
	"failure_class" text,
	"safe_result" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"started_at" timestamp with time zone DEFAULT now() NOT NULL,
	"completed_at" timestamp with time zone,
	CONSTRAINT "hermes_tool_calls_run_idempotency_unique" UNIQUE ("run_id", "idempotency_key"),
	CONSTRAINT "hermes_tool_calls_tool_check"
		CHECK ("tool_name" IN ('discover', 'view', 'like', 'reply', 'dm', 'complete_run')),
	CONSTRAINT "hermes_tool_calls_status_check"
		CHECK ("status" IN ('started', 'confirmed', 'failed', 'denied', 'uncertain')),
	CONSTRAINT "hermes_tool_calls_idempotency_check"
		CHECK ("idempotency_key" ~ '^[A-Za-z0-9_-]{16,128}$'),
	CONSTRAINT "hermes_tool_calls_result_check"
		CHECK (jsonb_typeof("safe_result") = 'object')
);
--> statement-breakpoint
CREATE INDEX "hermes_tool_calls_run_time_idx"
	ON "hermes_tool_calls" ("run_id", "started_at");
--> statement-breakpoint
CREATE TABLE "live_action_approvals" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"user_id" uuid NOT NULL REFERENCES "users"("id") ON DELETE CASCADE,
	"ig_account_id" uuid NOT NULL,
	"target_id" uuid NOT NULL,
	"story_fingerprint" text NOT NULL,
	"action" text NOT NULL,
	"generated_text_hash" text,
	"approved_by" text NOT NULL,
	"approved_at" timestamp with time zone DEFAULT now() NOT NULL,
	"expires_at" timestamp with time zone NOT NULL,
	"consumed_at" timestamp with time zone,
	CONSTRAINT "live_action_approvals_target_scope_fk"
		FOREIGN KEY ("target_id", "ig_account_id", "user_id")
		REFERENCES "targets"("id", "ig_account_id", "user_id") ON DELETE CASCADE,
	CONSTRAINT "live_action_approvals_action_check"
		CHECK ("action" IN ('view', 'like', 'reply', 'dm')),
	CONSTRAINT "live_action_approvals_text_hash_check"
		CHECK ("generated_text_hash" IS NULL OR "generated_text_hash" ~ '^[0-9a-f]{64}$')
);
--> statement-breakpoint
CREATE INDEX "live_action_approvals_unconsumed_expiry_idx"
	ON "live_action_approvals" ("expires_at")
	WHERE "consumed_at" IS NULL;
--> statement-breakpoint
CREATE TABLE "instagram_execution_controls" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"scope" text NOT NULL,
	"user_id" uuid REFERENCES "users"("id") ON DELETE CASCADE,
	"ig_account_id" uuid,
	"action" text,
	"blocked" boolean DEFAULT true NOT NULL,
	"reason_code" text NOT NULL,
	"failure_count" integer DEFAULT 0 NOT NULL,
	"opened_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "instagram_execution_controls_account_user_fk"
		FOREIGN KEY ("ig_account_id", "user_id")
		REFERENCES "ig_accounts"("id", "user_id") ON DELETE CASCADE,
	CONSTRAINT "instagram_execution_controls_scope_check"
		CHECK ("scope" IN ('system', 'action', 'tenant', 'account')),
	CONSTRAINT "instagram_execution_controls_action_check"
		CHECK ("action" IS NULL OR "action" IN ('view', 'like', 'reply', 'dm')),
	CONSTRAINT "instagram_execution_controls_reason_check"
		CHECK (char_length("reason_code") BETWEEN 1 AND 64),
	CONSTRAINT "instagram_execution_controls_failure_count_check"
		CHECK ("failure_count" >= 0),
	CONSTRAINT "instagram_execution_controls_shape_check" CHECK (
		("scope" = 'system' AND "user_id" IS NULL AND "ig_account_id" IS NULL AND "action" IS NULL)
		OR ("scope" = 'action' AND "user_id" IS NULL AND "ig_account_id" IS NULL AND "action" IS NOT NULL)
		OR ("scope" = 'tenant' AND "user_id" IS NOT NULL AND "ig_account_id" IS NULL AND "action" IS NULL)
		OR ("scope" = 'account' AND "user_id" IS NOT NULL AND "ig_account_id" IS NOT NULL AND "action" IS NULL)
	)
);
--> statement-breakpoint
CREATE UNIQUE INDEX "instagram_execution_controls_system_unique"
	ON "instagram_execution_controls" ((true)) WHERE "scope" = 'system';
--> statement-breakpoint
CREATE UNIQUE INDEX "instagram_execution_controls_action_unique"
	ON "instagram_execution_controls" ("action") WHERE "scope" = 'action';
--> statement-breakpoint
CREATE UNIQUE INDEX "instagram_execution_controls_tenant_unique"
	ON "instagram_execution_controls" ("user_id") WHERE "scope" = 'tenant';
--> statement-breakpoint
CREATE UNIQUE INDEX "instagram_execution_controls_account_unique"
	ON "instagram_execution_controls" ("user_id", "ig_account_id") WHERE "scope" = 'account';
