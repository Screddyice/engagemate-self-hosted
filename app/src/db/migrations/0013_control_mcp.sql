ALTER TABLE "agent_configs"
  ADD COLUMN "version" integer NOT NULL DEFAULT 1,
  ADD COLUMN "profile" jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD CONSTRAINT "agent_configs_profile_object_check"
    CHECK (jsonb_typeof("profile") = 'object');
--> statement-breakpoint
CREATE TABLE "agent_config_versions" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL REFERENCES "users"("id") ON DELETE cascade,
  "ig_account_id" uuid NOT NULL,
  "version" integer NOT NULL,
  "snapshot" jsonb NOT NULL,
  "actor_type" text NOT NULL CHECK ("actor_type" IN ('web', 'mcp', 'system')),
  "actor_id" text NOT NULL,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY ("ig_account_id", "user_id") REFERENCES "ig_accounts"("id", "user_id") ON DELETE cascade,
  UNIQUE ("user_id", "ig_account_id", "version")
);
--> statement-breakpoint
CREATE TABLE "mcp_oauth_clients" (
  "client_id" text PRIMARY KEY,
  "client_metadata" jsonb NOT NULL,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  "updated_at" timestamptz NOT NULL DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "mcp_authorization_requests" (
  "state_hash" text PRIMARY KEY,
  "client_id" text NOT NULL REFERENCES "mcp_oauth_clients"("client_id") ON DELETE cascade,
  "redirect_uri" text NOT NULL,
  "code_challenge" text NOT NULL,
  "scopes" text[] NOT NULL,
  "resource" text NOT NULL,
  "client_state" text,
  "expires_at" timestamptz NOT NULL,
  "consumed_at" timestamptz,
  "created_at" timestamptz NOT NULL DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "mcp_authorization_codes" (
  "code_hash" text PRIMARY KEY,
  "client_id" text NOT NULL REFERENCES "mcp_oauth_clients"("client_id") ON DELETE cascade,
  "user_id" uuid NOT NULL REFERENCES "users"("id") ON DELETE cascade,
  "redirect_uri" text NOT NULL,
  "code_challenge" text NOT NULL,
  "scopes" text[] NOT NULL,
  "resource" text NOT NULL,
  "expires_at" timestamptz NOT NULL,
  "consumed_at" timestamptz
);
--> statement-breakpoint
CREATE TABLE "mcp_tokens" (
  "token_hash" text PRIMARY KEY,
  "kind" text NOT NULL CHECK ("kind" IN ('access', 'refresh')),
  "client_id" text NOT NULL REFERENCES "mcp_oauth_clients"("client_id") ON DELETE cascade,
  "user_id" uuid NOT NULL REFERENCES "users"("id") ON DELETE cascade,
  "scopes" text[] NOT NULL,
  "resource" text NOT NULL,
  "expires_at" timestamptz NOT NULL,
  "revoked_at" timestamptz,
  "created_at" timestamptz NOT NULL DEFAULT now()
);
--> statement-breakpoint
CREATE TABLE "mcp_pending_changes" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL REFERENCES "users"("id") ON DELETE cascade,
  "ig_account_id" uuid NOT NULL,
  "client_id" text NOT NULL REFERENCES "mcp_oauth_clients"("client_id") ON DELETE cascade,
  "kind" text NOT NULL CHECK ("kind" IN ('enable_dm', 'raise_limit', 'broaden_audience')),
  "patch" jsonb NOT NULL,
  "before_state" jsonb NOT NULL,
  "after_state" jsonb NOT NULL,
  "expires_at" timestamptz NOT NULL,
  "confirmed_at" timestamptz,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY ("ig_account_id", "user_id") REFERENCES "ig_accounts"("id", "user_id") ON DELETE cascade
);
--> statement-breakpoint
CREATE TABLE "mcp_audit_events" (
  "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  "user_id" uuid NOT NULL REFERENCES "users"("id") ON DELETE cascade,
  "ig_account_id" uuid,
  "client_id" text,
  "tool_name" text NOT NULL,
  "outcome" text NOT NULL CHECK ("outcome" IN ('read', 'updated', 'pending', 'denied', 'failed')),
  "before_state" jsonb,
  "after_state" jsonb,
  "safe_metadata" jsonb NOT NULL DEFAULT '{}'::jsonb,
  "created_at" timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY ("ig_account_id", "user_id") REFERENCES "ig_accounts"("id", "user_id") ON DELETE cascade
);
--> statement-breakpoint
CREATE INDEX "agent_config_versions_account_version_idx"
  ON "agent_config_versions" ("ig_account_id", "version");
--> statement-breakpoint
CREATE INDEX "mcp_tokens_active_hash_idx"
  ON "mcp_tokens" ("token_hash", "expires_at") WHERE "revoked_at" IS NULL;
--> statement-breakpoint
CREATE INDEX "mcp_authorization_requests_expiry_idx"
  ON "mcp_authorization_requests" ("expires_at") WHERE "consumed_at" IS NULL;
--> statement-breakpoint
CREATE INDEX "mcp_pending_changes_expiry_idx"
  ON "mcp_pending_changes" ("expires_at") WHERE "confirmed_at" IS NULL;
--> statement-breakpoint
CREATE INDEX "mcp_audit_events_tenant_time_idx"
  ON "mcp_audit_events" ("user_id", "created_at");
