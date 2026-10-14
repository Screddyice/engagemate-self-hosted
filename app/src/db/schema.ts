import { DEFAULT_FOLLOW_REEL_MESSAGE } from "@/lib/follow-reel-settings";
import { sql } from "drizzle-orm";
import {
  boolean,
  check,
  date,
  foreignKey,
  index,
  integer,
  jsonb,
  numeric,
  pgTable,
  primaryKey,
  text,
  timestamp,
  uniqueIndex,
  unique,
  uuid,
} from "drizzle-orm/pg-core";

const createdAt = () =>
  timestamp("created_at", { withTimezone: true }).notNull().defaultNow();
const updatedAt = () =>
  timestamp("updated_at", { withTimezone: true }).notNull().defaultNow();

export const users = pgTable(
  "users",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    ownerKey: text("owner_key").notNull().unique(),
    email: text("email").notNull(),
    name: text("name"),
    plan: text("plan").notNull().default("self_hosted"),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
);

export const igAccounts = pgTable(
  "ig_accounts",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    username: text("username").notNull(),
    igUserId: text("ig_user_id"),
    loginCountry: text("login_country"),
    loginRegion: text("login_region"),
    loginCity: text("login_city"),
    followerCount: integer("follower_count"),
    warmupStartedAt: timestamp("warmup_started_at", { withTimezone: true }),
    healthStatus: text("health_status").notNull().default("active"),
    lastHealthyAt: timestamp("last_healthy_at", { withTimezone: true }),
    pausedAt: timestamp("paused_at", { withTimezone: true }),
    pauseReason: text("pause_reason"),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    unique("ig_accounts_id_user_unique").on(table.id, table.userId),
    uniqueIndex("ig_accounts_user_username_idx").on(
      table.userId,
      table.username,
    ),
    uniqueIndex("ig_accounts_user_ig_user_id_idx")
      .on(table.userId, table.igUserId)
      .where(sql`${table.igUserId} is not null`),
    check(
      "ig_accounts_health_status_check",
      sql`${table.healthStatus} in ('active', 'degraded', 'paused', 'challenged', 'banned')`,
    ),
  ],
);

export const igSessions = pgTable("ig_sessions", {
  id: uuid("id").primaryKey().defaultRandom(),
  userId: uuid("user_id")
    .notNull()
    .references(() => users.id, { onDelete: "cascade" }),
  igAccountId: uuid("ig_account_id")
    .notNull(),
  encryptedCookieBlob: text("encrypted_cookie_blob").notNull(),
  encryptionVersion: integer("encryption_version").notNull(),
  capturedUserAgent: text("captured_user_agent").notNull(),
  capturedAt: timestamp("captured_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  lastValidatedAt: timestamp("last_validated_at", { withTimezone: true }),
  expiresAt: timestamp("expires_at", { withTimezone: true }),
}, (table) => [
  foreignKey({
    columns: [table.igAccountId, table.userId],
    foreignColumns: [igAccounts.id, igAccounts.userId],
    name: "ig_sessions_account_user_fk",
  }).onDelete("cascade"),
  index("ig_sessions_account_user_id_idx").on(
    table.igAccountId,
    table.userId,
    table.id,
  ),
]);

export const proxies = pgTable(
  "proxies",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    provider: text("provider").notNull(),
    providerReference: text("provider_reference").notNull(),
    proxyType: text("proxy_type").notNull(),
    country: text("country").notNull(),
    region: text("region"),
    city: text("city"),
    host: text("host").notNull(),
    port: integer("port").notNull(),
    encryptedUsername: text("encrypted_username").notNull(),
    encryptedPassword: text("encrypted_password").notNull(),
    credentialScope: text("credential_scope").notNull().default("account"),
    healthStatus: text("health_status").notNull().default("active"),
    lastCheckedAt: timestamp("last_checked_at", { withTimezone: true }),
    createdAt: createdAt(),
  },
  (table) => [
    check(
      "proxies_health_status_check",
      sql`${table.healthStatus} in ('active', 'degraded', 'paused', 'challenged', 'banned')`,
    ),
    check(
      "proxies_credential_scope_check",
      sql`${table.credentialScope} in ('inventory', 'account')`,
    ),
  ],
);

export const proxyBindings = pgTable(
  "proxy_bindings",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id")
      .notNull(),
    proxyId: uuid("proxy_id")
      .notNull()
      .references(() => proxies.id),
    boundAt: timestamp("bound_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    releasedAt: timestamp("released_at", { withTimezone: true }),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "proxy_bindings_account_user_fk",
    }).onDelete("cascade"),
    uniqueIndex("proxy_bindings_active_account_idx")
      .on(table.igAccountId)
      .where(sql`${table.releasedAt} is null`),
    uniqueIndex("proxy_bindings_active_proxy_idx")
      .on(table.proxyId)
      .where(sql`${table.releasedAt} is null`),
  ],
);


// Hosted browsers hold paid inventory without creating an unverified account.
export const hostedLoginReservations = pgTable(
  "hosted_login_reservations",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id").notNull().references(() => users.id, { onDelete: "cascade" }),
    accountId: uuid("account_id").notNull(),
    username: text("username").notNull(),
    expectedIgUserId: text("expected_ig_user_id"),
    proxyId: uuid("proxy_id").notNull().references(() => proxies.id),
    createdAt: createdAt(),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull().default(sql`now() + interval '12 minutes'`),
    releasedAt: timestamp("released_at", { withTimezone: true }),
  },
  (table) => [
    uniqueIndex("hosted_login_active_user").on(table.userId).where(sql`${table.releasedAt} IS NULL`),
    uniqueIndex("hosted_login_active_proxy").on(table.proxyId).where(sql`${table.releasedAt} IS NULL`),
    index("hosted_login_expiry").on(table.expiresAt).where(sql`${table.releasedAt} IS NULL`),
    check("hosted_login_expiry_check", sql`${table.expiresAt} > ${table.createdAt}`),
  ],
);

export const proxyCapacityOrders = pgTable(
  "proxy_capacity_orders",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    country: text("country").notNull(),
    region: text("region").notNull(),
    city: text("city").notNull(),
    productId: integer("product_id").notNull(),
    planId: integer("plan_id").notNull(),
    locationId: integer("location_id").notNull(),
    quantity: integer("quantity").notNull(),
    quotedCostCents: integer("quoted_cost_cents").notNull(),
    status: text("status").notNull().default("submitting"),
    providerOrderId: text("provider_order_id"),
    providerStatus: text("provider_status"),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    check(
      "proxy_capacity_orders_geo_check",
      sql`char_length(${table.country}) = 2 and char_length(${table.region}) > 0 and char_length(${table.city}) > 0`,
    ),
    check(
      "proxy_capacity_orders_catalog_check",
      sql`${table.productId} > 0 and ${table.planId} > 0 and ${table.locationId} > 0`,
    ),
    check(
      "proxy_capacity_orders_quantity_check",
      sql`${table.quantity} > 0`,
    ),
    check(
      "proxy_capacity_orders_cost_check",
      sql`${table.quotedCostCents} >= 0`,
    ),
    check(
      "proxy_capacity_orders_status_check",
      sql`${table.status} in ('submitting', 'submitted', 'confirmed', 'failed', 'unknown')`,
    ),
    uniqueIndex("proxy_capacity_orders_provider_order_geo_idx")
      .on(table.providerOrderId, table.country, table.region, table.city)
      .where(sql`${table.providerOrderId} is not null`),
    index("proxy_capacity_orders_geo_status_idx").on(
      table.country,
      table.region,
      table.city,
      table.status,
    ),
  ],
);

export const fingerprints = pgTable(
  "fingerprints",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id")
      .notNull(),
    userAgent: text("user_agent").notNull(),
    viewportWidth: integer("viewport_width").notNull(),
    viewportHeight: integer("viewport_height").notNull(),
    timezone: text("timezone").notNull(),
    locale: text("locale").notNull(),
    colorScheme: text("color_scheme").notNull(),
    canvasSeed: text("canvas_seed").notNull(),
    webglSeed: text("webgl_seed").notNull(),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "fingerprints_account_user_fk",
    }).onDelete("cascade"),
    uniqueIndex("fingerprints_ig_account_idx").on(table.igAccountId),
  ],
);

export const brandProfiles = pgTable(
  "brand_profiles",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id"),
    followerAudienceApprovedAt: timestamp("follower_audience_approved_at", { withTimezone: true }),
    referenceUrl: text("reference_url").notNull(),
    referenceUsername: text("reference_username").notNull(),
    onboardingAnswers: jsonb("onboarding_answers").notNull().default({}),
    analysis: jsonb("analysis"),
    status: text("status").notNull().default("complete"),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "brand_profiles_account_user_fk",
    }).onDelete("cascade"),
    uniqueIndex("brand_profiles_user_idx").on(
      table.userId,
      table.igAccountId,
    ),
    check(
      "brand_profiles_status_check",
      sql`${table.status} in ('pending', 'complete', 'failed')`,
    ),
  ],
);
export const brandReferenceAccounts = pgTable(
  "brand_reference_accounts",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    brandProfileId: uuid("brand_profile_id")
      .notNull()
      .references(() => brandProfiles.id, { onDelete: "cascade" }),
    username: text("username").notNull(),
    profileUrl: text("profile_url").notNull(),
    position: integer("position").notNull(),
    scraped: jsonb("scraped").notNull().default({}),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    uniqueIndex("brand_reference_accounts_profile_username_idx").on(
      table.brandProfileId,
      table.username,
    ),
    uniqueIndex("brand_reference_accounts_profile_position_idx").on(
      table.brandProfileId,
      table.position,
    ),
    check(
      "brand_reference_accounts_position_check",
      sql`${table.position} between 0 and 4`,
    ),
  ],
);
export const discoveryCandidates = pgTable(
  "discovery_candidates",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    brandProfileId: uuid("brand_profile_id")
      .notNull()
      .references(() => brandProfiles.id, { onDelete: "cascade" }),
    username: text("username").notNull(),
    igUserId: text("ig_user_id"),
    profileUrl: text("profile_url").notNull(),
    followerCount: integer("follower_count"),
    score: integer("score").notNull(),
    engagementRateBps: integer("engagement_rate_bps").notNull().default(0),
    usSignal: boolean("us_signal").notNull().default(false),
    evidence: jsonb("evidence").notNull().default({}),
    status: text("status").notNull().default("suggested"),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    uniqueIndex("discovery_candidates_profile_username_idx").on(
      table.brandProfileId,
      table.username,
    ),
    index("discovery_candidates_profile_status_idx").on(
      table.brandProfileId,
      table.status,
      table.score,
    ),
    check(
      "discovery_candidates_score_check",
      sql`${table.score} between 0 and 100`,
    ),
    check(
      "discovery_candidates_rate_check",
      sql`${table.engagementRateBps} >= 0`,
    ),
    check(
      "discovery_candidates_status_check",
      sql`${table.status} in ('suggested', 'approved', 'rejected', 'imported')`,
    ),
  ],
);

export const agentConfigs = pgTable(
  "agent_configs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id")
      .notNull(),
    enabled: boolean("enabled").notNull().default(false),
    timezone: text("timezone").notNull().default("UTC"),
    scheduleHour: integer("schedule_hour").notNull().default(9),
    // Desired maxima, not today's allowance: the worker applies warm-up and plan caps.
    dailyLikeLimit: integer("daily_like_limit").notNull().default(20),
    dailyReplyLimit: integer("daily_reply_limit").notNull().default(0),
    dailyDmLimit: integer("daily_dm_limit").notNull().default(0),
    // Null preserves the legacy shared reply/reel allowance.
    dailyReelMessageLimit: integer("daily_reel_message_limit"),
    // Operator-only exemption for an established account; health gates still apply.
    warmupExempt: boolean("warmup_exempt").notNull().default(false),
    dailyViewLimit: integer("daily_view_limit").notNull().default(0),
    followAfterEngagement: boolean("follow_after_engagement").notNull().default(false),
    autoUnfollowEnabled: boolean("auto_unfollow_enabled").notNull().default(false),
    dailyFollowLimit: integer("daily_follow_limit").notNull().default(3),
    followReelMessageEnabled: boolean("follow_reel_message_enabled").notNull().default(false),
    followReelMessageEnabledAt: timestamp("follow_reel_message_enabled_at", { withTimezone: true }),
    followReelMessageTemplate: text("follow_reel_message_template").notNull().default(DEFAULT_FOLLOW_REEL_MESSAGE),
    followEnabledAt: timestamp("follow_enabled_at", { withTimezone: true }),
    growthGoal: integer("growth_goal"),
    persona: text("persona"),
    // The deterministic text gate reads both before any generated reply or DM
    // is sent, so they are per-account user configuration, not worker policy.
    bannedPhrases: text("banned_phrases")
      .array()
      .notNull()
      .default(sql`'{}'::text[]`),
    expectedLanguage: text("expected_language").notNull().default("en"),
    version: integer("version").notNull().default(1),
    profile: jsonb("profile").notNull().default({}),
    launchedAt: timestamp("launched_at", { withTimezone: true }),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "agent_configs_account_user_fk",
    }).onDelete("cascade"),
    uniqueIndex("agent_configs_ig_account_idx").on(table.igAccountId),
    check("agent_configs_daily_follow_limit_check", sql`${table.dailyFollowLimit} between 0 and 20`),
    check("agent_configs_daily_reel_message_limit_check", sql`${table.dailyReelMessageLimit} between 0 and 20`),
    check(
      "agent_configs_profile_object_check",
      sql`jsonb_typeof(${table.profile}) = 'object'`,
    ),
  ],
);

export const agentConfigVersions = pgTable(
  "agent_config_versions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id").notNull(),
    version: integer("version").notNull(),
    snapshot: jsonb("snapshot").notNull(),
    actorType: text("actor_type").notNull(),
    actorId: text("actor_id").notNull(),
    createdAt: createdAt(),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "agent_config_versions_account_user_fk",
    }).onDelete("cascade"),
    unique("agent_config_versions_tenant_version_unique").on(
      table.userId,
      table.igAccountId,
      table.version,
    ),
    index("agent_config_versions_account_version_idx").on(
      table.igAccountId,
      table.version,
    ),
    check(
      "agent_config_versions_actor_type_check",
      sql`${table.actorType} in ('web', 'mcp', 'system')`,
    ),
  ],
);

export const mcpOauthClients = pgTable("mcp_oauth_clients", {
  clientId: text("client_id").primaryKey(),
  clientMetadata: jsonb("client_metadata").notNull(),
  createdAt: createdAt(),
  updatedAt: updatedAt(),
});

export const mcpAuthorizationRequests = pgTable(
  "mcp_authorization_requests",
  {
    stateHash: text("state_hash").primaryKey(),
    clientId: text("client_id")
      .notNull()
      .references(() => mcpOauthClients.clientId, { onDelete: "cascade" }),
    redirectUri: text("redirect_uri").notNull(),
    codeChallenge: text("code_challenge").notNull(),
    scopes: text("scopes").array().notNull(),
    resource: text("resource").notNull(),
    clientState: text("client_state"),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
    consumedAt: timestamp("consumed_at", { withTimezone: true }),
    createdAt: createdAt(),
  },
  (table) => [
    index("mcp_authorization_requests_expiry_idx")
      .on(table.expiresAt)
      .where(sql`${table.consumedAt} is null`),
  ],
);

export const mcpAuthorizationCodes = pgTable("mcp_authorization_codes", {
  codeHash: text("code_hash").primaryKey(),
  clientId: text("client_id")
    .notNull()
    .references(() => mcpOauthClients.clientId, { onDelete: "cascade" }),
  userId: uuid("user_id")
    .notNull()
    .references(() => users.id, { onDelete: "cascade" }),
  redirectUri: text("redirect_uri").notNull(),
  codeChallenge: text("code_challenge").notNull(),
  scopes: text("scopes").array().notNull(),
  resource: text("resource").notNull(),
  expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
  consumedAt: timestamp("consumed_at", { withTimezone: true }),
});

export const mcpTokens = pgTable(
  "mcp_tokens",
  {
    tokenHash: text("token_hash").primaryKey(),
    kind: text("kind").notNull(),
    clientId: text("client_id")
      .notNull()
      .references(() => mcpOauthClients.clientId, { onDelete: "cascade" }),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    scopes: text("scopes").array().notNull(),
    resource: text("resource").notNull(),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
    revokedAt: timestamp("revoked_at", { withTimezone: true }),
    createdAt: createdAt(),
  },
  (table) => [
    check(
      "mcp_tokens_kind_check",
      sql`${table.kind} in ('access', 'refresh')`,
    ),
    index("mcp_tokens_active_hash_idx")
      .on(table.tokenHash, table.expiresAt)
      .where(sql`${table.revokedAt} is null`),
  ],
);

export const mcpPendingChanges = pgTable(
  "mcp_pending_changes",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id").notNull(),
    clientId: text("client_id")
      .notNull()
      .references(() => mcpOauthClients.clientId, { onDelete: "cascade" }),
    kind: text("kind").notNull(),
    patch: jsonb("patch").notNull(),
    beforeState: jsonb("before_state").notNull(),
    afterState: jsonb("after_state").notNull(),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
    confirmedAt: timestamp("confirmed_at", { withTimezone: true }),
    createdAt: createdAt(),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "mcp_pending_changes_account_user_fk",
    }).onDelete("cascade"),
    check(
      "mcp_pending_changes_kind_check",
      sql`${table.kind} in ('enable_dm', 'raise_limit', 'broaden_audience')`,
    ),
    index("mcp_pending_changes_expiry_idx")
      .on(table.expiresAt)
      .where(sql`${table.confirmedAt} is null`),
  ],
);

export const mcpAuditEvents = pgTable(
  "mcp_audit_events",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id"),
    clientId: text("client_id"),
    toolName: text("tool_name").notNull(),
    outcome: text("outcome").notNull(),
    beforeState: jsonb("before_state"),
    afterState: jsonb("after_state"),
    safeMetadata: jsonb("safe_metadata").notNull().default({}),
    createdAt: createdAt(),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "mcp_audit_events_account_user_fk",
    }).onDelete("cascade"),
    check(
      "mcp_audit_events_outcome_check",
      sql`${table.outcome} in ('read', 'updated', 'pending', 'denied', 'failed')`,
    ),
    index("mcp_audit_events_tenant_time_idx").on(
      table.userId,
      table.createdAt,
    ),
  ],
);

export const targets = pgTable(
  "targets",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id")
      .notNull(),
    username: text("username").notNull(),
    igUserId: text("ig_user_id"),
    tier: text("tier").notNull(),
    source: text("source").notNull(),
    enabled: boolean("enabled").notNull().default(true),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    unique("targets_identity_unique").on(
      table.id,
      table.igAccountId,
      table.userId,
    ),
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "targets_account_user_fk",
    }).onDelete("cascade"),
    uniqueIndex("targets_account_username_idx").on(
      table.igAccountId,
      table.username,
    ),
    check(
      "targets_tier_check",
      sql`${table.tier} in ('vip', 'broad', 'discovery')`,
    ),
  ],
);

export const jobs = pgTable(
  "jobs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id")
      .notNull(),
    scheduleKey: text("schedule_key").notNull().unique(),
    scheduledFor: timestamp("scheduled_for", { withTimezone: true }).notNull(),
    status: text("status").notNull().default("queued"),
    claimedBy: text("claimed_by"),
    leaseExpiresAt: timestamp("lease_expires_at", { withTimezone: true }),
    heartbeatAt: timestamp("heartbeat_at", { withTimezone: true }),
    attempts: integer("attempts").notNull().default(0),
    lastError: text("last_error"),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    unique("jobs_identity_unique").on(
      table.id,
      table.igAccountId,
      table.userId,
    ),
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "jobs_account_user_fk",
    }).onDelete("cascade"),
    index("jobs_status_scheduled_for_idx").on(
      table.status,
      table.scheduledFor,
    ),
    check(
      "jobs_status_check",
      sql`${table.status} in ('queued', 'running', 'completed', 'failed', 'cancelled')`,
    ),
  ],
);

export const storyEngagements = pgTable(
  "story_engagements",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id")
      .notNull(),
    targetId: uuid("target_id")
      .notNull(),
    storyFingerprint: text("story_fingerprint").notNull(),
    action: text("action").notNull(),
    generatedText: text("generated_text"),
    modelUsed: text("model_used"),
    rationale: text("rationale"),
    confidence: numeric("confidence", { precision: 5, scale: 4 }),
    status: text("status").notNull(),
    failureCode: text("failure_code"),
    createdAt: createdAt(),
    executedAt: timestamp("executed_at", { withTimezone: true }),
  },
  (table) => [
    unique("story_engagements_identity_unique").on(
      table.id,
      table.igAccountId,
      table.userId,
    ),
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "story_engagements_account_user_fk",
    }).onDelete("cascade"),
    foreignKey({
      columns: [table.targetId, table.igAccountId, table.userId],
      foreignColumns: [targets.id, targets.igAccountId, targets.userId],
      name: "story_engagements_target_scope_fk",
    }).onDelete("cascade"),
    uniqueIndex("story_engagements_dedupe_idx").on(
      table.igAccountId,
      table.storyFingerprint,
      table.action,
    ),
    check(
      "story_engagements_action_check",
      sql`${table.action} in ('like', 'reply', 'dm', 'view')`,
    ),
    check(
      "story_engagements_status_check",
      sql`${table.status} in ('sent', 'skipped', 'gated', 'failed')`,
    ),
  ],
);

export const relationshipFollows = pgTable("relationship_follows", {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id").notNull().references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id").notNull(),
    targetId: uuid("target_id").notNull(),
    ownerId: text("owner_id").notNull(),
    prerequisiteEngagementId: uuid("prerequisite_engagement_id").notNull(),
    status: text("status").notNull().default("follow_pending"),
    confirmedFollowAt: timestamp("confirmed_follow_at", { withTimezone: true }),
    unfollowDueAt: timestamp("unfollow_due_at", { withTimezone: true }),
    unfollowedAt: timestamp("unfollowed_at", { withTimezone: true }),
    leaseExpiresAt: timestamp("lease_expires_at", { withTimezone: true }),
    attemptedAt: timestamp("attempted_at", { withTimezone: true }),
    followAttemptedAt: timestamp("follow_attempted_at", { withTimezone: true }),
    unfollowAttemptedAt: timestamp("unfollow_attempted_at", { withTimezone: true }),
    followReservedAt: timestamp("follow_reserved_at", { withTimezone: true }),
    unfollowReservedAt: timestamp("unfollow_reserved_at", { withTimezone: true }),
    nextAttemptAt: timestamp("next_attempt_at", { withTimezone: true }),
    claimedBy: text("claimed_by"), leaseToken: uuid("lease_token"),
    lastError: text("last_error"), evidence: jsonb("evidence").notNull().default({}),
    createdAt: createdAt(), updatedAt: updatedAt(),
}, (table) => [
    foreignKey({ columns: [table.igAccountId, table.userId], foreignColumns: [igAccounts.id, igAccounts.userId], name: "relationship_follows_account_user_fk" }).onDelete("cascade"),
    foreignKey({ columns: [table.targetId, table.igAccountId, table.userId], foreignColumns: [targets.id, targets.igAccountId, targets.userId], name: "relationship_follows_target_scope_fk" }).onDelete("no action"),
    foreignKey({ columns: [table.prerequisiteEngagementId, table.igAccountId, table.userId], foreignColumns: [storyEngagements.id, storyEngagements.igAccountId, storyEngagements.userId], name: "relationship_follows_engagement_scope_fk" }).onDelete("no action"),
    unique("relationship_follows_scope_unique").on(table.id, table.igAccountId, table.userId),
    unique("relationship_follows_account_owner_unique").on(table.igAccountId, table.ownerId),
    check("relationship_follows_owner_id_check", sql`${table.ownerId} ~ '^[0-9]{1,32}$'`),
    check("relationship_follows_status_check", sql`${table.status} in ('follow_pending','follow_running','following','unfollow_running','unfollowed','skipped','uncertain','cancelled')`),
    check("relationship_follows_due_check", sql`(${table.confirmedFollowAt} is null and ${table.unfollowDueAt} is null) or (${table.confirmedFollowAt} is not null and ${table.unfollowDueAt} is not null and ${table.unfollowDueAt} = ${table.confirmedFollowAt} + interval '120 hours')`),
    check("relationship_follows_owned_check", sql`${table.status} not in ('following','unfollow_running','unfollowed') or ${table.confirmedFollowAt} is not null`),
    check("relationship_follows_lease_check", sql`(${table.status} in ('follow_running','unfollow_running') and ${table.claimedBy} is not null and ${table.leaseToken} is not null and ${table.leaseExpiresAt} is not null) or (${table.status} not in ('follow_running','unfollow_running') and ${table.claimedBy} is null and ${table.leaseToken} is null and ${table.leaseExpiresAt} is null)`),
    check("relationship_follows_evidence_check", sql`jsonb_typeof(${table.evidence}) = 'object'`),
    index("relationship_follows_due_idx").on(table.unfollowDueAt, table.nextAttemptAt, table.createdAt).where(sql`${table.status} in ('follow_pending','following')`),
    index("relationship_follows_account_status_idx").on(table.userId, table.igAccountId, table.status),
]);

export const engagementUsageEvents = pgTable(
  "engagement_usage_events",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id").notNull(),
    engagementId: uuid("engagement_id").notNull(),
    surface: text("surface").notNull().default("story"),
    action: text("action").notNull(),
    quantity: integer("quantity").notNull().default(1),
    status: text("status").notNull().default("reserved"),
    reservedAt: timestamp("reserved_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    finalizedAt: timestamp("finalized_at", { withTimezone: true }),
    occurredAt: timestamp("occurred_at", { withTimezone: true }),
    createdAt: createdAt(),
  },
  (table) => [
    unique("engagement_usage_events_engagement_id_unique").on(
      table.engagementId,
    ),
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "engagement_usage_events_account_user_fk",
    }).onDelete("cascade"),
    foreignKey({
      columns: [table.engagementId, table.igAccountId, table.userId],
      foreignColumns: [
        storyEngagements.id,
        storyEngagements.igAccountId,
        storyEngagements.userId,
      ],
      name: "engagement_usage_events_engagement_scope_fk",
    }).onDelete("cascade"),
    check(
      "engagement_usage_events_surface_check",
      sql`${table.surface} in ('story', 'post')`,
    ),
    check(
      "engagement_usage_events_action_check",
      sql`${table.action} in ('like', 'reply', 'dm')`,
    ),
    check(
      "engagement_usage_events_status_check",
      sql`${table.status} in ('reserved', 'billable', 'released')`,
    ),
    check(
      "engagement_usage_events_quantity_check",
      sql`${table.quantity} = 1`,
    ),
    index("engagement_usage_events_user_occurred_status_idx").on(
      table.userId,
      table.occurredAt,
      table.status,
    ),
  ],
);

export const accountHealthEvents = pgTable("account_health_events", {
  id: uuid("id").primaryKey().defaultRandom(),
  userId: uuid("user_id")
    .notNull()
    .references(() => users.id, { onDelete: "cascade" }),
  igAccountId: uuid("ig_account_id")
    .notNull(),
  eventType: text("event_type").notNull(),
  severity: text("severity").notNull(),
  evidence: jsonb("evidence").notNull(),
  createdAt: createdAt(),
}, (table) => [
  foreignKey({
    columns: [table.igAccountId, table.userId],
    foreignColumns: [igAccounts.id, igAccounts.userId],
    name: "account_health_events_account_user_fk",
  }).onDelete("cascade"),
]);

export const runSummaries = pgTable("run_summaries", {
  id: uuid("id").primaryKey().defaultRandom(),
  userId: uuid("user_id")
    .notNull()
    .references(() => users.id, { onDelete: "cascade" }),
  igAccountId: uuid("ig_account_id")
    .notNull(),
  jobId: uuid("job_id")
    .notNull(),
  opportunitiesFound: integer("opportunities_found").notNull().default(0),
  likesSent: integer("likes_sent").notNull().default(0),
  repliesSent: integer("replies_sent").notNull().default(0),
  dmsSent: integer("dms_sent").notNull().default(0),
  skipped: integer("skipped").notNull().default(0),
  gated: integer("gated").notNull().default(0),
  failed: integer("failed").notNull().default(0),
  followerCountBefore: integer("follower_count_before"),
  followerCountAfter: integer("follower_count_after"),
  inputTokens: integer("input_tokens").notNull().default(0),
  outputTokens: integer("output_tokens").notNull().default(0),
  imagesAnalyzed: integer("images_analyzed").notNull().default(0),
  videoFramesAnalyzed: integer("video_frames_analyzed").notNull().default(0),
  retries: integer("retries").notNull().default(0),
  runtimeMs: integer("runtime_ms").notNull().default(0),
  startedAt: timestamp("started_at", { withTimezone: true })
    .notNull()
    .defaultNow(),
  completedAt: timestamp("completed_at", { withTimezone: true }),
}, (table) => [
  foreignKey({
    columns: [table.igAccountId, table.userId],
    foreignColumns: [igAccounts.id, igAccounts.userId],
    name: "run_summaries_account_user_fk",
  }).onDelete("cascade"),
  foreignKey({
    columns: [table.jobId, table.igAccountId, table.userId],
    foreignColumns: [jobs.id, jobs.igAccountId, jobs.userId],
    name: "run_summaries_job_scope_fk",
  }).onDelete("cascade"),
  uniqueIndex("run_summaries_job_idx").on(table.jobId),
  check(
    "run_summaries_cost_counters_check",
    sql`${table.inputTokens} >= 0
      and ${table.outputTokens} >= 0
      and ${table.imagesAnalyzed} >= 0
      and ${table.videoFramesAnalyzed} >= 0
      and ${table.retries} >= 0
      and ${table.runtimeMs} >= 0`,
  ),
]);

export const accountGrowthSnapshots = pgTable(
  "account_growth_snapshots",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id").notNull(),
    snapshotDate: date("snapshot_date", { mode: "string" }).notNull(),
    capturedAt: timestamp("captured_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    followerCount: integer("follower_count"),
    followingCount: integer("following_count"),
    mediaCount: integer("media_count"),
    engagementRateBps: integer("engagement_rate_bps"),
    postsSampled: integer("posts_sampled").notNull().default(0),
    source: text("source").notNull().default("hypercrawl"),
    status: text("status").notNull(),
    failureCode: text("failure_code"),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "account_growth_snapshots_account_user_fk",
    }).onDelete("cascade"),
    uniqueIndex("account_growth_snapshots_account_date_idx").on(
      table.igAccountId,
      table.snapshotDate,
    ),
    index("account_growth_snapshots_user_date_status_idx").on(
      table.userId,
      table.snapshotDate,
      table.status,
    ),
    check(
      "account_growth_snapshots_status_check",
      sql`${table.status} in ('complete', 'partial', 'unavailable')`,
    ),
    check(
      "account_growth_snapshots_source_check",
      sql`${table.source} in ('hypercrawl', 'account_session')`,
    ),
    check(
      "account_growth_snapshots_counts_check",
      sql`(${table.followerCount} is null or ${table.followerCount} >= 0)
        and (${table.followingCount} is null or ${table.followingCount} >= 0)
        and (${table.mediaCount} is null or ${table.mediaCount} >= 0)
        and (${table.engagementRateBps} is null or ${table.engagementRateBps} >= 0)
        and ${table.postsSampled} >= 0`,
    ),
    check(
      "account_growth_snapshots_follower_status_check",
      sql`(${table.status} = 'unavailable' and ${table.followerCount} is null)
        or (${table.status} in ('complete', 'partial') and ${table.followerCount} is not null)`,
    ),
    check(
      "account_growth_snapshots_failure_code_check",
      sql`${table.failureCode} is null or char_length(${table.failureCode}) <= 64`,
    ),
  ],
);

export const weeklyReportPreferences = pgTable(
  "weekly_report_preferences",
  {
    userId: uuid("user_id")
      .primaryKey()
      .references(() => users.id, { onDelete: "cascade" }),
    enabled: boolean("enabled").notNull().default(true),
    timezone: text("timezone").notNull().default("UTC"),
    sendHour: integer("send_hour").notNull().default(9),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
  },
  (table) => [
    check(
      "weekly_report_preferences_send_hour_check",
      sql`${table.sendHour} between 0 and 23`,
    ),
    index("weekly_report_preferences_enabled_idx")
      .on(table.sendHour, table.timezone)
      .where(sql`${table.enabled} = true`),
  ],
);

// Emails a user can turn off from a footer link or Settings (migration 0024).
// No row means every email is on; the weekly report uses weeklyReportPreferences.
export const emailPreferences = pgTable("email_preferences", {
  userId: uuid("user_id")
    .primaryKey()
    .references(() => users.id, { onDelete: "cascade" }),
  activityEmails: boolean("activity_emails").notNull().default(true),
  onboardingEmails: boolean("onboarding_emails").notNull().default(true),
  updatedAt: updatedAt(),
});

export const weeklyReportDeliveries = pgTable(
  "weekly_report_deliveries",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    weekStart: date("week_start", { mode: "string" }).notNull(),
    status: text("status").notNull().default("pending"),
    attempts: integer("attempts").notNull().default(0),
    recipientEmail: text("recipient_email").notNull(),
    providerMessageId: text("provider_message_id"),
    lastErrorCode: text("last_error_code"),
    leaseToken: uuid("lease_token"),
    leaseExpiresAt: timestamp("lease_expires_at", { withTimezone: true }),
    payloadHash: text("payload_hash"),
    createdAt: createdAt(),
    updatedAt: updatedAt(),
    sentAt: timestamp("sent_at", { withTimezone: true }),
  },
  (table) => [
    uniqueIndex("weekly_report_deliveries_user_week_idx").on(
      table.userId,
      table.weekStart,
    ),
    index("weekly_report_deliveries_retryable_idx")
      .on(table.status, table.leaseExpiresAt, table.createdAt)
      .where(sql`${table.status} in ('pending', 'sending', 'failed')`),
    check(
      "weekly_report_deliveries_status_check",
      sql`${table.status} in ('pending', 'sending', 'sent', 'failed')`,
    ),
    check(
      "weekly_report_deliveries_attempts_check",
      sql`${table.attempts} >= 0`,
    ),
    check(
      "weekly_report_deliveries_lease_check",
      sql`(${table.status} = 'sending'
          and ${table.leaseToken} is not null
          and ${table.leaseExpiresAt} is not null)
        or (${table.status} <> 'sending'
          and ${table.leaseToken} is null
          and ${table.leaseExpiresAt} is null)`,
    ),
    check(
      "weekly_report_deliveries_payload_hash_check",
      sql`${table.payloadHash} is null or ${table.payloadHash} ~ '^[0-9a-f]{64}$'`,
    ),
    check(
      "weekly_report_deliveries_last_error_code_check",
      sql`${table.lastErrorCode} is null or char_length(${table.lastErrorCode}) <= 64`,
    ),
  ],
);

export const hermesExecutionRuns = pgTable(
  "hermes_execution_runs",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    jobId: uuid("job_id").notNull().unique(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id").notNull(),
    hermesSessionId: text("hermes_session_id").notNull().unique(),
    configVersion: integer("config_version").notNull(),
    configSnapshot: jsonb("config_snapshot").notNull(),
    discoveryReport: jsonb("discovery_report").notNull().default({}),
    status: text("status").notNull(),
    stopReason: text("stop_reason"),
    startedAt: timestamp("started_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    completedAt: timestamp("completed_at", { withTimezone: true }),
  },
  (table) => [
    foreignKey({
      columns: [table.jobId, table.igAccountId, table.userId],
      foreignColumns: [jobs.id, jobs.igAccountId, jobs.userId],
      name: "hermes_execution_runs_job_scope_fk",
    }).onDelete("cascade"),
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "hermes_execution_runs_account_user_fk",
    }).onDelete("cascade"),
    check(
      "hermes_execution_runs_status_check",
      sql`${table.status} in ('starting', 'running', 'completed', 'failed', 'cancelled')`,
    ),
    check(
      "hermes_execution_runs_snapshot_check",
      sql`jsonb_typeof(${table.configSnapshot}) = 'object'`,
    ),
    check(
      "hermes_execution_runs_discovery_report_check",
      sql`jsonb_typeof(${table.discoveryReport}) = 'object'`,
    ),
    index("hermes_execution_runs_active_idx")
      .on(table.status, table.startedAt)
      .where(sql`${table.status} in ('starting', 'running')`),
  ],
);

export const hermesToolCalls = pgTable(
  "hermes_tool_calls",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    runId: uuid("run_id")
      .notNull()
      .references(() => hermesExecutionRuns.id, { onDelete: "cascade" }),
    toolName: text("tool_name").notNull(),
    opportunityId: uuid("opportunity_id"),
    // What the worker asked to do, recorded at the attempt rather than at
    // success. A denied call is the only record that the attempt happened,
    // and canary approval has to name the exact target, Story, and text the
    // worker proposed. Null on discover and complete_run, which mutate
    // nothing, and on rows written before this column existed.
    targetId: uuid("target_id"),
    storyFingerprint: text("story_fingerprint"),
    generatedTextHash: text("generated_text_hash"),
    // The message itself, so a human approving a canary reads what is about
    // to reach a stranger rather than a hash of it. The hash stays the
    // matching key; this is only ever read by the operator scripts and never
    // enters safe_result, which Hermes can see.
    proposedText: text("proposed_text"),
    idempotencyKey: text("idempotency_key").notNull(),
    status: text("status").notNull(),
    failureClass: text("failure_class"),
    safeResult: jsonb("safe_result").notNull().default({}),
    startedAt: timestamp("started_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    completedAt: timestamp("completed_at", { withTimezone: true }),
  },
  (table) => [
    unique("hermes_tool_calls_run_idempotency_unique").on(
      table.runId,
      table.idempotencyKey,
    ),
    check(
      "hermes_tool_calls_fingerprint_check",
      sql`${table.storyFingerprint} is null or ${table.storyFingerprint} ~ '^[0-9a-f]{64}$'`,
    ),
    check(
      "hermes_tool_calls_text_hash_check",
      sql`${table.generatedTextHash} is null or ${table.generatedTextHash} ~ '^[0-9a-f]{64}$'`,
    ),
    check(
      "hermes_tool_calls_tool_check",
      sql`${table.toolName} in ('discover', 'view', 'like', 'reply', 'dm', 'complete_run')`,
    ),
    check(
      "hermes_tool_calls_status_check",
      sql`${table.status} in ('started', 'confirmed', 'failed', 'denied', 'uncertain')`,
    ),
    check(
      "hermes_tool_calls_idempotency_check",
      sql`${table.idempotencyKey} ~ '^[A-Za-z0-9_-]{16,128}$'`,
    ),
    check(
      "hermes_tool_calls_result_check",
      sql`jsonb_typeof(${table.safeResult}) = 'object'`,
    ),
    index("hermes_tool_calls_run_time_idx").on(table.runId, table.startedAt),
  ],
);

export const liveActionApprovals = pgTable(
  "live_action_approvals",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id")
      .notNull()
      .references(() => users.id, { onDelete: "cascade" }),
    igAccountId: uuid("ig_account_id").notNull(),
    targetId: uuid("target_id").notNull(),
    storyFingerprint: text("story_fingerprint").notNull(),
    action: text("action").notNull(),
    generatedTextHash: text("generated_text_hash"),
    approvedBy: text("approved_by").notNull(),
    approvedAt: timestamp("approved_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    expiresAt: timestamp("expires_at", { withTimezone: true }).notNull(),
    consumedAt: timestamp("consumed_at", { withTimezone: true }),
  },
  (table) => [
    foreignKey({
      columns: [table.targetId, table.igAccountId, table.userId],
      foreignColumns: [targets.id, targets.igAccountId, targets.userId],
      name: "live_action_approvals_target_scope_fk",
    }).onDelete("cascade"),
    check(
      "live_action_approvals_action_check",
      sql`${table.action} in ('view', 'like', 'reply', 'dm')`,
    ),
    check(
      "live_action_approvals_text_hash_check",
      sql`${table.generatedTextHash} is null or ${table.generatedTextHash} ~ '^[0-9a-f]{64}$'`,
    ),
    index("live_action_approvals_unconsumed_expiry_idx")
      .on(table.expiresAt)
      .where(sql`${table.consumedAt} is null`),
  ],
);

export const instagramExecutionControls = pgTable(
  "instagram_execution_controls",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    scope: text("scope").notNull(),
    userId: uuid("user_id").references(() => users.id, {
      onDelete: "cascade",
    }),
    igAccountId: uuid("ig_account_id"),
    action: text("action"),
    blocked: boolean("blocked").notNull().default(true),
    reasonCode: text("reason_code").notNull(),
    failureCount: integer("failure_count").notNull().default(0),
    openedAt: timestamp("opened_at", { withTimezone: true })
      .notNull()
      .defaultNow(),
    updatedAt: updatedAt(),
  },
  (table) => [
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
      name: "instagram_execution_controls_account_user_fk",
    }).onDelete("cascade"),
    check(
      "instagram_execution_controls_scope_check",
      sql`${table.scope} in ('system', 'action', 'tenant', 'account')`,
    ),
    check(
      "instagram_execution_controls_action_check",
      sql`${table.action} is null or ${table.action} in ('view', 'like', 'reply', 'dm', 'follow', 'unfollow')`,
    ),
    check(
      "instagram_execution_controls_reason_check",
      sql`char_length(${table.reasonCode}) between 1 and 64`,
    ),
    check(
      "instagram_execution_controls_failure_count_check",
      sql`${table.failureCount} >= 0`,
    ),
    check(
      "instagram_execution_controls_shape_check",
      sql`(${table.scope} = 'system' and ${table.userId} is null and ${table.igAccountId} is null and ${table.action} is null)
        or (${table.scope} = 'action' and ${table.userId} is null and ${table.igAccountId} is null and ${table.action} is not null)
        or (${table.scope} = 'tenant' and ${table.userId} is not null and ${table.igAccountId} is null and ${table.action} is null)
        or (${table.scope} = 'account' and ${table.userId} is not null and ${table.igAccountId} is not null and ${table.action} is null)`,
    ),
    uniqueIndex("instagram_execution_controls_system_unique")
      .on(sql`(true)`)
      .where(sql`${table.scope} = 'system'`),
    uniqueIndex("instagram_execution_controls_action_unique")
      .on(table.action)
      .where(sql`${table.scope} = 'action'`),
    uniqueIndex("instagram_execution_controls_tenant_unique")
      .on(table.userId)
      .where(sql`${table.scope} = 'tenant'`),
    uniqueIndex("instagram_execution_controls_account_unique")
      .on(table.userId, table.igAccountId)
      .where(sql`${table.scope} = 'account'`),
  ],
);

export const followerSourceStates = pgTable(
  "follower_source_states",
  {
    userId: uuid("user_id").notNull(),
    igAccountId: uuid("ig_account_id").notNull(),
    sourceId: uuid("source_id")
      .notNull()
      .references(() => brandReferenceAccounts.id, { onDelete: "cascade" }),
    cursor: text("cursor"),
    exhausted: boolean("exhausted").notNull().default(false),
    updatedAt: updatedAt(),
  },
  (table) => [
    primaryKey({ columns: [table.userId, table.igAccountId, table.sourceId] }),
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
    }).onDelete("cascade"),
  ],
);

export const followerCandidates = pgTable(
  "follower_candidates",
  {
    userId: uuid("user_id").notNull(),
    igAccountId: uuid("ig_account_id").notNull(),
    ownerId: text("owner_id").notNull(),
    handle: text("handle").notNull(),
    status: text("status").notNull().default("new"),
    lastCheckedAt: timestamp("last_checked_at", { withTimezone: true }),
    nextCheckAt: timestamp("next_check_at", { withTimezone: true }),
    createdAt: createdAt(),
  },
  (table) => [
    primaryKey({ columns: [table.userId, table.igAccountId, table.ownerId] }),
    foreignKey({
      columns: [table.igAccountId, table.userId],
      foreignColumns: [igAccounts.id, igAccounts.userId],
    }).onDelete("cascade"),
    index("follower_candidates_due_idx").on(
      table.userId,
      table.igAccountId,
      table.nextCheckAt,
      table.createdAt,
    ),
    check(
      "follower_candidates_status_check",
      sql`${table.status} in ('new', 'excluded', 'no_story', 'unknown', 'available')`,
    ),
  ],
);

export const followerCandidateSources = pgTable(
  "follower_candidate_sources",
  {
    userId: uuid("user_id").notNull(),
    igAccountId: uuid("ig_account_id").notNull(),
    ownerId: text("owner_id").notNull(),
    sourceId: uuid("source_id").notNull(),
  },
  (table) => [
    primaryKey({
      columns: [table.userId, table.igAccountId, table.ownerId, table.sourceId],
    }),
    foreignKey({
      columns: [table.userId, table.igAccountId, table.ownerId],
      foreignColumns: [
        followerCandidates.userId,
        followerCandidates.igAccountId,
        followerCandidates.ownerId,
      ],
    }).onDelete("cascade"),
    foreignKey({
      columns: [table.userId, table.igAccountId, table.sourceId],
      foreignColumns: [
        followerSourceStates.userId,
        followerSourceStates.igAccountId,
        followerSourceStates.sourceId,
      ],
    }).onDelete("cascade"),
  ],
);


// Public source-profile cache; session and proxy credentials never enter these rows.
export const onboardingProfileCache = pgTable("onboarding_profile_cache", {
  userId: uuid("user_id").notNull(),
  igAccountId: uuid("ig_account_id").notNull(),
  username: text("username").notNull(),
  profile: jsonb("profile").notNull(),
  capturedAt: timestamp("captured_at", { withTimezone: true }).notNull().defaultNow(),
}, (table) => [
  primaryKey({ columns: [table.userId, table.igAccountId, table.username] }),
  foreignKey({ columns: [table.igAccountId, table.userId], foreignColumns: [igAccounts.id, igAccounts.userId] }).onDelete("cascade"),
  check("onboarding_profile_cache_profile_check", sql`jsonb_typeof(${table.profile}) = 'object'`),
]);

export const profileCheckState = pgTable("profile_check_state", {
  userId: uuid("user_id").notNull(),
  igAccountId: uuid("ig_account_id").notNull(),
  nextAllowedAt: timestamp("next_allowed_at", { withTimezone: true }).notNull(),
  failureReason: text("failure_reason"),
}, (table) => [
  primaryKey({ columns: [table.userId, table.igAccountId] }),
  foreignKey({ columns: [table.igAccountId, table.userId], foreignColumns: [igAccounts.id, igAccounts.userId] }).onDelete("cascade"),
]);


export const relationshipReconciliationAttempts = pgTable("relationship_reconciliation_attempts", {
    id: uuid("id").primaryKey().defaultRandom(),
    userId: uuid("user_id").notNull(),
    igAccountId: uuid("ig_account_id").notNull(),
    relationshipId: uuid("relationship_id").notNull(),
    workerId: text("worker_id").notNull(),
    reservedAt: timestamp("reserved_at", { withTimezone: true }).notNull(),
    leaseExpiresAt: timestamp("lease_expires_at", { withTimezone: true }).notNull(),
    finishedAt: timestamp("finished_at", { withTimezone: true }),
    nextCheckAt: timestamp("next_check_at", { withTimezone: true }),
    outcome: text("outcome").notNull().default("pending"),
    failureCode: text("failure_code"),
}, (table) => [
    foreignKey({ columns: [table.relationshipId, table.igAccountId, table.userId], foreignColumns: [relationshipFollows.id, relationshipFollows.igAccountId, relationshipFollows.userId], name: "relationship_reconciliation_scope_fk" }).onDelete("cascade"),
    check("relationship_reconciliation_outcome_check", sql`${table.outcome} in ('pending','follow_back','not_following_back','unknown','deferred')`),
    check("relationship_reconciliation_lease_check", sql`${table.leaseExpiresAt} > ${table.reservedAt}`),
    check("relationship_reconciliation_finished_check", sql`(${table.outcome} = 'pending' and ${table.finishedAt} is null) or (${table.outcome} <> 'pending' and ${table.finishedAt} >= ${table.reservedAt})`),
    index("relationship_reconciliation_budget_idx").on(table.userId, table.igAccountId, table.reservedAt),
    index("relationship_reconciliation_due_idx").on(table.relationshipId, table.reservedAt),
]);

export const notificationRecipients = pgTable("notification_recipients", {
  userId: uuid("user_id").notNull().references(() => users.id, { onDelete: "cascade" }),
  email: text("email").notNull(),
  createdAt: createdAt(),
}, table => [primaryKey({ columns: [table.userId, table.email] })]);
