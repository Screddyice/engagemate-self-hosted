import { DEFAULT_FOLLOW_REEL_MESSAGE } from "@/lib/follow-reel-settings";
import { beforeEach, describe, expect, it, vi } from "vitest";

const write = vi.hoisted(() => vi.fn());
vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/account-config-write", () => ({ saveAccountConfig: (...args: unknown[]) => { write(args[3]); return { version: 2 }; } }));
vi.mock("@/lib/queries", () => ({ getUserByOwnerKey: vi.fn(), getAccountById: vi.fn(), getAgentConfig: vi.fn() }));
import { GET, PUT } from "@/app/api/accounts/[id]/config/route";
import { auth } from "@/lib/owner-auth";
import { getUserByOwnerKey, getAccountById, getAgentConfig } from "@/lib/queries";
const context = { params: Promise.resolve({ id: "account-1" }) };
const request = (body?: unknown) => new Request("http://localhost/api/accounts/account-1/config", body === undefined ? {} : {
  method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

describe("saved account allocations", () => {
  it("saves separate Story reply and reel targets", async () => {
    expect((await PUT(request({dailyReplyLimit:3,dailyReelMessageLimit:3}),context)).status).toBe(200);
    expect(write).toHaveBeenCalledWith({dailyReplyLimit:3,dailyReelMessageLimit:3});
  });
  it.each([-1, 221, 1.5, "3", true])("rejects invalid reel target %s", async value => {
    expect((await PUT(request({dailyReelMessageLimit:value}),context)).status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });
  it("keeps warm-up exemptions operator-only", async () => {
    expect((await PUT(request({warmupExempt:true}),context)).status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });
  it("rejects an invalid schedule timezone", async () => {
    expect((await PUT(request({timezone:"Invalid/Zone"}),context)).status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });
  it("normalizes Pacific shorthand to the worker's IANA timezone", async () => {
    expect((await PUT(request({timezone:"PST"}),context)).status).toBe(200);
    expect(write).toHaveBeenCalledWith({timezone:"America/Los_Angeles"});
  });
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-1" } as never);
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1", plan: "starter" } as never);
    vi.mocked(getAccountById).mockResolvedValue({ id: "account-1" } as never);
    vi.mocked(getAgentConfig).mockResolvedValue({
      id: "config-1", dailyViewLimit: 0, dailyLikeLimit: 0,
      dailyReplyLimit: 0, dailyDmLimit: 0, followAfterEngagement: false, autoUnfollowEnabled: true, dailyFollowLimit: 3, followEnabledAt: null, persona: "private voice",
    } as never);
  });

  it("returns the four saved zeros without exposing the rest of the config", async () => {
    const response = await GET(request(), context);
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("private, no-store");
    expect(await response.json()).toEqual({ planDailyActions: 220, planDailyViews: 600, igAccountId: "account-1", dailyViewLimit: 0, dailyLikeLimit: 0, dailyReplyLimit: 0, dailyDmLimit: 0, dailyReelMessageLimit:null, warmupExempt:false, followAfterEngagement: false, autoUnfollowEnabled: true, dailyFollowLimit: 3, followEnabledAt: null, followReelMessageEnabled:false, followReelMessageEnabledAt:null, followReelMessageTemplate:DEFAULT_FOLLOW_REEL_MESSAGE });
    expect(getAccountById).toHaveBeenCalledWith("user-1", "account-1");
    expect(getAgentConfig).toHaveBeenCalledWith("user-1", "account-1");
    expect(write).not.toHaveBeenCalled();
  });

  it("rejects unauthenticated allocation reads", async () => {
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);
    expect((await GET(request(), context)).status).toBe(401);
    expect(getAgentConfig).not.toHaveBeenCalled();
  });

  it("does not read a foreign account's configuration", async () => {
    vi.mocked(getAccountById).mockResolvedValue(null);
    expect((await GET(request(), context)).status).toBe(404);
    expect(getAgentConfig).not.toHaveBeenCalled();
  });

  it("reports a missing config without fabricating default allocations", async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(null);
    const response = await GET(request(), context);
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({ error: "Saved allocations are unavailable" });
    expect(write).not.toHaveBeenCalled();
  });

  it.each([0, 100])("persists a view limit of %s using the starter view ceiling", async (dailyViewLimit) => {
    const response = await PUT(request({ dailyViewLimit }), context);
    expect(response.status).toBe(200);
    expect(write).toHaveBeenCalledWith({ dailyViewLimit });
  });

  it.each([-1, 601, 1.5, "30", null])("rejects invalid view allocation %s without writing", async (dailyViewLimit) => {
    expect((await PUT(request({ dailyViewLimit }), context)).status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });

  it("does not use the view ceiling for likes", async () => {
    expect((await PUT(request({ dailyLikeLimit: 221 }), context)).status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });

  it.each([null, [], true, "settings", 42])("rejects non-object settings %s without writing", async (body) => {
    const result = await PUT(request(body), context);
    expect(result.status).toBe(400);
    expect(await result.json()).toEqual({ error: "Request body must be a JSON object" });
    expect(write).not.toHaveBeenCalled();
  });

  it("rejects malformed JSON without writing", async () => {
    const result = await PUT(new Request("http://localhost/api/accounts/account-1/config", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: "{",
    }), context);
    expect(result.status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });
});

describe("follow configuration validation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-1" } as never);
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1", plan: "starter" } as never);
    vi.mocked(getAccountById).mockResolvedValue({ id: "account-1" } as never);
  });
  it.each([0, 3, 20])("accepts a separate nonbillable follow cap %s", async (dailyFollowLimit) => {
    expect((await PUT(request({ dailyFollowLimit, followAfterEngagement: true, autoUnfollowEnabled: false }), context)).status).toBe(200);
    expect(write).toHaveBeenCalledWith({ dailyFollowLimit, followAfterEngagement: true, autoUnfollowEnabled: false });
  });
  it.each([-1, 21, 1.5, "3", true, null])("rejects malformed follow cap %s", async (dailyFollowLimit) => {
    expect((await PUT(request({ dailyFollowLimit }), context)).status).toBe(400); expect(write).not.toHaveBeenCalled();
  });
  it.each(["followAfterEngagement", "autoUnfollowEnabled", "followReelMessageEnabled"])("requires explicit booleans for %s", async (field) => {
    for (const value of [0, 1, "true", "false", null, []]) expect((await PUT(request({ [field]: value }), context)).status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });
  it("rejects caller-supplied enable timestamps and versions", async () => {
    for (const patch of [{ followEnabledAt: "2020-01-01" }, { version: 99 }, {followReelMessageEnabledAt:"2020-01-01"}]) expect((await PUT(request(patch), context)).status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });
  it("rejects a foreign account before configuration writes", async () => {
    vi.mocked(getAccountById).mockResolvedValue(null);
    expect((await PUT(request({ followAfterEngagement: true }), context)).status).toBe(404); expect(write).not.toHaveBeenCalled();
  });
  it.each(["", " ", "a".repeat(501), "See https://evil.example/reel", "See www.example.com", "See example.com", null, 12, "hello\u0000"])("rejects invalid reel template %s", async (template) => {
    expect((await PUT(request({followReelMessageTemplate:template}), context)).status).toBe(400);
    expect(write).not.toHaveBeenCalled();
  });
  it("saves opt-in and a trimmed custom message without changing follows", async () => {
    expect((await PUT(request({followReelMessageEnabled:true, followReelMessageTemplate:" Thanks for your support! "}), context)).status).toBe(200);
    expect(write).toHaveBeenCalledWith({followReelMessageEnabled:true, followReelMessageTemplate:"Thanks for your support!"});
  });
  it("requires sign-in for reel settings", async () => {
    vi.mocked(auth).mockResolvedValue({userId:null} as never);
    expect((await PUT(request({followReelMessageEnabled:true}), context)).status).toBe(401);
    expect(write).not.toHaveBeenCalled();
  });

});
