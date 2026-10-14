ALTER TABLE "agent_configs"
  ADD COLUMN "launched_at" timestamp with time zone;
--> statement-breakpoint
ALTER TABLE "brand_profiles"
  ADD COLUMN "ig_account_id" uuid;
--> statement-breakpoint
UPDATE "brand_profiles" AS "profile"
   SET "ig_account_id" = "account"."id"
  FROM "ig_accounts" AS "account"
 WHERE "profile"."ig_account_id" IS NULL
   AND "account"."user_id" = "profile"."user_id"
   AND NOT EXISTS (
       SELECT 1
         FROM "ig_accounts" AS "other_account"
        WHERE "other_account"."user_id" = "profile"."user_id"
          AND "other_account"."id" <> "account"."id"
   );
--> statement-breakpoint
DROP INDEX "brand_profiles_user_idx";
--> statement-breakpoint
CREATE UNIQUE INDEX "brand_profiles_user_idx"
  ON "brand_profiles" USING btree ("user_id", "ig_account_id");
--> statement-breakpoint
ALTER TABLE "brand_profiles"
  ADD CONSTRAINT "brand_profiles_account_user_fk"
  FOREIGN KEY ("ig_account_id", "user_id")
  REFERENCES "ig_accounts" ("id", "user_id")
  ON DELETE cascade ON UPDATE no action;
--> statement-breakpoint
UPDATE "agent_configs" AS "config"
   SET "launched_at" = COALESCE("config"."updated_at", "config"."created_at", now())
  FROM "ig_accounts" AS "account"
 WHERE "config"."launched_at" IS NULL
   AND "account"."id" = "config"."ig_account_id"
   AND "account"."user_id" = "config"."user_id"
   AND (
       "config"."enabled" = true
       OR EXISTS (
           SELECT 1
             FROM "run_summaries" AS "run"
            WHERE "run"."ig_account_id" = "config"."ig_account_id"
              AND "run"."user_id" = "config"."user_id"
       )
   );
--> statement-breakpoint
CREATE INDEX "ig_sessions_account_user_id_idx"
  ON "ig_sessions" USING btree ("ig_account_id", "user_id", "id");
