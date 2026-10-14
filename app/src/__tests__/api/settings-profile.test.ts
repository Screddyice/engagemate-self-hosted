import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/owner-auth", () => ({
  auth: vi.fn(),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
}));

describe("GET /api/settings/profile", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  // Test 52
  it("should return 401 when not authenticated", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const { GET } = await import("@/app/api/settings/profile/route");
    const res = await GET();
    expect(res.status).toBe(401);
  });

  // Test 53
  it("should return 404 when the user row is missing", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue(null);

    const { GET } = await import("@/app/api/settings/profile/route");
    const res = await GET();
    expect(res.status).toBe(404);
  });

  // Test 54
  it("should filter out stripe identifiers", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: "user-1",
      name: "Test User",
      email: "test@example.com",
      plan: "starter",
      stripeCustomerId: "cus_secret_123",
      stripeSubscriptionId: "sub_secret_123",
    } as never);

    const { GET } = await import("@/app/api/settings/profile/route");
    const res = await GET();
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.name).toBe("Test User");
    expect(data.plan).toBe("starter");
    expect(data.stripeCustomerId).toBeUndefined();
    expect(data.stripeSubscriptionId).toBeUndefined();
  });
});
