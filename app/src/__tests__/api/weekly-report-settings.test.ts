import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  afterAll,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import {
  agentConfigs,
  igAccounts,
  users,
  weeklyReportPreferences,
} from "@/db/schema";

const mocks = vi.hoisted(() => ({
  auth: vi.fn(),
  getUserByOwnerKey: vi.fn(),
  getOrCreateWeeklyReportPreference: vi.fn(),
  updateWeeklyReportPreference: vi.fn(),
}));

vi.mock("@/lib/owner-auth", () => ({ auth: mocks.auth }));
vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: mocks.getUserByOwnerKey,
  getOrCreateWeeklyReportPreference: mocks.getOrCreateWeeklyReportPreference,
  updateWeeklyReportPreference: mocks.updateWeeklyReportPreference,
}));
vi.unmock("@/db");

const LOCAL_USER = {
  id: "11111111-1111-4111-8111-111111111111",
  ownerKey: "clerk_primary",
  email: "primary@example.test",
};
const OTHER_USER_ID = "22222222-2222-4222-8222-222222222222";
const DEFAULT_PREFERENCE = {
  enabled: true,
  timezone: "Asia/Makassar",
  sendHour: 9,
};

function patchRequest(body: unknown, serialize = true) {
  return new Request("http://localhost/api/settings/weekly-report", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: serialize ? JSON.stringify(body) : String(body),
  });
}

describe("/api/settings/weekly-report", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.auth.mockResolvedValue({ userId: "clerk_primary" });
    mocks.getUserByOwnerKey.mockResolvedValue(LOCAL_USER);
    mocks.getOrCreateWeeklyReportPreference.mockResolvedValue(
      DEFAULT_PREFERENCE,
    );
    mocks.updateWeeklyReportPreference.mockResolvedValue(DEFAULT_PREFERENCE);
  });

  it.each(["GET", "PATCH"] as const)(
    "returns 401 for an unauthenticated %s",
    async (method) => {
      mocks.auth.mockResolvedValue({ userId: null });
      const route = await import("@/app/api/settings/weekly-report/route");
      const response =
        method === "GET"
          ? await route.GET()
          : await route.PATCH(patchRequest(DEFAULT_PREFERENCE));

      expect(response.status).toBe(401);
      expect(response.headers.get("content-type")).toContain(
        "application/json",
      );
      await expect(response.json()).resolves.toEqual({
        error: "Unauthorized",
      });
      expect(mocks.getUserByOwnerKey).not.toHaveBeenCalled();
      expect(mocks.getOrCreateWeeklyReportPreference).not.toHaveBeenCalled();
      expect(mocks.updateWeeklyReportPreference).not.toHaveBeenCalled();
    },
  );

  it.each(["GET", "PATCH"] as const)(
    "returns 404 when the local user is missing for %s",
    async (method) => {
      mocks.getUserByOwnerKey.mockResolvedValue(null);
      const route = await import("@/app/api/settings/weekly-report/route");
      const response =
        method === "GET"
          ? await route.GET()
          : await route.PATCH(patchRequest(DEFAULT_PREFERENCE));

      expect(response.status).toBe(404);
    },
  );

  it("returns only the safe preference fields", async () => {
    mocks.getOrCreateWeeklyReportPreference.mockResolvedValue({
      ...DEFAULT_PREFERENCE,
      userId: LOCAL_USER.id,
      recipientEmail: LOCAL_USER.email,
      updatedAt: new Date(),
    });

    const { GET } = await import("@/app/api/settings/weekly-report/route");
    const response = await GET();

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual(DEFAULT_PREFERENCE);
    expect(mocks.getOrCreateWeeklyReportPreference).toHaveBeenCalledWith(
      LOCAL_USER.id,
    );
  });

  it("updates only the authenticated local user's preference", async () => {
    const requested = {
      enabled: false,
      timezone: "America/Los_Angeles",
      sendHour: 18,
    };
    mocks.updateWeeklyReportPreference.mockResolvedValue(requested);

    const { PATCH } = await import("@/app/api/settings/weekly-report/route");
    const response = await PATCH(
      patchRequest({
        ...requested,
        userId: OTHER_USER_ID,
        recipientEmail: "attacker@example.test",
        ignored: "field",
      }),
    );

    expect(response.status).toBe(200);
    expect(mocks.updateWeeklyReportPreference).toHaveBeenCalledWith(
      LOCAL_USER.id,
      requested,
    );
    await expect(response.json()).resolves.toEqual(requested);
  });

  it("returns 400 for malformed JSON", async () => {
    const { PATCH } = await import("@/app/api/settings/weekly-report/route");
    const response = await PATCH(patchRequest("{broken", false));

    expect(response.status).toBe(400);
    expect(mocks.updateWeeklyReportPreference).not.toHaveBeenCalled();
  });

  it.each([
    { body: { timezone: "UTC", sendHour: 9 }, label: "missing enabled" },
    {
      body: { ...DEFAULT_PREFERENCE, enabled: "true" },
      label: "non-boolean enabled",
    },
    {
      body: { ...DEFAULT_PREFERENCE, timezone: "Mars/Olympus_Mons" },
      label: "invalid timezone",
    },
    {
      body: { ...DEFAULT_PREFERENCE, timezone: "A".repeat(65) },
      label: "overlong timezone",
    },
    {
      body: { ...DEFAULT_PREFERENCE, sendHour: 9.5 },
      label: "non-integer send hour",
    },
    {
      body: { ...DEFAULT_PREFERENCE, sendHour: -1 },
      label: "negative send hour",
    },
    {
      body: { ...DEFAULT_PREFERENCE, sendHour: 24 },
      label: "send hour above 23",
    },
  ])("returns 400 for $label", async ({ body }) => {
    const { PATCH } = await import("@/app/api/settings/weekly-report/route");
    const response = await PATCH(patchRequest(body));

    expect(response.status).toBe(400);
    expect(mocks.updateWeeklyReportPreference).not.toHaveBeenCalled();
  });
});

const testDirectory = dirname(fileURLToPath(import.meta.url));
const appDirectory = resolve(testDirectory, "../../..");
const database = `engagemate_weekly_settings_${process.pid}`;
const databaseUrl = `postgresql://localhost/${database}`;
const NO_ACCOUNT_USER_ID = "33333333-3333-4333-8333-333333333333";
const CONCURRENT_USER_ID = "44444444-4444-4444-8444-444444444444";
const REPAIR_USER_ID = "55555555-5555-4555-8555-555555555555";
const TIE_BREAK_USER_ID = "66666666-6666-4666-8666-666666666666";

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
let databaseModule: typeof import("@/db");
let getOrCreatePreference: typeof import("@/lib/queries")["getOrCreateWeeklyReportPreference"];
let updatePreference: typeof import("@/lib/queries")["updateWeeklyReportPreference"];

postgresDescribe("weekly report preference persistence", () => {
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

    databaseModule = await vi.importActual<typeof import("@/db")>("@/db");
    const queries = await vi.importActual<typeof import("@/lib/queries")>(
      "@/lib/queries",
    );
    getOrCreatePreference = queries.getOrCreateWeeklyReportPreference;
    updatePreference = queries.updateWeeklyReportPreference;
    closeDatabaseClient = () => databaseModule.db.$client.end({ timeout: 5 });

    await databaseModule.db.insert(users).values([
      LOCAL_USER,
      {
        id: OTHER_USER_ID,
        ownerKey: "clerk_other",
        email: "other@example.test",
      },
      {
        id: NO_ACCOUNT_USER_ID,
        ownerKey: "clerk_no_account",
        email: "no-account@example.test",
      },
      {
        id: CONCURRENT_USER_ID,
        ownerKey: "clerk_concurrent",
        email: "concurrent@example.test",
      },
      {
        id: REPAIR_USER_ID,
        ownerKey: "clerk_repair",
        email: "repair@example.test",
      },
      {
        id: TIE_BREAK_USER_ID,
        ownerKey: "clerk_tie_break",
        email: "tie-break@example.test",
      },
    ]);

    const earlierAccountId = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
    const laterAccountId = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
    const otherAccountId = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
    const concurrentAccountId = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";
    const lowerTieAccountId = "00000000-0000-4000-8000-000000000001";
    const higherTieAccountId = "00000000-0000-4000-8000-000000000002";
    await databaseModule.db.insert(igAccounts).values([
      {
        id: earlierAccountId,
        userId: LOCAL_USER.id,
        username: "primary_first",
        createdAt: new Date("2026-01-02T00:00:00Z"),
      },
      {
        id: laterAccountId,
        userId: LOCAL_USER.id,
        username: "primary_second",
        createdAt: new Date("2026-02-02T00:00:00Z"),
      },
      {
        id: otherAccountId,
        userId: OTHER_USER_ID,
        username: "other_first",
        createdAt: new Date("2026-01-01T00:00:00Z"),
      },
      {
        id: concurrentAccountId,
        userId: CONCURRENT_USER_ID,
        username: "concurrent_first",
        createdAt: new Date("2026-01-03T00:00:00Z"),
      },
      {
        id: lowerTieAccountId,
        userId: TIE_BREAK_USER_ID,
        username: "tie_lower_id",
        createdAt: new Date("2026-01-04T00:00:00Z"),
      },
      {
        id: higherTieAccountId,
        userId: TIE_BREAK_USER_ID,
        username: "tie_higher_id",
        createdAt: new Date("2026-01-04T00:00:00Z"),
      },
    ]);
    await databaseModule.db.insert(agentConfigs).values([
      {
        userId: LOCAL_USER.id,
        igAccountId: earlierAccountId,
        timezone: "Asia/Makassar",
        createdAt: new Date("2026-03-01T00:00:00Z"),
      },
      {
        userId: LOCAL_USER.id,
        igAccountId: laterAccountId,
        timezone: "America/New_York",
        createdAt: new Date("2026-01-01T00:00:00Z"),
      },
      {
        userId: OTHER_USER_ID,
        igAccountId: otherAccountId,
        timezone: "Pacific/Auckland",
      },
      {
        userId: CONCURRENT_USER_ID,
        igAccountId: concurrentAccountId,
        timezone: "Europe/London",
      },
      {
        userId: TIE_BREAK_USER_ID,
        igAccountId: lowerTieAccountId,
        timezone: "Europe/Paris",
        createdAt: new Date("2026-03-02T00:00:00Z"),
      },
      {
        userId: TIE_BREAK_USER_ID,
        igAccountId: higherTieAccountId,
        timezone: "Europe/Berlin",
        createdAt: new Date("2026-01-02T00:00:00Z"),
      },
    ]);
  }, 30_000);

  afterAll(async () => {
    if (closeDatabaseClient) await closeDatabaseClient();
    if (previousDatabaseUrl === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = previousDatabaseUrl;
    vi.resetModules();

    if (databaseCreated) {
      const dropped = command("dropdb", ["--if-exists", database]);
      expect(dropped.status, dropped.stderr).toBe(0);
    }
  }, 30_000);

  it("creates defaults from only the owner's earliest configured account", async () => {
    await expect(getOrCreatePreference(LOCAL_USER.id)).resolves.toEqual({
      enabled: true,
      timezone: "Asia/Makassar",
      sendHour: 9,
    });
  });

  it("uses account ID as the stable tie-break instead of config creation order", async () => {
    await expect(getOrCreatePreference(TIE_BREAK_USER_ID)).resolves.toEqual({
      enabled: true,
      timezone: "Europe/Paris",
      sendHour: 9,
    });
  });

  it("falls back to UTC when the owner has no configured account", async () => {
    await expect(getOrCreatePreference(NO_ACCOUNT_USER_ID)).resolves.toEqual({
      enabled: true,
      timezone: "UTC",
      sendHour: 9,
    });
  });

  it("returns one stable row during concurrent default creation", async () => {
    const preferences = await Promise.all(
      Array.from({ length: 4 }, () =>
        getOrCreatePreference(CONCURRENT_USER_ID),
      ),
    );

    expect(preferences).toEqual(
      Array.from({ length: 4 }, () => ({
        enabled: true,
        timezone: "Europe/London",
        sendHour: 9,
      })),
    );
    const rows = await databaseModule.db
      .select()
      .from(weeklyReportPreferences)
      .where(
        (await import("drizzle-orm")).eq(
          weeklyReportPreferences.userId,
          CONCURRENT_USER_ID,
        ),
      );
    expect(rows).toHaveLength(1);
  });

  it("repairs one invalid existing timezone safely under concurrent reads", async () => {
    const staleUpdatedAt = new Date("2025-01-01T00:00:00Z");
    await databaseModule.db.insert(weeklyReportPreferences).values({
      userId: REPAIR_USER_ID,
      enabled: false,
      timezone: "Mars/Olympus_Mons",
      sendHour: 22,
      updatedAt: staleUpdatedAt,
    });

    const preferences = await Promise.all(
      Array.from({ length: 4 }, () => getOrCreatePreference(REPAIR_USER_ID)),
    );

    expect(preferences).toEqual(
      Array.from({ length: 4 }, () => ({
        enabled: false,
        timezone: "UTC",
        sendHour: 22,
      })),
    );
    const rows = await databaseModule.db
      .select()
      .from(weeklyReportPreferences)
      .where(
        (await import("drizzle-orm")).eq(
          weeklyReportPreferences.userId,
          REPAIR_USER_ID,
        ),
      );
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      enabled: false,
      timezone: "UTC",
      sendHour: 22,
    });
    expect(rows[0].updatedAt.getTime()).toBeGreaterThan(
      staleUpdatedAt.getTime(),
    );
  });

  it("updates only the requested owner and refreshes updated_at", async () => {
    const otherBefore = await getOrCreatePreference(OTHER_USER_ID);
    const [before] = await databaseModule.db
      .select({ updatedAt: weeklyReportPreferences.updatedAt })
      .from(weeklyReportPreferences)
      .where(
        (await import("drizzle-orm")).eq(
          weeklyReportPreferences.userId,
          LOCAL_USER.id,
        ),
      );

    await new Promise((resolve) => setTimeout(resolve, 5));
    await expect(
      updatePreference(LOCAL_USER.id, {
        enabled: false,
        timezone: "America/Los_Angeles",
        sendHour: 18,
      }),
    ).resolves.toEqual({
      enabled: false,
      timezone: "America/Los_Angeles",
      sendHour: 18,
    });

    const [after] = await databaseModule.db
      .select({ updatedAt: weeklyReportPreferences.updatedAt })
      .from(weeklyReportPreferences)
      .where(
        (await import("drizzle-orm")).eq(
          weeklyReportPreferences.userId,
          LOCAL_USER.id,
        ),
      );
    expect(after.updatedAt.getTime()).toBeGreaterThan(before.updatedAt.getTime());
    await expect(getOrCreatePreference(OTHER_USER_ID)).resolves.toEqual(
      otherBefore,
    );
  });
});
