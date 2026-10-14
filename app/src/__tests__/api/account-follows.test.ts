import { beforeEach, describe, expect, it, vi } from "vitest";
import { PgDialect } from "drizzle-orm/pg-core";
const state = vi.hoisted(() => ({ where: null as unknown, changes: null as unknown, updated: true, audit: vi.fn(), read: vi.fn(), recordWhere: null as unknown, transaction: vi.fn() }));
vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/queries", () => ({ getUserByOwnerKey: vi.fn(), getAccountById: vi.fn() }));
vi.mock("@/db", () => ({ db: {
  select: (...args: unknown[]) => { state.read(...args); return { from: () => ({ where: async () => [{ pendingUnfollows: 0, uncertain: 0 }], innerJoin: () => ({ where: (condition: unknown) => { state.recordWhere = condition; return ({ orderBy: () => ({ limit: async () => [] }) }); } }) }) }; },
  transaction: async (callback: (tx: unknown) => Promise<unknown>) => {
    state.transaction(); return callback({
      update: () => ({ set: (values: unknown) => { state.changes = values; return { where: (condition: unknown) => { state.where = condition; return { returning: async () => state.updated ? [{ id: "11111111-1111-4111-8111-111111111111" }] : [] }; } }; } }),
      insert: () => ({ values: state.audit }),
    });
  },
} }));
import { GET, POST } from "@/app/api/accounts/[id]/follows/route";
import { auth } from "@/lib/owner-auth";
import { getAccountById, getUserByOwnerKey } from "@/lib/queries";
const context = { params: Promise.resolve({ id: "account" }) };
const id = "11111111-1111-4111-8111-111111111111";
const request = (body: unknown = { id }) => new Request("http://localhost/api/accounts/account/follows", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
beforeEach(() => { vi.clearAllMocks(); state.where = null; state.changes = null; state.updated = true;
  vi.mocked(auth).mockResolvedValue({ userId: "clerk" } as never);
  vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user" } as never);
  vi.mocked(getAccountById).mockResolvedValue({ id: "account" } as never);
});
describe("owned follow history", () => {
  it("reads scoped status without caching", async () => {
    const response = await GET(request(), context); expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("private, no-store");
    expect(await response.json()).toEqual({ igAccountId: "account", pendingUnfollows: 0, uncertain: 0, records: [] });
    expect(getAccountById).toHaveBeenCalledWith("user", "account");
  });
  it("counts uncertain reel deliveries in the review total", async () => {
    await GET(request(), context);
    const selection = state.read.mock.calls[0][0];
    const query = new PgDialect().sqlToQuery(selection.uncertain);
    expect(query.sql).toContain("follow_reel_messages");
    expect(query.sql).toContain("status = 'uncertain'");
    expect(query.sql).toContain("relationship_follow_id");
    expect(query.sql).toContain("user_id");
    expect(query.sql).toContain("ig_account_id");
  });
  it("correlates message receipts by relationship, owner and account without exposing message text", async () => {
    await GET(request(), context);
    const selection = state.read.mock.calls[1][0];
    for (const field of ["messageStatus", "messageSentAt", "reelUrl"]) {
      const query = new PgDialect().sqlToQuery(selection[field]);
      expect(query.sql).toContain('frm.relationship_follow_id = "relationship_follows"."id"');
      expect(query.sql).toContain('frm.user_id = "relationship_follows"."user_id"');
      expect(query.sql).toContain('frm.ig_account_id = "relationship_follows"."ig_account_id"');
    }
    expect(selection).not.toHaveProperty("body");
    const where = new PgDialect().sqlToQuery(state.recordWhere as never);
    expect(where.params).toEqual(["user", "account"]);
    expect(where.sql).toContain("or (");
    expect(where.sql).toContain("is not null");

  });
  it("cancels only a confirmed owned follow with no lease and preserves its history", async () => {
    expect((await POST(request(), context)).status).toBe(200);
    const query = new PgDialect().sqlToQuery(state.where as never);
    expect(query.params).toEqual([id, "user", "account", "following"]);
    for (const column of ["claimed_by", "lease_token", "lease_expires_at"]) expect(query.sql).toContain(`"relationship_follows"."${column}" is null`);
    expect(query.sql).toContain('"relationship_follows"."confirmed_follow_at" is not null');
    expect(state.changes).toMatchObject({ status: "cancelled", nextAttemptAt: null, lastError: "cancelled_by_user" });
    expect(state.changes).not.toHaveProperty("confirmedFollowAt"); expect(state.changes).not.toHaveProperty("unfollowDueAt");
    expect(state.audit).toHaveBeenCalledWith(expect.objectContaining({ userId: "user", igAccountId: "account", afterState: { id, status: "cancelled" } }));
  });
  it("refuses missing, uncertain, foreign, already-cancelled, or claimed records without a success audit", async () => {
    state.updated = false; expect((await POST(request(), context)).status).toBe(409); expect(state.audit).not.toHaveBeenCalled();
  });
  it.each([null, [], {}, { id: "bad" }, { id, status: "unfollowed" }])("rejects malformed cancellation %s", async (body) => {
    expect((await POST(request(body), context)).status).toBe(400); expect(state.transaction).not.toHaveBeenCalled();
  });
  it("rejects unauthenticated reads and cancellations", async () => {
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);
    expect((await GET(request(), context)).status).toBe(401); expect((await POST(request(), context)).status).toBe(401);
    expect(state.read).not.toHaveBeenCalled(); expect(state.transaction).not.toHaveBeenCalled();
  });
  it("rejects a foreign account before reading or updating history", async () => {
    vi.mocked(getAccountById).mockResolvedValue(null);
    expect((await GET(request(), context)).status).toBe(404); expect((await POST(request(), context)).status).toBe(404);
    expect(state.read).not.toHaveBeenCalled(); expect(state.transaction).not.toHaveBeenCalled();
  });
});
