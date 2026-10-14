CREATE TABLE "proxy_capacity_orders" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"country" text NOT NULL,
	"region" text NOT NULL,
	"city" text NOT NULL,
	"product_id" integer NOT NULL,
	"plan_id" integer NOT NULL,
	"location_id" integer NOT NULL,
	"quantity" integer NOT NULL,
	"quoted_cost_cents" integer NOT NULL,
	"status" text DEFAULT 'submitting' NOT NULL,
	"provider_order_id" text,
	"provider_status" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "proxy_capacity_orders_geo_check" CHECK (char_length("proxy_capacity_orders"."country") = 2 and char_length("proxy_capacity_orders"."region") > 0 and char_length("proxy_capacity_orders"."city") > 0),
	CONSTRAINT "proxy_capacity_orders_catalog_check" CHECK ("proxy_capacity_orders"."product_id" > 0 and "proxy_capacity_orders"."plan_id" > 0 and "proxy_capacity_orders"."location_id" > 0),
	CONSTRAINT "proxy_capacity_orders_quantity_check" CHECK ("proxy_capacity_orders"."quantity" > 0),
	CONSTRAINT "proxy_capacity_orders_cost_check" CHECK ("proxy_capacity_orders"."quoted_cost_cents" >= 0),
	CONSTRAINT "proxy_capacity_orders_status_check" CHECK ("proxy_capacity_orders"."status" in ('submitting', 'submitted', 'confirmed', 'failed', 'unknown'))
);
--> statement-breakpoint
CREATE UNIQUE INDEX "proxy_capacity_orders_provider_order_idx" ON "proxy_capacity_orders" USING btree ("provider_order_id") WHERE "proxy_capacity_orders"."provider_order_id" is not null;--> statement-breakpoint
CREATE INDEX "proxy_capacity_orders_geo_status_idx" ON "proxy_capacity_orders" USING btree ("country","region","city","status");