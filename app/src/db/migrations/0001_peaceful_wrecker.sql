CREATE TABLE "content_pillars" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"org_id" uuid NOT NULL,
	"name" text NOT NULL,
	"description" text,
	"keywords" text[],
	"is_active" boolean DEFAULT true NOT NULL,
	"created_at" timestamp with time zone DEFAULT now()
);
--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "promo_code" text;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "voice_bio" text;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "voice_tone" text;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "voice_style" text;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "voice_dos" text[];--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "voice_donts" text[];--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "onboarding_complete" boolean DEFAULT false NOT NULL;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "slack_webhook_url" text;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "notification_email" text;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "notify_on_error" boolean DEFAULT true NOT NULL;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "notify_on_complete" boolean DEFAULT true NOT NULL;--> statement-breakpoint
ALTER TABLE "organizations" ADD COLUMN "notify_on_milestone" boolean DEFAULT true NOT NULL;--> statement-breakpoint
ALTER TABLE "social_accounts" ADD COLUMN "last_synced_at" timestamp with time zone;--> statement-breakpoint
ALTER TABLE "content_pillars" ADD CONSTRAINT "content_pillars_org_id_organizations_id_fk" FOREIGN KEY ("org_id") REFERENCES "public"."organizations"("id") ON DELETE cascade ON UPDATE no action;