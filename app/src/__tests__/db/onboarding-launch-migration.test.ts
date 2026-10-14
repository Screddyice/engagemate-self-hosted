import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { getTableConfig } from "drizzle-orm/pg-core";

import { brandProfiles } from "@/db/schema";

const testDirectory = dirname(fileURLToPath(import.meta.url));
const migrationsDirectory = resolve(testDirectory, "../../db/migrations");
const migrationPath = resolve(migrationsDirectory, "0015_onboarding_launch_state.sql");
const database = `engagemate_onboarding_migration_${process.pid}`;

function command(program: string, args: string[]) {
  return spawnSync(program, args, { encoding: "utf8" });
}

function psql(sql: string) {
  return command("psql", ["-X", "-v", "ON_ERROR_STOP=1", "-At", "-d", database, "-c", sql]);
}

const postgresAvailable =
  command("pg_isready", []).status === 0 && command("createdb", [database]).status === 0;
const postgresDescribe = postgresAvailable ? describe.sequential : describe.skip;

describe("onboarding launch state Drizzle schema", () => {
  it("scopes brand profiles to a tenant-owned Instagram account", () => {
    const config = getTableConfig(brandProfiles);
    const accountColumn = config.columns.find(
      (column) => column.name === "ig_account_id",
    );
    const userAccountIndex = config.indexes.find(
      (index) => index.config.name === "brand_profiles_user_idx",
    );
    const accountUserForeignKey = config.foreignKeys.find(
      (foreignKey) => foreignKey.getName() === "brand_profiles_account_user_fk",
    );

    expect(accountColumn).toBeDefined();
    expect(accountColumn?.notNull).toBe(false);
    expect(userAccountIndex?.config.unique).toBe(true);
    expect(
      userAccountIndex?.config.columns.map((column) =>
        "name" in column ? column.name : undefined,
      ),
    ).toEqual(["user_id", "ig_account_id"]);
    expect(accountUserForeignKey).toBeDefined();
    expect(
      accountUserForeignKey?.reference().columns.map((column) => column.name),
    ).toEqual(["ig_account_id", "user_id"]);
    expect(
      accountUserForeignKey?.reference().foreignColumns.map(
        (column) => column.name,
      ),
    ).toEqual(["id", "user_id"]);
    expect(accountUserForeignKey?.onDelete).toBe("cascade");
  });
});

postgresDescribe("onboarding launch state PostgreSQL migration", () => {
  beforeAll(() => {
    const setup = psql(`
      CREATE TABLE ig_accounts (
        id uuid PRIMARY KEY,
        user_id uuid NOT NULL,
        warmup_started_at timestamp with time zone,
        UNIQUE (id, user_id)
      );
      CREATE TABLE brand_profiles (
        id uuid PRIMARY KEY,
        label text NOT NULL,
        user_id uuid NOT NULL
      );
      CREATE UNIQUE INDEX brand_profiles_user_idx ON brand_profiles (user_id);
      CREATE TABLE agent_configs (
        id uuid PRIMARY KEY,
        label text NOT NULL,
        user_id uuid NOT NULL,
        ig_account_id uuid NOT NULL REFERENCES ig_accounts(id),
        enabled boolean NOT NULL DEFAULT false,
        created_at timestamp with time zone NOT NULL,
        updated_at timestamp with time zone NOT NULL
      );
      CREATE TABLE ig_sessions (
        id uuid PRIMARY KEY,
        user_id uuid NOT NULL,
        ig_account_id uuid NOT NULL
      );
      CREATE TABLE run_summaries (
        id uuid PRIMARY KEY,
        user_id uuid NOT NULL,
        ig_account_id uuid NOT NULL
      );

      INSERT INTO ig_accounts (id, user_id, warmup_started_at) VALUES
        ('00000000-0000-0000-0000-000000000001',
         '90000000-0000-0000-0000-000000000001', NULL),
        ('00000000-0000-0000-0000-000000000002',
         '90000000-0000-0000-0000-000000000001', NULL),
        ('00000000-0000-0000-0000-000000000003',
         '90000000-0000-0000-0000-000000000001', '2026-08-01T00:00:00Z'),
        ('00000000-0000-0000-0000-000000000004',
         '90000000-0000-0000-0000-000000000001', NULL),
        ('00000000-0000-0000-0000-000000000101',
         '90000000-0000-0000-0000-000000000101', NULL),
        ('00000000-0000-0000-0000-000000000102',
         '90000000-0000-0000-0000-000000000102', NULL),
        ('00000000-0000-0000-0000-000000000103',
         '90000000-0000-0000-0000-000000000102', NULL);
      INSERT INTO brand_profiles (id, label, user_id) VALUES
        (
          '30000000-0000-0000-0000-000000000101', 'single-account',
          '90000000-0000-0000-0000-000000000101'
        ),
        (
          '30000000-0000-0000-0000-000000000102', 'multiple-accounts',
          '90000000-0000-0000-0000-000000000102'
        );
      INSERT INTO agent_configs (
        id, label, user_id, ig_account_id, enabled, created_at, updated_at
      ) VALUES
        (
          '10000000-0000-0000-0000-000000000001', 'unfinished',
          '90000000-0000-0000-0000-000000000001',
          '00000000-0000-0000-0000-000000000001', false,
          '2026-07-01T00:00:00Z', '2026-08-01T00:00:00Z'
        ),
        (
          '10000000-0000-0000-0000-000000000002', 'enabled',
          '90000000-0000-0000-0000-000000000001',
          '00000000-0000-0000-0000-000000000002', true,
          '2026-07-02T00:00:00Z', '2026-08-02T00:00:00Z'
        ),
        (
          '10000000-0000-0000-0000-000000000003', 'connected-not-launched',
          '90000000-0000-0000-0000-000000000001',
          '00000000-0000-0000-0000-000000000003', false,
          '2026-07-03T00:00:00Z', '2026-08-03T00:00:00Z'
        ),
        (
          '10000000-0000-0000-0000-000000000004', 'paused-after-run',
          '90000000-0000-0000-0000-000000000001',
          '00000000-0000-0000-0000-000000000004', false,
          '2026-07-04T00:00:00Z', '2026-08-04T00:00:00Z'
        );
      INSERT INTO run_summaries (id, user_id, ig_account_id) VALUES
        ('20000000-0000-0000-0000-000000000004',
         '90000000-0000-0000-0000-000000000001',
         '00000000-0000-0000-0000-000000000004');
    `);
    expect(setup.status, setup.stderr).toBe(0);
  });

  afterAll(() => {
    if (postgresAvailable) command("dropdb", ["--if-exists", "--force", database]);
  }, 30_000);

  it("backfills only configurations with durable evidence of a prior launch", () => {
    const migrated = command("psql", [
      "-X", "-v", "ON_ERROR_STOP=1", "-q", "-d", database, "-f", migrationPath,
    ]);
    expect(migrated.status, migrated.stderr).toBe(0);

    const result = psql(`
      SELECT label || ':' ||
        CASE
          WHEN launched_at IS NULL THEN 'null'
          WHEN launched_at = updated_at THEN 'updated_at'
          ELSE 'unexpected'
        END
      FROM agent_configs
      ORDER BY label;
    `);
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout.trim().split("\n")).toEqual([
      "connected-not-launched:null",
      "enabled:updated_at",
      "paused-after-run:updated_at",
      "unfinished:null",
    ]);
  });

  it("scopes only legacy brand profiles with one unambiguous account", () => {
    const result = psql(`
      SELECT label || ':' || COALESCE(ig_account_id::text, 'null')
      FROM brand_profiles
      ORDER BY label;
    `);
    expect(result.status, result.stderr).toBe(0);
    expect(result.stdout.trim().split("\n")).toEqual([
      "multiple-accounts:null",
      "single-account:00000000-0000-0000-0000-000000000101",
    ]);
  });
});
