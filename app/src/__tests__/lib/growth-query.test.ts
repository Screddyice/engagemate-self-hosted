import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import {
  accountGrowthSnapshots,
  agentConfigs,
  igAccounts,
  users,
} from "@/db/schema";

vi.unmock("@/db");

const testDirectory = dirname(fileURLToPath(import.meta.url));
const appDirectory = resolve(testDirectory, "../../..");
const database = `engagemate_growth_query_${process.pid}`;
const databaseUrl = `postgresql://localhost/${database}`;

const PRIMARY_USER_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_USER_ID = "22222222-2222-4222-8222-222222222222";
const PRIMARY_ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SAME_USER_ACCOUNT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const OTHER_USER_ACCOUNT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

function command(
  program: string,
  args: string[],
  env: NodeJS.ProcessEnv = process.env,
) {
  return spawnSync(program, args, {
    cwd: appDirectory,
    encoding: "utf8",
    env,
  });
}

const postgresAvailable =
  command("pg_isready", []).status === 0 &&
  command("createdb", ["--version"]).status === 0;
const postgresDescribe = postgresAvailable
  ? describe.sequential
  : describe.skip;

let databaseCreated = false;
let previousDatabaseUrl: string | undefined;
let closeDatabaseClient: (() => Promise<void>) | undefined;
let getGrowthDashboard: typeof import("@/lib/queries")["getGrowthDashboard"];

postgresDescribe("getGrowthDashboard PostgreSQL query", () => {
  beforeAll(async () => {
    command("dropdb", ["--if-exists", database]);
    const created = command("createdb", [database]);
    expect(created.status, created.stderr).toBe(0);
    databaseCreated = true;

    const migrated = command(
      "npx",
      ["drizzle-kit", "migrate"],
      { ...process.env, DATABASE_URL: databaseUrl },
    );
    expect(
      migrated.status,
      `${migrated.stdout}\n${migrated.stderr}`,
    ).toBe(0);

    previousDatabaseUrl = process.env.DATABASE_URL;
    process.env.DATABASE_URL = databaseUrl;
    vi.resetModules();

    const databaseModule = await import("@/db");
    ({ getGrowthDashboard } = await import("@/lib/queries"));
    closeDatabaseClient = () =>
      databaseModule.db.$client.end({ timeout: 5 });

    await databaseModule.db.insert(users).values([
      {
        id: PRIMARY_USER_ID,
        ownerKey: "clerk_growth_primary",
        email: "growth-primary@example.test",
      },
      {
        id: OTHER_USER_ID,
        ownerKey: "clerk_growth_other",
        email: "growth-other@example.test",
      },
    ]);
    await databaseModule.db.insert(igAccounts).values([
      {
        id: PRIMARY_ACCOUNT_ID,
        userId: PRIMARY_USER_ID,
        username: "growth_primary",
        igUserId: "shared_instagram_identity",
      },
      {
        id: SAME_USER_ACCOUNT_ID,
        userId: PRIMARY_USER_ID,
        username: "growth_same_user",
      },
      {
        id: OTHER_USER_ACCOUNT_ID,
        userId: OTHER_USER_ID,
        username: "growth_other_user",
        igUserId: "shared_instagram_identity",
      },
    ]);
    await databaseModule.db.insert(agentConfigs).values({
      userId: PRIMARY_USER_ID,
      igAccountId: PRIMARY_ACCOUNT_ID,
      timezone: "UTC",
      growthGoal: 2_000,
    });

    await databaseModule.db.insert(accountGrowthSnapshots).values([
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-07-14",
        capturedAt: new Date("2026-07-14T12:00:00Z"),
        followerCount: 1_000,
        engagementRateBps: 400,
        status: "partial",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-07-15",
        capturedAt: new Date("2026-07-15T12:00:00Z"),
        followerCount: 1_010,
        engagementRateBps: null,
        status: "partial",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-08-06",
        capturedAt: new Date("2026-08-06T12:00:00Z"),
        followerCount: 1_020,
        engagementRateBps: null,
        status: "partial",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-08-07",
        capturedAt: new Date("2026-08-07T12:00:00Z"),
        followerCount: 1_030,
        engagementRateBps: null,
        status: "partial",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-08-08",
        capturedAt: new Date("2026-08-08T12:00:00Z"),
        followerCount: 1_040,
        engagementRateBps: null,
        status: "partial",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-08-12",
        capturedAt: new Date("2026-08-12T09:00:00Z"),
        followerCount: 1_045,
        engagementRateBps: 450,
        status: "partial",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-08-13",
        capturedAt: new Date("2026-08-13T12:00:00Z"),
        followerCount: null,
        engagementRateBps: 9_000,
        status: "unavailable",
        failureCode: "fetch_failed",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-08-14",
        capturedAt: new Date("2026-08-14T09:00:00Z"),
        followerCount: 1_050,
        engagementRateBps: null,
        status: "partial",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        snapshotDate: "2026-08-15",
        capturedAt: new Date("2026-08-15T09:00:00Z"),
        followerCount: 9_999,
        engagementRateBps: 9_999,
        status: "complete",
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: SAME_USER_ACCOUNT_ID,
        snapshotDate: "2026-08-14",
        capturedAt: new Date("2026-08-14T10:00:00Z"),
        followerCount: 8_000,
        engagementRateBps: 8_000,
        status: "complete",
      },
      {
        userId: OTHER_USER_ID,
        igAccountId: OTHER_USER_ACCOUNT_ID,
        snapshotDate: "2026-08-14",
        capturedAt: new Date("2026-08-14T11:00:00Z"),
        followerCount: 7_000,
        engagementRateBps: 7_000,
        status: "complete",
      },
    ]);
  }, 30_000);

  afterAll(async () => {
    if (closeDatabaseClient) {
      await closeDatabaseClient();
    }
    if (previousDatabaseUrl === undefined) {
      delete process.env.DATABASE_URL;
    } else {
      process.env.DATABASE_URL = previousDatabaseUrl;
    }
    vi.resetModules();

    if (databaseCreated) {
      const dropped = command("dropdb", ["--if-exists", database]);
      expect(dropped.status, dropped.stderr).toBe(0);
    }
  }, 30_000);

  it("isolates sparse metric anchors and chart points in PostgreSQL", async () => {
    const data = await getGrowthDashboard(
      PRIMARY_USER_ID,
      PRIMARY_ACCOUNT_ID,
      new Date("2026-08-14T12:00:00Z"),
    );

    expect(data.currentFollowers).toBe(1_050);
    expect(data.followerTrend7d).toEqual({
      start: 1_030,
      end: 1_050,
      delta: 20,
    });
    expect(data.followerTrend30d).toEqual({
      start: 1_010,
      end: 1_050,
      delta: 40,
    });
    expect(data.currentEngagementRateBps).toBe(450);
    expect(data.engagementTrend7d).toEqual({
      start: 400,
      end: 450,
      delta: 50,
    });
    expect(data.engagementTrend30d).toEqual({
      start: 400,
      end: 450,
      delta: 50,
    });
    expect(data.points).toEqual([
      { date: "2026-07-15", value: 1_010 },
      { date: "2026-08-06", value: 1_020 },
      { date: "2026-08-07", value: 1_030 },
      { date: "2026-08-08", value: 1_040 },
      { date: "2026-08-12", value: 1_045 },
      { date: "2026-08-14", value: 1_050 },
    ]);
    expect(data.growthGoal).toBe(2_000);
    expect(data.activity7d).toEqual({
      reviewed: 0,
      likes: 0,
      replies: 0,
      dms: 0,
      views: 0,
      skipped: 0,
      gated: 0,
      failed: 0,
    });
    expect(data.estimatedTimeSavedSeconds).toBe(0);
  });

  it("requires the user and account IDs to match one tenant", async () => {
    const data = await getGrowthDashboard(
      OTHER_USER_ID,
      PRIMARY_ACCOUNT_ID,
      new Date("2026-08-14T12:00:00Z"),
    );

    expect(data).toMatchObject({
      points: [],
      currentFollowers: null,
      currentEngagementRateBps: null,
      followerTrend7d: null,
      followerTrend30d: null,
      engagementTrend7d: null,
      engagementTrend30d: null,
      growthGoal: null,
    });
  });
});
