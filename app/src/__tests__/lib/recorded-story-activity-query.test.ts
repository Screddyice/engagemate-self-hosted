import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import {
  igAccounts,
  storyEngagements,
  relationshipFollows,
  targets,
  users,
} from "@/db/schema";

vi.unmock("@/db");

const testDirectory = dirname(fileURLToPath(import.meta.url));
const appDirectory = resolve(testDirectory, "../../..");
const database = `engagemate_recorded_activity_${process.pid}`;
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
let getRecordedStoryActivity: typeof import("@/lib/queries")["getRecordedStoryActivity"];
let getRelationshipSummary: typeof import("@/lib/queries")["getRelationshipSummary"];
let getActionCountsSince: typeof import("@/lib/queries")["getActionCountsSince"];

postgresDescribe("recorded Story activity PostgreSQL query", () => {
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
    ({ getRecordedStoryActivity, getActionCountsSince, getRelationshipSummary } = await import("@/lib/queries"));
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

    const rows = [
      [PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, PRIMARY_TARGET_ID, "like", "sent", "2026-09-22T06:59:59Z"],
      [PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, PRIMARY_TARGET_ID, "like", "sent", "2026-09-22T07:00:00Z"],
      [PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, PRIMARY_TARGET_ID, "view", "sent", "2026-09-22T08:00:00Z"],
      [PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, PRIMARY_TARGET_ID, "reply", "failed", "2026-09-22T08:00:00Z"],
      [PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, PRIMARY_TARGET_ID, "reply", "sent", null],
      [PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, PRIMARY_TARGET_ID, "like", "sent", "2026-09-23T08:00:00Z"],
      [PRIMARY_USER_ID, SECOND_ACCOUNT_ID, SECOND_TARGET_ID, "like", "sent", "2026-09-22T08:00:00Z"],
      [OTHER_USER_ID, OTHER_ACCOUNT_ID, OTHER_TARGET_ID, "like", "sent", "2026-09-22T08:00:00Z"],
      ...["2026-03-08T07:59:59Z", "2026-03-08T08:00:00Z", "2026-11-01T06:59:59Z", "2026-11-01T07:00:00Z"].map(time => [PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, PRIMARY_TARGET_ID, "dm", "sent", time]),
    ];
    const inserted = await databaseModule.db.insert(storyEngagements).values(rows.map((row, i) => ({
      userId: row[0]!, igAccountId: row[1]!, targetId: row[2]!,
      action: row[3]!, status: row[4]!, executedAt: row[5] ? new Date(row[5]) : null,
      createdAt: new Date("2026-01-01T00:00:00Z"), storyFingerprint: `activity-${i}`,
    }))).returning();
    const baseline = { outcome: "confirmed", followed_by: false };
    await databaseModule.db.insert(relationshipFollows).values([
      {ownerId:"1",status:"unfollowed", evidence:{follow:baseline,unfollow:{outcome:"confirmed",identity_matches:true,followed_by:true},reconciliation:{identity_matches:true,owner_id:"999",followed_by:true,observed_at:"2026-09-22T11:00:00Z"}}},
      {ownerId:"2",status:"unfollowed", evidence:{follow:baseline,unfollow:{outcome:"already_absent",identity_matches:true,followed_by:false},reconciliation:{identity_matches:true,owner_id:"2",followed_by:false,observed_at:"2026-09-20T11:00:00Z"}}},
      {ownerId:"3",status:"unfollowed", evidence:{follow:{outcome:"confirmed"},unfollow:{outcome:"already_absent",identity_matches:true,followed_by:true}}},
      {ownerId:"4",status:"following", evidence:{follow:baseline,
        first_follow_back:{identity_matches:true,owner_id:"4",followed_by:true,observed_at:"2026-09-21T13:00:00Z"},
        reconciliation:{identity_matches:true,owner_id:"4",followed_by:false,observed_at:"2026-09-22T11:00:00Z"}}},
    ].map(row => ({...row, userId:PRIMARY_USER_ID,igAccountId:PRIMARY_ACCOUNT_ID,targetId:PRIMARY_TARGET_ID,
      prerequisiteEngagementId:inserted[0].id,
      confirmedFollowAt:new Date("2026-09-16T12:00:00Z"),unfollowDueAt:new Date("2026-09-21T12:00:00Z"),
      unfollowedAt:row.status === "unfollowed" ? new Date("2026-09-21T13:00:00Z") : null,
    })));
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

  it("uses Pacific execution dates and excludes failed, unexecuted, future and other-account actions", async () => {
    const rows = await getRecordedStoryActivity(PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, new Date("2026-09-22T12:00:00Z"));
    expect(rows.find(row => row.action === "like")).toEqual({ action: "like", total: 2, today: 1 });
    expect(rows.find(row => row.action === "view")).toEqual({ action: "view", total: 1, today: 1 });
    expect(rows.find(row => row.action === "reply")).toEqual({ action: "reply", total: 0, today: 0 });
    expect(await getRecordedStoryActivity(OTHER_USER_ID, PRIMARY_ACCOUNT_ID, new Date("2026-09-22T12:00:00Z"))).toEqual(
      ["view", "like", "reply", "dm"].map(action => ({ action, total: 0, today: 0 })),
    );
  });
  it("uses the requested account timezone for the calendar boundary", async () => {
    const rows = await getRecordedStoryActivity(PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, new Date("2026-09-22T12:00:00Z"), "UTC");
    expect(rows.find(row => row.action === "like")).toEqual({ action: "like", total: 2, today: 2 });
  });
  it.each(["2026-03-08T12:00:00Z", "2026-11-01T12:00:00Z"])("handles Pacific daylight-saving transition %s", async now => {
    const rows = await getRecordedStoryActivity(PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, new Date(now));
    expect(rows.find(row => row.action === "dm")?.today).toBe(1);
  });
  it("counts UTC quota actions by execution rather than creation time", async () => {
    expect(await getActionCountsSince(PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, new Date("2026-09-22T00:00:00Z"), new Date("2026-09-22T12:00:00Z"))).toEqual({like: 2, reply: 0, dm: 0});
  });
  it("retains historical follow-backs after unfollow and counts only fresh identity-matched coverage", async () => {
    expect(await getRelationshipSummary(PRIMARY_USER_ID, PRIMARY_ACCOUNT_ID, new Date("2026-09-22T12:00:00Z"))).toEqual({
      follows:4, unfollows:1, alreadyAbsent:2, pending:1, overdue:1, followBacks:2, checked:1,
    });
    expect(await getRelationshipSummary(OTHER_USER_ID, PRIMARY_ACCOUNT_ID, new Date("2026-09-22T12:00:00Z"))).toEqual({
      follows:0, unfollows:0, alreadyAbsent:0, pending:0, overdue:0, followBacks:0, checked:0,
    });
  });

});
