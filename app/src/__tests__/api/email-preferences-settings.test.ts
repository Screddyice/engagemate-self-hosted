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
import { users } from "@/db/schema";

const mocks = vi.hoisted(() => ({
  auth: vi.fn(),
  getUserByOwnerKey: vi.fn(),
  getEmailPreferences: vi.fn(),
  updateEmailPreferences: vi.fn(),
}));

vi.mock("@/lib/owner-auth", () => ({ auth: mocks.auth }));
vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: mocks.getUserByOwnerKey,
  getEmailPreferences: mocks.getEmailPreferences,
  updateEmailPreferences: mocks.updateEmailPreferences,
}));
vi.unmock("@/db");

const LOCAL_USER = {
  id: "11111111-1111-4111-8111-111111111111",
  ownerKey: "clerk_primary",
  email: "primary@example.test",
};

function patch(body: unknown) {
  return new Request("http://localhost/api/settings/email-preferences", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("/api/settings/email-preferences", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.auth.mockResolvedValue({ userId: "clerk_primary" });
    mocks.getUserByOwnerKey.mockResolvedValue(LOCAL_USER);
    mocks.getEmailPreferences.mockResolvedValue({
      activityEmails: true,
      onboardingEmails: true,
    });
    mocks.updateEmailPreferences.mockImplementation(
      async (_userId: string, preferences: unknown) => preferences,
    );
  });

  it("requires a signed-in user", async () => {
    mocks.auth.mockResolvedValue({ userId: null });
    const { GET, PATCH } = await import("@/app/api/settings/email-preferences/route");
    expect((await GET()).status).toBe(401);
    expect((await PATCH(patch({}))).status).toBe(401);
  });

  it("reads and saves only the signed-in user's switches", async () => {
    const { GET, PATCH } = await import("@/app/api/settings/email-preferences/route");
    expect(await (await GET()).json()).toEqual({
      activityEmails: true,
      onboardingEmails: true,
    });
    const response = await PATCH(
      patch({ activityEmails: false, onboardingEmails: true }),
    );
    expect(response.status).toBe(200);
    expect(mocks.updateEmailPreferences).toHaveBeenCalledWith(LOCAL_USER.id, {
      activityEmails: false,
      onboardingEmails: true,
    });
  });

  it.each([
    { label: "a missing switch", body: { activityEmails: false } },
    { label: "a string", body: { activityEmails: "false", onboardingEmails: true } },
  ])("returns 400 for $label", async ({ body }) => {
    const { PATCH } = await import("@/app/api/settings/email-preferences/route");
    expect((await PATCH(patch(body))).status).toBe(400);
    expect(mocks.updateEmailPreferences).not.toHaveBeenCalled();
  });
});

const testDirectory = dirname(fileURLToPath(import.meta.url));
const appDirectory = resolve(testDirectory, "../../..");
const database = `engagemate_email_preferences_${process.pid}`;
const databaseUrl = `postgresql://localhost/${database}`;

function command(program: string, args: string[], env: NodeJS.ProcessEnv = process.env) {
  return spawnSync(program, args, { cwd: appDirectory, encoding: "utf8", env });
}

const postgresAvailable =
  command("pg_isready", []).status === 0 &&
  command("createdb", ["--version"]).status === 0;
const postgresDescribe = postgresAvailable ? describe.sequential : describe.skip;

let previousDatabaseUrl: string | undefined;
let databaseCreated = false;
let databaseModule: typeof import("@/db");
let queries: typeof import("@/lib/queries");

postgresDescribe("email preference persistence (migration 0024)", () => {
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
    queries = await vi.importActual<typeof import("@/lib/queries")>("@/lib/queries");
    await databaseModule.db.insert(users).values(LOCAL_USER);
  }, 30_000);

  afterAll(async () => {
    await databaseModule?.db.$client.end({ timeout: 5 });
    if (previousDatabaseUrl === undefined) delete process.env.DATABASE_URL;
    else process.env.DATABASE_URL = previousDatabaseUrl;
    vi.resetModules();
    if (databaseCreated) command("dropdb", ["--if-exists", database]);
  });

  it("defaults every email on, then keeps each unsubscribe separate", async () => {
    expect(await queries.getEmailPreferences(LOCAL_USER.id)).toEqual({
      activityEmails: true,
      onboardingEmails: true,
    });

    expect(await queries.unsubscribeUserFromCategory(LOCAL_USER.id, "activity")).toBe(true);
    expect(await queries.getEmailPreferences(LOCAL_USER.id)).toEqual({
      activityEmails: false,
      onboardingEmails: true,
    });

    expect(await queries.unsubscribeUserFromCategory(LOCAL_USER.id, "weekly_report")).toBe(true);
    const weekly = await queries.getOrCreateWeeklyReportPreference(LOCAL_USER.id);
    expect(weekly.enabled).toBe(false);

    await queries.updateEmailPreferences(LOCAL_USER.id, {
      activityEmails: true,
      onboardingEmails: false,
    });
    expect(await queries.getEmailPreferences(LOCAL_USER.id)).toEqual({
      activityEmails: true,
      onboardingEmails: false,
    });

    expect(
      await queries.unsubscribeUserFromCategory(
        "99999999-9999-4999-8999-999999999999",
        "activity",
      ),
    ).toBe(false);
  });
});
