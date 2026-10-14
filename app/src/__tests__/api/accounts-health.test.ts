import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/owner-auth", () => ({
  auth: vi.fn(),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
  getAccountById: vi.fn(),
}));

vi.mock("@/db", () => {
  const selectMock = vi.fn();
  const insertMock = vi.fn();
  const updateMock = vi.fn();
  return {
    db: { select: selectMock, insert: insertMock, update: updateMock },
    __selectMock: selectMock,
    __insertMock: insertMock,
    __updateMock: updateMock,
  };
});

describe("POST /api/accounts/health", () => {
  let POST: (req: Request) => Promise<Response>;

  beforeEach(async () => {
    vi.resetModules();
    const mod = await import("@/app/api/accounts/health/route");
    POST = mod.POST;
  });

  function makeRequest(body: Record<string, unknown>) {
    return new Request("http://localhost:3000/api/accounts/health", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  // Test 41
  it("should return 401 when not authenticated", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const res = await POST(makeRequest({ igAccountId: "11111111-1111-4111-8111-111111111111", isHealthy: true }));
    expect(res.status).toBe(401);
  });

  // Test 42
  it("should return 404 when the user row is missing", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue(null);

    const res = await POST(makeRequest({ igAccountId: "11111111-1111-4111-8111-111111111111", isHealthy: true }));
    expect(res.status).toBe(404);
  });

  // Test 43
  it("should return 400 when missing igAccountId", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);

    const res = await POST(makeRequest({ isHealthy: true }));
    expect(res.status).toBe(400);
  });

  // Test 44
  it("should return 400 when isHealthy is not boolean", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);

    const res = await POST(makeRequest({ igAccountId: "11111111-1111-4111-8111-111111111111", isHealthy: "yes" }));
    expect(res.status).toBe(400);
  });

  // Test 45
  it("should return 404 when account not found", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey, getAccountById } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getAccountById).mockResolvedValue(null);

    const res = await POST(makeRequest({ igAccountId: "11111111-1111-4111-8111-111111111111", isHealthy: true }));
    expect(res.status).toBe(404);
  });

  it("does not append duplicate login events while the account is already paused", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey, getAccountById } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getAccountById).mockResolvedValue({
      id: "account-1",
      username: "demo_user",
      healthStatus: "paused",
      pauseReason: "login_required",
    } as never);

    const db = await import("@/db") as unknown as {
      __updateMock: ReturnType<typeof vi.fn>;
      __insertMock: ReturnType<typeof vi.fn>;
    };
    const response = await POST(makeRequest({ igAccountId: "account-1", isHealthy: false }));

    expect(response.status).toBe(200);
    expect(db.__updateMock).not.toHaveBeenCalled();
    expect(db.__insertMock).not.toHaveBeenCalled();
    expect(await response.json()).toMatchObject({ accountId: "account-1", healthStatus: "paused" });
  });
});
