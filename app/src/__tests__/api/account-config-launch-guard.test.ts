import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
const valuesMock = vi.hoisted(() => vi.fn().mockResolvedValue(undefined));
vi.mock("@/db", () => ({
  db: { insert: vi.fn(() => ({ values: valuesMock })) },
}));
vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
  getAccountById: vi.fn(),
  getAgentConfig: vi.fn(),
}));

describe("PUT /api/accounts/:id/config launch guard", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { auth } = await import("@/lib/owner-auth");
    const queries = await import("@/lib/queries");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-1" } as never);
    vi.mocked(queries.getUserByOwnerKey).mockResolvedValue({
      id: "user-1",
      plan: "agency",
    } as never);
    vi.mocked(queries.getAccountById).mockResolvedValue({
      id: "account-1",
      userId: "user-1",
    } as never);
  });

  it("rejects direct enablement that would bypass launch readiness", async () => {
    const { PUT } = await import("@/app/api/accounts/[id]/config/route");
    const response = await PUT(
      new Request("http://localhost/api/accounts/account-1/config", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: true }),
      }),
      { params: Promise.resolve({ id: "account-1" }) },
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({
      error: "Use onboarding launch to enable an account",
    });
  });
});
