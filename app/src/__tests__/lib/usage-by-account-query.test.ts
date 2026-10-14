import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import {
  engagementUsageEvents,
  igAccounts,
  storyEngagements,
  targets,
  users,
} from "@/db/schema";

vi.unmock("@/db");

const testDirectory = dirname(fileURLToPath(import.meta.url));
const appDirectory = resolve(testDirectory, "../../..");
const database = `engagemate_usage_by_account_${process.pid}`;
const databaseUrl = `postgresql://localhost/${database}`;

const PRIMARY_USER_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_USER_ID = "22222222-2222-4222-8222-222222222222";
const PRIMARY_ACCOUNT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const SECOND_ACCOUNT_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const OTHER_ACCOUNT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const PRIMARY_TARGET_ID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
const SECOND_TARGET_ID = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const OTHER_TARGET_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff";

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
const postgresDescribe = postgresAvailable ? describe.sequential : describe.skip;

let databaseCreated = false;
let previousDatabaseUrl: string | undefined;
let closeDatabaseClient: (() => Promise<void>) | undefined;
let getUsageByAccount: typeof import("@/lib/queries")["getUsageByAccount"];

postgresDescribe("getUsageByAccount PostgreSQL query", () => {
  beforeAll(async () => {
    command("dropdb", ["--if-exists", database]);
    const created = command("createdb", [database]);
    expect(created.status, created.stderr).toBe(0);
    databaseCreated = true;

    const migrated = command("npx", ["drizzle-kit", "migrate"], {
      ...process.env,
      DATABASE_URL: databaseUrl,
    });
    expect(migrated.status, `${migrated.stdout}\n${migrated.stderr}`).toBe(0);

    previousDatabaseUrl = process.env.DATABASE_URL;
    process.env.DATABASE_URL = databaseUrl;
    vi.resetModules();

    const databaseModule = await import("@/db");
    ({ getUsageByAccount } = await import("@/lib/queries"));
    closeDatabaseClient = () => databaseModule.db.$client.end({ timeout: 5 });

    await databaseModule.db.insert(users).values([
      {
        id: PRIMARY_USER_ID,
        ownerKey: "clerk_usage_primary",
        email: "usage-primary@example.test",
      },
      {
        id: OTHER_USER_ID,
        ownerKey: "clerk_usage_other",
        email: "usage-other@example.test",
      },
    ]);
    await databaseModule.db.insert(igAccounts).values([
      {
        id: PRIMARY_ACCOUNT_ID,
        userId: PRIMARY_USER_ID,
        username: "usage_primary",
      },
      {
        id: SECOND_ACCOUNT_ID,
        userId: PRIMARY_USER_ID,
        username: "usage_second",
      },
      {
        id: OTHER_ACCOUNT_ID,
        userId: OTHER_USER_ID,
        username: "usage_other",
      },
    ]);
    await databaseModule.db.insert(targets).values([
      {
        id: PRIMARY_TARGET_ID,
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        username: "usage_target_primary",
        tier: "vip",
        source: "manual",
      },
      {
        id: SECOND_TARGET_ID,
        userId: PRIMARY_USER_ID,
        igAccountId: SECOND_ACCOUNT_ID,
        username: "usage_target_second",
        tier: "vip",
        source: "manual",
      },
      {
        id: OTHER_TARGET_ID,
        userId: OTHER_USER_ID,
        igAccountId: OTHER_ACCOUNT_ID,
        username: "usage_target_other",
        tier: "vip",
        source: "manual",
      },
    ]);

    const engagements: (typeof storyEngagements.$inferInsert)[] = [
      {
        id: "10000000-0000-4000-8000-000000000001",
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        targetId: PRIMARY_TARGET_ID,
        storyFingerprint: "primary-current",
        action: "like",
        status: "sent",
      },
      {
        id: "10000000-0000-4000-8000-000000000002",
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        targetId: PRIMARY_TARGET_ID,
        storyFingerprint: "primary-reserved",
        action: "dm",
        status: "sent",
      },
      {
        id: "10000000-0000-4000-8000-000000000003",
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        targetId: PRIMARY_TARGET_ID,
        storyFingerprint: "primary-before-period",
        action: "reply",
        status: "sent",
      },
      {
        id: "10000000-0000-4000-8000-000000000004",
        userId: PRIMARY_USER_ID,
        igAccountId: SECOND_ACCOUNT_ID,
        targetId: SECOND_TARGET_ID,
        storyFingerprint: "second-current",
        action: "reply",
        status: "sent",
      },
      {
        id: "10000000-0000-4000-8000-000000000005",
        userId: OTHER_USER_ID,
        igAccountId: OTHER_ACCOUNT_ID,
        targetId: OTHER_TARGET_ID,
        storyFingerprint: "other-current",
        action: "like",
        status: "sent",
      },
      {
        id: "10000000-0000-4000-8000-000000000006",
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        targetId: PRIMARY_TARGET_ID,
        storyFingerprint: "primary-at-period-start",
        action: "like",
        status: "sent",
      },
      {
        id: "10000000-0000-4000-8000-000000000007",
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        targetId: PRIMARY_TARGET_ID,
        storyFingerprint: "primary-at-period-end",
        action: "like",
        status: "sent",
      },
    ];
    await databaseModule.db.insert(storyEngagements).values(engagements);

    await databaseModule.db.insert(engagementUsageEvents).values([
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        engagementId: engagements[0].id!,
        action: engagements[0].action,
        status: "billable",
        occurredAt: new Date("2026-08-10T12:00:00Z"),
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        engagementId: engagements[1].id!,
        action: engagements[1].action,
        status: "reserved",
        reservedAt: new Date("2026-08-11T12:00:00Z"),
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        engagementId: engagements[2].id!,
        action: engagements[2].action,
        status: "billable",
        occurredAt: new Date("2026-07-31T23:59:59Z"),
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: SECOND_ACCOUNT_ID,
        engagementId: engagements[3].id!,
        action: engagements[3].action,
        status: "billable",
        occurredAt: new Date("2026-08-12T12:00:00Z"),
      },
      {
        userId: OTHER_USER_ID,
        igAccountId: OTHER_ACCOUNT_ID,
        engagementId: engagements[4].id!,
        action: engagements[4].action,
        status: "billable",
        occurredAt: new Date("2026-08-13T12:00:00Z"),
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        engagementId: engagements[5].id!,
        action: engagements[5].action,
        status: "billable",
        occurredAt: new Date("2026-08-01T00:00:00Z"),
      },
      {
        userId: PRIMARY_USER_ID,
        igAccountId: PRIMARY_ACCOUNT_ID,
        engagementId: engagements[6].id!,
        action: engagements[6].action,
        status: "billable",
        occurredAt: new Date("2026-09-01T00:00:00Z"),
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

  it("serializes period boundaries and isolates usage to the requested user", async () => {
    const rows = await getUsageByAccount(
      PRIMARY_USER_ID,
      new Date("2026-08-01T00:00:00Z"),
      new Date("2026-09-01T00:00:00Z"),
    );

    expect(
      rows
        .map((row) => ({
          igAccountId: row.igAccountId,
          billable: Number(row.billable),
          reserved: Number(row.reserved),
        }))
        .sort((left, right) =>
          left.igAccountId.localeCompare(right.igAccountId),
        ),
    ).toEqual([
      { igAccountId: PRIMARY_ACCOUNT_ID, billable: 2, reserved: 1 },
      { igAccountId: SECOND_ACCOUNT_ID, billable: 1, reserved: 0 },
    ]);
  });

  it("includes usage at the exact period start", async () => {
    const rows = await getUsageByAccount(
      PRIMARY_USER_ID,
      new Date("2026-08-01T00:00:00Z"),
      new Date("2026-08-01T00:00:01Z"),
    );
    const primary = rows.find((row) => row.igAccountId === PRIMARY_ACCOUNT_ID);

    expect(Number(primary?.billable)).toBe(1);
    expect(Number(primary?.reserved)).toBe(0);
  });

  it("excludes usage at the exact period end", async () => {
    const rows = await getUsageByAccount(
      PRIMARY_USER_ID,
      new Date("2026-08-31T23:59:59Z"),
      new Date("2026-09-01T00:00:00Z"),
    );
    const primary = rows.find((row) => row.igAccountId === PRIMARY_ACCOUNT_ID);

    expect(Number(primary?.billable)).toBe(0);
    expect(Number(primary?.reserved)).toBe(0);
  });
});
