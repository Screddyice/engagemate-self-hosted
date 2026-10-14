CREATE TABLE "capacity_waitlist" (
    "id" uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    "user_id" uuid NOT NULL REFERENCES "users"("id") ON DELETE CASCADE,
    "email" text NOT NULL,
    "requested_plan" text NOT NULL,
    "requested_accounts" integer NOT NULL DEFAULT 1,
    "reasons" text[] NOT NULL DEFAULT '{}'::text[],
    "status" text NOT NULL DEFAULT 'pending',
    "admin_notified_at" timestamptz,
    "admin_notification_attempts" integer NOT NULL DEFAULT 0,
    "admin_notification_error" text,
    "approved_at" timestamptz,
    "approved_by" uuid REFERENCES "users"("id"),
    "created_at" timestamptz NOT NULL DEFAULT now(),
    "updated_at" timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT "capacity_waitlist_requested_accounts_check" CHECK ("requested_accounts" BETWEEN 1 AND 100),
    CONSTRAINT "capacity_waitlist_status_check" CHECK ("status" IN ('pending', 'approved', 'rejected'))
);
CREATE UNIQUE INDEX "capacity_waitlist_pending_user_idx"
    ON "capacity_waitlist" ("user_id") WHERE "status" = 'pending';
CREATE INDEX "capacity_waitlist_status_created_idx"
    ON "capacity_waitlist" ("status", "created_at");
--> statement-breakpoint
