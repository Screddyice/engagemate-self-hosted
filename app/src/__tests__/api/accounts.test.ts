import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/owner-auth", () => ({
  auth: vi.fn(),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
  getUserAccounts: vi.fn(),
}));

vi.mock("@/lib/account-limits", () => {
  class AccountLimitReached extends Error {
    code = "account_limit_reached" as const;
    constructor(readonly limit: number) {
      super("account limit reached");
    }
  }
  return { AccountLimitReached, connectAccount: vi.fn() };
});

vi.mock("@/db", () => {
  const insertMock = vi.fn();
  return {
    db: { insert: insertMock },
    __insertMock: insertMock,
  };
});

describe("Accounts API", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  // Test 66
  it("GET should return 401 when not authenticated", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const { GET } = await import("@/app/api/accounts/route");
    const res = await GET();
    expect(res.status).toBe(401);
  });

  // Test 67
  it("GET should return 404 when the user row is missing", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue(null);

    const { GET } = await import("@/app/api/accounts/route");
    const res = await GET();
    expect(res.status).toBe(404);
  });

  // Test 68
  it("GET should return accounts array", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey, getUserAccounts } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getUserAccounts).mockResolvedValue([
      { id: "a1", username: "user1", healthStatus: "active" },
    ] as never);

    const { GET } = await import("@/app/api/accounts/route");
    const res = await GET();
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data).toHaveLength(1);
  });

  // Test 69
  it("POST should return 401 when not authenticated", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const { POST } = await import("@/app/api/accounts/route");
    const res = await POST(
      new Request("http://localhost/api/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ platform: "x", username: "test" }),
      })
    );
    expect(res.status).toBe(401);
  });

  it("POST returns the server-enforced account cap", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    const { AccountLimitReached, connectAccount } = await import(
      "@/lib/account-limits"
    );
    vi.mocked(connectAccount).mockRejectedValue(new AccountLimitReached(3));

    const { POST } = await import("@/app/api/accounts/route");
    const res = await POST(
      new Request("http://localhost/api/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "new.account" }),
      }),
    );

    expect(res.status).toBe(409);
    await expect(res.json()).resolves.toEqual({
      error: "account_limit_reached",
      limit: 3,
    });
  });
});
