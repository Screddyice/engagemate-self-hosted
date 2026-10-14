import { beforeEach, expect, it, vi } from "vitest";
import { PgDialect } from "drizzle-orm/pg-core";
import { agentConfigs, agentConfigVersions, mcpAuditEvents } from "@/db/schema";

const state = vi.hoisted(() => ({ before: null as Record<string, unknown> | null, patch: {} as Record<string, unknown>, writes: [] as { table: unknown; values: Record<string, unknown> }[], locks: [] as string[], missingAccount: false, executions: [] as unknown[] }));
vi.mock("@/db", () => ({ db: { transaction: async (fn: (tx: unknown) => Promise<unknown>) => {
  let read = 0;
  const write = (table: unknown, values: Record<string, unknown>) => {
    state.writes.push({ table, values }); state.patch = values;
    return { returning: async () => [{ ...state.before, ...values, id: "config", followEnabledAt: values.followEnabledAt, version: values.version }] };
  };
  const tx = {
    execute: async (query: unknown) => { state.executions.push(query); },
    select: () => ({ from: () => ({ where: () => ({ for: async (lock: string) => {
      state.locks.push(lock); read++;
      return read === 1 ? state.missingAccount ? [] : [{ id: "account" }] : state.before ? [state.before] : [];
    } }) }) }),
    update: (table: unknown) => ({ set: (values: Record<string, unknown>) => ({ where: () => write(table, values) }) }),
    insert: (table: unknown) => ({ values: (values: Record<string, unknown>) => write(table, values) }),
  };
  return fn(tx);
} } }));
import { saveAccountConfig } from "@/lib/account-config-write";
const enabledAt = new Date("2026-09-14T00:00:00Z");
beforeEach(() => { state.before = { id: "config", version: 4, followAfterEngagement: false, followEnabledAt: null }; state.writes = []; state.locks = []; state.missingAccount = false; state.executions = []; });
const configWrite = () => state.writes.find((row) => row.table === agentConfigs)!.values;
const save = (patch: Parameters<typeof saveAccountConfig>[3]) => saveAccountConfig("user", "account", "clerk", patch);

it("stamps first enable in the database and writes version and audit in the transaction", async () => {
  await save({ followAfterEngagement: true, dailyFollowLimit: 3 });
  const values = configWrite();
  expect(new PgDialect().sqlToQuery(values.followEnabledAt as never).sql).toBe("clock_timestamp()");
  expect(values.version).toBe(5); expect(state.locks).toEqual(["update", "update"]);
  expect(state.writes.find((row) => row.table === agentConfigVersions)?.values).toMatchObject({ userId: "user", igAccountId: "account", version: 5, actorType: "web", actorId: "clerk" });
  expect(state.writes.find((row) => row.table === mcpAuditEvents)?.values).toMatchObject({ userId: "user", igAccountId: "account", toolName: "web.config.update", outcome: "updated" });
});
it("preserves enable time while enabled, including cap and automatic-unfollow changes", async () => {
  state.before = { ...state.before, followAfterEngagement: true, followEnabledAt: enabledAt };
  await save({ followAfterEngagement: true, dailyFollowLimit: 0, autoUnfollowEnabled: false });
  expect(configWrite().followEnabledAt).toBe(enabledAt);
  expect(configWrite()).toMatchObject({ dailyFollowLimit: 0, autoUnfollowEnabled: false });
});
it("clears enable time on disable and never stamps unrelated changes", async () => {
  state.before = { ...state.before, followAfterEngagement: true, followEnabledAt: enabledAt };
  await save({ followAfterEngagement: false }); expect(configWrite().followEnabledAt).toBeNull();
  state.writes = []; await save({ dailyLikeLimit: 0 }); expect(configWrite().followEnabledAt).toBe(enabledAt);
});
it("creates config version one without opting into following", async () => {
  state.before = null; await save({ dailyFollowLimit: 3 });
  expect(configWrite()).toMatchObject({ userId: "user", igAccountId: "account", version: 1, followEnabledAt: null });
  expect(configWrite()).not.toHaveProperty("followAfterEngagement");
});
it("refuses an account removed before the transaction without writes", async () => {
  state.missingAccount = true; await expect(save({ followAfterEngagement: true })).rejects.toThrow("Account is unavailable"); expect(state.writes).toEqual([]);
});


it("stamps reel opt-in once and preserves it on unrelated changes", async () => {
  await save({followReelMessageEnabled:true});
  expect(new PgDialect().sqlToQuery(configWrite().followReelMessageEnabledAt as never).sql).toBe("clock_timestamp()");
  state.before = {...state.before, followReelMessageEnabled:true, followReelMessageEnabledAt:enabledAt};
  state.writes = [];
  await save({followReelMessageEnabled:true, followReelMessageTemplate:"Thanks for your support!"});
  expect(configWrite().followReelMessageEnabledAt).toBe(enabledAt);
});
it("cancels only owned pending unattempted messages when disabling", async () => {
  await save({followReelMessageEnabled:false});
  expect(configWrite().followReelMessageEnabledAt).toBeNull();
  const query = new PgDialect().sqlToQuery(state.executions[0] as never);
  expect(query.params).toEqual(["user", "account"]);
  expect(query.sql).toContain("status = 'pending' AND attempted_at IS NULL");
  expect(query.sql).toContain("user_id =");
  expect(query.sql).toContain("ig_account_id =");
});
it("does not opt in on first config creation", async () => {
  state.before = null;
  await save({dailyFollowLimit:3});
  expect(configWrite().followReelMessageEnabledAt).toBeNull();
  expect(configWrite()).not.toHaveProperty("followReelMessageEnabled");
  expect(state.executions).toEqual([]);
});
