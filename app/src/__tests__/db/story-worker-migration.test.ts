import { afterAll, describe, expect, it } from "vitest";
import { existsSync, readFileSync } from "node:fs";
import { spawn, spawnSync, type ChildProcessWithoutNullStreams } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const testDirectory = dirname(fileURLToPath(import.meta.url));
const migrationsDirectory = resolve(testDirectory, "../../db/migrations");
const migrationPaths = [0, 1, 2].map((index) => {
  const filenames = [
    "0000_right_martin_li.sql",
    "0001_peaceful_wrecker.sql",
    "0002_story_worker.sql",
  ];
  return resolve(migrationsDirectory, filenames[index]);
});
const migrationSql = readFileSync(migrationPaths[2], "utf8");
const snapshotJson = readFileSync(
  resolve(migrationsDirectory, "meta/0002_snapshot.json"),
  "utf8",
);
const agencyUsageMigrationPath = resolve(
  migrationsDirectory,
  "0004_agency_usage.sql",
);
const agencyUsageMigrationSql = existsSync(agencyUsageMigrationPath)
  ? readFileSync(agencyUsageMigrationPath, "utf8")
  : "";
const agencyUsageSnapshotPath = resolve(
  migrationsDirectory,
  "meta/0004_snapshot.json",
);
const agencyUsageSnapshotJson = existsSync(agencyUsageSnapshotPath)
  ? readFileSync(agencyUsageSnapshotPath, "utf8")
  : "";
const proxyCapacityMigrationPath = resolve(
  migrationsDirectory,
  "0005_proxy_capacity.sql",
);
const proxyCapacityMigrationSql = existsSync(proxyCapacityMigrationPath)
  ? readFileSync(proxyCapacityMigrationPath, "utf8")
  : "";
const proxyCapacitySnapshotPath = resolve(
  migrationsDirectory,
  "meta/0005_snapshot.json",
);
const proxyCapacitySnapshotJson = existsSync(proxyCapacitySnapshotPath)
  ? readFileSync(proxyCapacitySnapshotPath, "utf8")
  : "";
const proxyInventoryScopeMigrationPath = resolve(
  migrationsDirectory,
  "0006_proxy_inventory_scope.sql",
);
const proxyInventoryScopeMigrationSql = existsSync(proxyInventoryScopeMigrationPath)
  ? readFileSync(proxyInventoryScopeMigrationPath, "utf8")
  : "";
const proxyInventoryScopeSnapshotPath = resolve(
  migrationsDirectory,
  "meta/0006_snapshot.json",
);
const proxyInventoryScopeSnapshotJson = existsSync(proxyInventoryScopeSnapshotPath)
  ? readFileSync(proxyInventoryScopeSnapshotPath, "utf8")
  : "";
const growthMigrationPath = resolve(
  migrationsDirectory,
  "0012_growth_reporting.sql",
);
const growthMigrationSql = existsSync(growthMigrationPath)
  ? readFileSync(growthMigrationPath, "utf8")
  : "";
const growthSnapshotPath = resolve(
  migrationsDirectory,
  "meta/0012_snapshot.json",
);
const growthSnapshotJson = existsSync(growthSnapshotPath)
  ? readFileSync(growthSnapshotPath, "utf8")
  : "";
const journal = JSON.parse(
  readFileSync(resolve(migrationsDirectory, "meta/_journal.json"), "utf8"),
) as { entries: Array<{ idx: number; tag: string; when: number }> };

function command(
  program: string,
  args: string[],
  env: NodeJS.ProcessEnv = process.env,
) {
  return spawnSync(program, args, {
    encoding: "utf8",
    env,
  });
}

function psql(database: string, sql: string) {
  return command("psql", [
    "-v",
    "ON_ERROR_STOP=1",
    "-At",
    "-d",
    database,
    "-c",
    sql,
  ]);
}

function applyMigration(database: string, path: string) {
  return command("psql", [
    "-v",
    "ON_ERROR_STOP=1",
    "-q",
    "-d",
    database,
    "-f",
    path,
  ]);
}

function startPsql(
  database: string,
  args: string[],
  applicationName: string,
) {
  const child = spawn(
    "psql",
    ["-X", "-v", "ON_ERROR_STOP=1", "-qAt", "-d", database, ...args],
    {
      env: { ...process.env, PGAPPNAME: applicationName },
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  let stdout = "";
  let stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk.toString();
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString();
  });
  const completed = new Promise<{
    status: number | null;
    stdout: string;
    stderr: string;
  }>((resolveResult) => {
    child.on("close", (status) => {
      resolveResult({ status, stdout, stderr });
    });
  });
  return { child, completed };
}

async function waitForDatabaseCondition(
  database: string,
  sql: string,
  attempts = 200,
) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const result = psql(database, sql);
    if (result.status === 0 && result.stdout.trim() === "t") {
      return;
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 10));
  }
  throw new Error(`database condition timed out: ${sql}`);
}

function stopProcess(child: ChildProcessWithoutNullStreams) {
  if (child.exitCode === null) {
    child.kill("SIGTERM");
  }
}

const postgresAvailable =
  command("pg_isready", []).status === 0 &&
  command("createdb", ["--version"]).status === 0;
const postgresDescribe = postgresAvailable ? describe.sequential : describe.skip;
const databases = new Set<string>();

function createLegacyDatabase(suffix: string) {
  const database = `engagemate_story_worker_${process.pid}_${suffix}`;
  command("dropdb", ["--if-exists", database]);
  const created = command("createdb", [database]);
  expect(created.status, created.stderr).toBe(0);
  databases.add(database);

  for (const path of migrationPaths.slice(0, 2)) {
    const result = applyMigration(database, path);
    expect(result.status, result.stderr).toBe(0);
  }

  return database;
}

afterAll(
  () => {
    databases.forEach((database) => {
      command("dropdb", ["--if-exists", database]);
    });
  },
  30_000,
);

describe("story worker migration contract", () => {
  it("wraps destructive DDL in a fail-closed transaction", () => {
    expect(migrationSql.trimStart().startsWith("DO $migration$")).toBe(true);
    expect(migrationSql).not.toContain("--> statement-breakpoint");
    expect(migrationSql).not.toMatch(/^BEGIN;|^COMMIT;/m);
    expect(migrationSql.indexOf("LOCK TABLE")).toBeGreaterThanOrEqual(0);
    expect(migrationSql.indexOf("LOCK TABLE")).toBeLessThan(
      migrationSql.indexOf("FOREACH guarded_table"),
    );
    expect(migrationSql.indexOf("FOREACH guarded_table")).toBeLessThan(
      migrationSql.indexOf('CREATE TABLE "account_health_events"'),
    );
    expect(migrationSql.trimEnd().endsWith("$migration$;")).toBe(true);

    for (const table of [
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
    ]) {
      expect(migrationSql).toContain(`'${table}'`);
      expect(migrationSql).toContain(`"${table}"`);
    }
    expect(migrationSql).toContain('"users"\n\tIN ACCESS EXCLUSIVE MODE');
  });

  it("keeps generated tenant constraints aligned with the snapshot", () => {
    for (const constraint of [
      "ig_accounts_id_user_unique",
      "ig_accounts_user_ig_user_id_idx",
      "ig_sessions_account_user_fk",
      "proxy_bindings_account_user_fk",
      "fingerprints_account_user_fk",
      "agent_configs_account_user_fk",
      "targets_account_user_fk",
      "targets_identity_unique",
      "jobs_account_user_fk",
      "jobs_identity_unique",
      "story_engagements_account_user_fk",
      "story_engagements_target_scope_fk",
      "account_health_events_account_user_fk",
      "run_summaries_account_user_fk",
      "run_summaries_job_scope_fk",
      "proxy_bindings_active_proxy_idx",
    ]) {
      expect(migrationSql).toContain(`"${constraint}"`);
      expect(snapshotJson).toContain(`"${constraint}"`);
    }
  });
});

describe("agency usage migration contract", () => {
  it("adds the reservation ledger without destructive DDL", () => {
    expect(agencyUsageMigrationSql).toContain(
      'CREATE TABLE "engagement_usage_events"',
    );
    expect(agencyUsageMigrationSql).toContain(
      'ADD COLUMN "stripe_current_period_start"',
    );
    expect(agencyUsageMigrationSql).toContain(
      'ADD COLUMN "stripe_current_period_end"',
    );
    expect(agencyUsageMigrationSql).toContain('ADD COLUMN "overage_enabled"');
    expect(agencyUsageMigrationSql).toContain('ADD COLUMN "overage_cap_cents"');
    expect(agencyUsageMigrationSql).not.toMatch(/DROP TABLE|DROP COLUMN|RENAME/i);
  });

  it("includes idempotency, tenant scope, state checks, and reporting index", () => {
    for (const constraint of [
      "engagement_usage_events_engagement_id_unique",
      "engagement_usage_events_account_user_fk",
      "engagement_usage_events_engagement_scope_fk",
      "engagement_usage_events_surface_check",
      "engagement_usage_events_action_check",
      "engagement_usage_events_status_check",
      "engagement_usage_events_quantity_check",
      "engagement_usage_events_user_occurred_status_idx",
    ]) {
      expect(agencyUsageMigrationSql).toContain(`"${constraint}"`);
      expect(agencyUsageSnapshotJson).toContain(`"${constraint}"`);
    }
  });

  it("creates the Story identity key before its usage foreign key", () => {
    expect(
      agencyUsageMigrationSql.indexOf('"story_engagements_identity_unique"'),
    ).toBeLessThan(
      agencyUsageMigrationSql.indexOf(
        '"engagement_usage_events_engagement_scope_fk"',
      ),
    );
  });

  it("adds non-negative aggregate cost telemetry", () => {
    for (const column of [
      "input_tokens",
      "output_tokens",
      "images_analyzed",
      "video_frames_analyzed",
      "retries",
      "runtime_ms",
    ]) {
      expect(agencyUsageMigrationSql).toContain(`"${column}"`);
      expect(agencyUsageSnapshotJson).toContain(`"${column}"`);
    }
    expect(agencyUsageMigrationSql).toContain(
      '"run_summaries_cost_counters_check"',
    );
    expect(agencyUsageSnapshotJson).toContain(
      '"run_summaries_cost_counters_check"',
    );
  });
});

describe("proxy capacity migration contract", () => {
  it("tracks at-most-once provider purchase intents and spend", () => {
    expect(proxyCapacityMigrationSql).toContain(
      'CREATE TABLE "proxy_capacity_orders"',
    );
    for (const column of [
      "country",
      "region",
      "city",
      "quantity",
      "quoted_cost_cents",
      "status",
      "provider_order_id",
      "provider_status",
    ]) {
      expect(proxyCapacityMigrationSql).toContain(`"${column}"`);
      expect(proxyCapacitySnapshotJson).toContain(`"${column}"`);
    }
    expect(proxyCapacityMigrationSql).toContain(
      'CONSTRAINT "proxy_capacity_orders_status_check"',
    );
    expect(proxyCapacityMigrationSql).toContain(
      'CREATE UNIQUE INDEX "proxy_capacity_orders_provider_order_idx"',
    );
    expect(proxyCapacityMigrationSql).not.toMatch(/DROP TABLE|DROP COLUMN|RENAME/i);
  });

  it("distinguishes encrypted spare inventory from account-bound credentials", () => {
    expect(proxyInventoryScopeMigrationSql).toContain(
      'ADD COLUMN "credential_scope"',
    );
    expect(proxyInventoryScopeMigrationSql).toContain(
      'CONSTRAINT "proxies_credential_scope_check"',
    );
    expect(proxyInventoryScopeSnapshotJson).toContain('"credential_scope"');
    expect(proxyInventoryScopeMigrationSql).not.toMatch(
      /DROP TABLE|DROP COLUMN|RENAME/i,
    );
  });
});

describe("growth reporting migration contract", () => {
  it("adds tenant-scoped growth and delivery tables without destructive DDL", () => {
    for (const table of [
      "account_growth_snapshots",
      "weekly_report_preferences",
      "weekly_report_deliveries",
    ]) {
      expect(growthMigrationSql).toContain(`CREATE TABLE "${table}"`);
      expect(growthSnapshotJson).toContain(`"public.${table}"`);
    }

    for (const constraint of [
      "account_growth_snapshots_account_user_fk",
      "account_growth_snapshots_account_date_idx",
      "account_growth_snapshots_status_check",
      "account_growth_snapshots_source_check",
      "account_growth_snapshots_counts_check",
      "account_growth_snapshots_follower_status_check",
      "account_growth_snapshots_failure_code_check",
      "weekly_report_preferences_send_hour_check",
      "weekly_report_deliveries_user_week_idx",
      "weekly_report_deliveries_status_check",
      "weekly_report_deliveries_attempts_check",
      "weekly_report_deliveries_lease_check",
      "weekly_report_deliveries_payload_hash_check",
      "weekly_report_deliveries_last_error_code_check",
    ]) {
      expect(growthMigrationSql).toContain(`"${constraint}"`);
      expect(growthSnapshotJson).toContain(`"${constraint}"`);
    }

    expect(growthMigrationSql).not.toMatch(/DROP TABLE|DROP COLUMN|RENAME/i);
  });

  it("backfills enabled manager preferences from the earliest account config", () => {
    expect(growthMigrationSql).toContain(
      'INSERT INTO "weekly_report_preferences"',
    );
    expect(growthMigrationSql).toContain(
      'ORDER BY "ig_accounts"."created_at", "ig_accounts"."id"',
    );
    expect(growthMigrationSql).toContain("'UTC'");
  });
});

postgresDescribe("story worker PostgreSQL migration", () => {
  it("backfills managers and enforces growth reporting tenant and retry keys", () => {
    const database = `engagemate_story_worker_${process.pid}_growth`;
    command("dropdb", ["--if-exists", database]);
    const created = command("createdb", [database]);
    expect(created.status, created.stderr).toBe(0);
    databases.add(database);

    for (const entry of journal.entries.slice(0, 12)) {
      const migrated = applyMigration(
        database,
        resolve(migrationsDirectory, `${entry.tag}.sql`),
      );
      expect(migrated.status, migrated.stderr).toBe(0);
    }

    const seeded = psql(
      database,
      `
        INSERT INTO users (id, clerk_id, email)
        VALUES
          ('10000000-0000-0000-0000-000000000001', 'growth-a', 'growth-a@example.com'),
          ('10000000-0000-0000-0000-000000000002', 'growth-b', 'growth-b@example.com');

        INSERT INTO ig_accounts (id, user_id, username, created_at)
        VALUES
          ('10000000-0000-0000-0000-000000000101', '10000000-0000-0000-0000-000000000001', 'growth-account-a1', '2026-01-01T00:00:00Z'),
          ('10000000-0000-0000-0000-000000000100', '10000000-0000-0000-0000-000000000001', 'growth-account-a0', '2026-01-01T00:00:00Z'),
          ('10000000-0000-0000-0000-000000000102', '10000000-0000-0000-0000-000000000001', 'growth-account-a2', '2026-01-02T00:00:00Z'),
          ('10000000-0000-0000-0000-000000000201', '10000000-0000-0000-0000-000000000002', 'growth-account-b', '2025-12-01T00:00:00Z');

        INSERT INTO agent_configs (
          id, user_id, ig_account_id, timezone, created_at
        )
        VALUES
          ('10000000-0000-0000-0000-000000000300', '10000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000100', 'America/Chicago', '2026-01-04T00:00:00Z'),
          ('10000000-0000-0000-0000-000000000301', '10000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000102', 'America/Los_Angeles', '2025-12-31T00:00:00Z'),
          ('10000000-0000-0000-0000-000000000302', '10000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000101', 'America/New_York', '2026-01-03T00:00:00Z'),
          ('10000000-0000-0000-0000-000000000303', '10000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000201', 'Pacific/Honolulu', '2025-12-01T00:00:00Z');
      `,
    );
    expect(seeded.status, seeded.stderr).toBe(0);

    const migrated = applyMigration(database, growthMigrationPath);
    expect(migrated.status, migrated.stderr).toBe(0);

    const preferences = psql(
      database,
      `
        SELECT user_id || ':' || enabled || ':' || timezone || ':' || send_hour
        FROM weekly_report_preferences
        ORDER BY user_id;
      `,
    );
    expect(preferences.status, preferences.stderr).toBe(0);
    expect(preferences.stdout.trim().split("\n")).toEqual([
      "10000000-0000-0000-0000-000000000001:true:America/Chicago:9",
      "10000000-0000-0000-0000-000000000002:true:Pacific/Honolulu:9",
    ]);

    const firstSnapshot = psql(
      database,
      `
        INSERT INTO account_growth_snapshots (
          user_id, ig_account_id, snapshot_date, follower_count,
          following_count, media_count, engagement_rate_bps,
          posts_sampled, status
        )
        VALUES (
          '10000000-0000-0000-0000-000000000001',
          '10000000-0000-0000-0000-000000000101',
          '2026-08-14', 100, 50, 10, 250, 5, 'complete'
        );
      `,
    );
    expect(firstSnapshot.status, firstSnapshot.stderr).toBe(0);

    const firstDelivery = psql(
      database,
      `
        INSERT INTO weekly_report_deliveries (
          user_id, week_start, recipient_email
        )
        VALUES (
          '10000000-0000-0000-0000-000000000001',
          '2026-08-10', 'growth-a@example.com'
        );
      `,
    );
    expect(firstDelivery.status, firstDelivery.stderr).toBe(0);

    const failures = [
      [
        "account_growth_snapshots_account_user_fk",
        `
          INSERT INTO account_growth_snapshots (
            user_id, ig_account_id, snapshot_date, follower_count, status
          )
          VALUES (
            '10000000-0000-0000-0000-000000000002',
            '10000000-0000-0000-0000-000000000101',
            '2026-08-15', 100, 'complete'
          );
        `,
      ],
      [
        "weekly_report_preferences_send_hour_check",
        `
          UPDATE weekly_report_preferences SET send_hour = 24
          WHERE user_id = '10000000-0000-0000-0000-000000000001';
        `,
      ],
      [
        "account_growth_snapshots_counts_check",
        `
          INSERT INTO account_growth_snapshots (
            user_id, ig_account_id, snapshot_date, follower_count, status
          )
          VALUES (
            '10000000-0000-0000-0000-000000000001',
            '10000000-0000-0000-0000-000000000101',
            '2026-08-15', -1, 'complete'
          );
        `,
      ],
      [
        "account_growth_snapshots_account_date_idx",
        `
          INSERT INTO account_growth_snapshots (
            user_id, ig_account_id, snapshot_date, follower_count, status
          )
          VALUES (
            '10000000-0000-0000-0000-000000000001',
            '10000000-0000-0000-0000-000000000101',
            '2026-08-14', 101, 'complete'
          );
        `,
      ],
      [
        "weekly_report_deliveries_user_week_idx",
        `
          INSERT INTO weekly_report_deliveries (
            user_id, week_start, recipient_email
          )
          VALUES (
            '10000000-0000-0000-0000-000000000001',
            '2026-08-10', 'growth-a@example.com'
          );
        `,
      ],
    ] as const;

    for (const [constraint, sql] of failures) {
      const result = psql(database, sql);
      expect(result.status).not.toBe(0);
      expect(`${result.stdout}\n${result.stderr}`).toContain(constraint);
    }

    const unavailable = psql(
      database,
      `
        INSERT INTO account_growth_snapshots (
          user_id, ig_account_id, snapshot_date, follower_count,
          status, failure_code
        )
        VALUES (
          '10000000-0000-0000-0000-000000000001',
          '10000000-0000-0000-0000-000000000101',
          '2026-08-15', NULL, 'unavailable', 'PROFILE_UNAVAILABLE'
        );
      `,
    );
    expect(unavailable.status, unavailable.stderr).toBe(0);
  }, 15_000);

  it("rolls back before destructive DDL when legacy rows exist", () => {
    const database = createLegacyDatabase("guard");
    const inserted = psql(
      database,
      "INSERT INTO organizations (name, slug) VALUES ('Sentinel', 'sentinel');",
    );
    expect(inserted.status, inserted.stderr).toBe(0);

    const migrated = applyMigration(database, migrationPaths[2]);
    expect(migrated.status).not.toBe(0);
    expect(`${migrated.stdout}\n${migrated.stderr}`).toContain(
      'requires empty legacy table "organizations"',
    );

    const sentinel = psql(
      database,
      "SELECT slug FROM organizations WHERE slug = 'sentinel';",
    );
    expect(sentinel.status, sentinel.stderr).toBe(0);
    expect(sentinel.stdout.trim()).toBe("sentinel");

    const legacySchema = psql(
      database,
      `
        SELECT bool_and(to_regclass('public.' || table_name) IS NOT NULL)
        FROM unnest(ARRAY[
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
          'usage_records',
          'users'
        ]) AS table_name;
      `,
    );
    expect(legacySchema.status, legacySchema.stderr).toBe(0);
    expect(legacySchema.stdout.trim()).toBe("t");

    const newTable = psql(
      database,
      "SELECT to_regclass('public.ig_accounts') IS NULL;",
    );
    expect(newTable.status, newTable.stderr).toBe(0);
    expect(newTable.stdout.trim()).toBe("t");
  }, 15_000);

  it("migrates an empty baseline and rejects cross-tenant references", () => {
    const database = createLegacyDatabase("constraints");
    const legacyUser = psql(
      database,
      `
        INSERT INTO users (
          id, clerk_id, email, created_at, updated_at
        )
        VALUES (
          '00000000-0000-0000-0000-000000000099',
          'legacy-user', 'legacy@example.com', NULL, NULL
        );
      `,
    );
    expect(legacyUser.status, legacyUser.stderr).toBe(0);

    const migrated = applyMigration(database, migrationPaths[2]);
    expect(migrated.status, `${migrated.stdout}\n${migrated.stderr}`).toBe(0);

    const normalizedUser = psql(
      database,
      `
        SELECT created_at IS NOT NULL AND updated_at IS NOT NULL
        FROM users
        WHERE id = '00000000-0000-0000-0000-000000000099';
      `,
    );
    expect(normalizedUser.status, normalizedUser.stderr).toBe(0);
    expect(normalizedUser.stdout.trim()).toBe("t");

    const seeded = psql(
      database,
      `
        INSERT INTO users (id, clerk_id, email)
        VALUES
          ('00000000-0000-0000-0000-000000000001', 'clerk-a', 'a@example.com'),
          ('00000000-0000-0000-0000-000000000002', 'clerk-b', 'b@example.com');

        INSERT INTO ig_accounts (id, user_id, username)
        VALUES
          ('00000000-0000-0000-0000-000000000101', '00000000-0000-0000-0000-000000000001', 'account-a'),
          ('00000000-0000-0000-0000-000000000102', '00000000-0000-0000-0000-000000000002', 'account-b');

        INSERT INTO targets (id, user_id, ig_account_id, username, tier, source)
        VALUES
          ('00000000-0000-0000-0000-000000000201', '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000101', 'target-a', 'vip', 'manual'),
          ('00000000-0000-0000-0000-000000000202', '00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000102', 'target-b', 'vip', 'manual');

        INSERT INTO jobs (id, user_id, ig_account_id, schedule_key, scheduled_for)
        VALUES
          ('00000000-0000-0000-0000-000000000301', '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000101', 'job-a', now()),
          ('00000000-0000-0000-0000-000000000302', '00000000-0000-0000-0000-000000000002', '00000000-0000-0000-0000-000000000102', 'job-b', now());

        INSERT INTO proxies (
          id, provider, provider_reference, proxy_type, country, host, port,
          encrypted_username, encrypted_password
        )
        VALUES (
          '00000000-0000-0000-0000-000000000401',
          'test', 'proxy-a', 'static_residential', 'US', '127.0.0.1', 8080,
          'encrypted-user', 'encrypted-password'
        );
      `,
    );
    expect(seeded.status, seeded.stderr).toBe(0);

    const failures = [
      [
        "ig_accounts_user_ig_user_id_idx",
        `
          UPDATE ig_accounts
          SET ig_user_id = 'stable-instagram-user'
          WHERE id = '00000000-0000-0000-0000-000000000101';

          INSERT INTO ig_accounts (user_id, username, ig_user_id)
          VALUES (
            '00000000-0000-0000-0000-000000000001',
            'account-a-duplicate',
            'stable-instagram-user'
          );
        `,
      ],
      [
        "ig_sessions_account_user_fk",
        `
          INSERT INTO ig_sessions (
            user_id, ig_account_id, encrypted_cookie_blob,
            encryption_version, captured_user_agent
          )
          VALUES (
            '00000000-0000-0000-0000-000000000002',
            '00000000-0000-0000-0000-000000000101',
            'encrypted', 1, 'test-agent'
          );
        `,
      ],
      [
        "story_engagements_target_scope_fk",
        `
          INSERT INTO story_engagements (
            user_id, ig_account_id, target_id, story_fingerprint, action, status
          )
          VALUES (
            '00000000-0000-0000-0000-000000000001',
            '00000000-0000-0000-0000-000000000101',
            '00000000-0000-0000-0000-000000000202',
            'story-a', 'like', 'sent'
          );
        `,
      ],
      [
        "run_summaries_job_scope_fk",
        `
          INSERT INTO run_summaries (user_id, ig_account_id, job_id)
          VALUES (
            '00000000-0000-0000-0000-000000000001',
            '00000000-0000-0000-0000-000000000101',
            '00000000-0000-0000-0000-000000000302'
          );
        `,
      ],
    ] as const;

    for (const [constraint, sql] of failures) {
      const result = psql(database, sql);
      expect(result.status).not.toBe(0);
      expect(`${result.stdout}\n${result.stderr}`).toContain(constraint);
    }

    const firstBinding = psql(
      database,
      `
        INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id)
        VALUES (
          '00000000-0000-0000-0000-000000000001',
          '00000000-0000-0000-0000-000000000101',
          '00000000-0000-0000-0000-000000000401'
        );
      `,
    );
    expect(firstBinding.status, firstBinding.stderr).toBe(0);

    const sharedProxy = psql(
      database,
      `
        INSERT INTO proxy_bindings (user_id, ig_account_id, proxy_id)
        VALUES (
          '00000000-0000-0000-0000-000000000002',
          '00000000-0000-0000-0000-000000000102',
          '00000000-0000-0000-0000-000000000401'
        );
      `,
    );
    expect(sharedProxy.status).not.toBe(0);
    expect(`${sharedProxy.stdout}\n${sharedProxy.stderr}`).toContain(
      "proxy_bindings_active_proxy_idx",
    );
  });

  it("locks before checking and rejects a row committed while waiting", async () => {
    const database = createLegacyDatabase("concurrency");
    const writerName = `engagemate-writer-${process.pid}`;
    const migrationName = `engagemate-migration-${process.pid}`;
    const writer = startPsql(database, [], writerName);
    let migration:
      | ReturnType<typeof startPsql>
      | undefined;

    try {
      writer.child.stdin.write(`
        BEGIN;
        INSERT INTO organizations (name, slug)
        VALUES ('Concurrent Sentinel', 'concurrent-sentinel');
      `);

      await waitForDatabaseCondition(
        database,
        `
          SELECT EXISTS (
            SELECT 1
            FROM pg_locks AS locks
            JOIN pg_stat_activity AS activity
              ON activity.pid = locks.pid
            WHERE activity.application_name = '${writerName}'
              AND locks.relation = 'organizations'::regclass
              AND locks.mode = 'RowExclusiveLock'
              AND locks.granted
          );
        `,
      );

      migration = startPsql(
        database,
        ["-f", migrationPaths[2]],
        migrationName,
      );

      await waitForDatabaseCondition(
        database,
        `
          SELECT EXISTS (
            SELECT 1
            FROM pg_locks AS locks
            JOIN pg_stat_activity AS activity
              ON activity.pid = locks.pid
            WHERE activity.application_name = '${migrationName}'
              AND locks.mode = 'AccessExclusiveLock'
              AND NOT locks.granted
          );
        `,
      );
      expect(migration.child.exitCode).toBeNull();

      writer.child.stdin.end("COMMIT;\n\\q\n");
      const writerResult = await writer.completed;
      expect(writerResult.status, writerResult.stderr).toBe(0);

      const migrationResult = await migration.completed;
      expect(migrationResult.status).not.toBe(0);
      expect(
        `${migrationResult.stdout}\n${migrationResult.stderr}`,
      ).toContain('requires empty legacy table "organizations"');

      const preserved = psql(
        database,
        `
          SELECT
            EXISTS (
              SELECT 1 FROM organizations
              WHERE slug = 'concurrent-sentinel'
            )
            AND to_regclass('public.ig_accounts') IS NULL;
        `,
      );
      expect(preserved.status, preserved.stderr).toBe(0);
      expect(preserved.stdout.trim()).toBe("t");
    } finally {
      stopProcess(writer.child);
      if (migration) {
        stopProcess(migration.child);
      }
    }
  });

  it("applies every migration through Drizzle without transaction warnings", () => {
    const database = `engagemate_story_worker_${process.pid}_drizzle`;
    command("dropdb", ["--if-exists", database]);
    const created = command("createdb", [database]);
    expect(created.status, created.stderr).toBe(0);
    databases.add(database);

    const databaseUrl = `postgresql://localhost/${database}`;
    const migrated = command(
      "npx",
      ["drizzle-kit", "migrate"],
      { ...process.env, DATABASE_URL: databaseUrl },
    );
    expect(
      migrated.status,
      `${migrated.stdout}\n${migrated.stderr}`,
    ).toBe(0);
    expect(`${migrated.stdout}\n${migrated.stderr}`).not.toContain("25P01");
    expect(`${migrated.stdout}\n${migrated.stderr}`).not.toContain(
      "no transaction in progress",
    );

    const ledger = psql(
      database,
      `
        SELECT
          count(*) || ':' || max(created_at)
        FROM drizzle.__drizzle_migrations;
      `,
    );
    expect(ledger.status, ledger.stderr).toBe(0);
    expect(ledger.stdout.trim()).toBe(
      `${journal.entries.length}:${journal.entries.at(-1)?.when}`,
    );
  }, 15_000);

  it("keeps the 0002 ledger row out when Drizzle rolls back", () => {
    const database = createLegacyDatabase("drizzle_failure");
    const prepared = psql(
      database,
      `
        CREATE SCHEMA drizzle;
        CREATE TABLE drizzle.__drizzle_migrations (
          id serial PRIMARY KEY,
          hash text NOT NULL,
          created_at bigint
        );
        INSERT INTO drizzle.__drizzle_migrations (hash, created_at)
        VALUES
          ('baseline-0000', ${journal.entries[0].when}),
          ('baseline-0001', ${journal.entries[1].when});
        INSERT INTO organizations (name, slug)
        VALUES ('Drizzle Sentinel', 'drizzle-sentinel');
      `,
    );
    expect(prepared.status, prepared.stderr).toBe(0);

    const migrated = command(
      "npx",
      ["drizzle-kit", "migrate"],
      {
        ...process.env,
        DATABASE_URL: `postgresql://localhost/${database}`,
      },
    );
    expect(migrated.status).not.toBe(0);
    expect(`${migrated.stdout}\n${migrated.stderr}`).toContain(
      'requires empty legacy table "organizations"',
    );
    expect(`${migrated.stdout}\n${migrated.stderr}`).not.toContain("25P01");

    const ledger = psql(
      database,
      `
        SELECT
          count(*) || ':' || max(created_at)
        FROM drizzle.__drizzle_migrations;
      `,
    );
    expect(ledger.status, ledger.stderr).toBe(0);
    expect(ledger.stdout.trim()).toBe(
      `2:${journal.entries[1].when}`,
    );

    const sentinel = psql(
      database,
      `
        SELECT slug
        FROM organizations
        WHERE slug = 'drizzle-sentinel';
      `,
    );
    expect(sentinel.status, sentinel.stderr).toBe(0);
    expect(sentinel.stdout.trim()).toBe("drizzle-sentinel");
  }, 15_000);
});
