import { describe, it, expect, vi, beforeEach } from "vitest";
import { NextRequest } from "next/server";

vi.mock("@/lib/owner-auth", () => ({
  auth: vi.fn(),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
}));

describe("GET /api/extension/session", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    vi.stubEnv("INTERNAL_API_KEY", "test-signing-key");
  });

  it("should return 401 when not authenticated", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const { GET } = await import("@/app/api/extension/session/route");
    const res = await GET(new NextRequest("http://localhost/api/extension/session"));
    expect(res.status).toBe(401);
  });

  it("should return authenticated shape when user has no DB row", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue(null);

    const { GET } = await import("@/app/api/extension/session/route");
    const res = await GET(new NextRequest("http://localhost/api/extension/session"));
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.authenticated).toBe(true);
    expect(data.connected).toBe(false);
    expect(data.account).toBeNull();
    // Must not expose multi-platform org tables or secrets.
    expect(data).not.toHaveProperty("orgId");
    expect(data).not.toHaveProperty("accounts");
  });

  it("should return authenticated shape without org multi-platform data", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: "11111111-2222-4333-8444-555555555555",
      ownerKey: "clerk_1",
    } as never);

    const { GET } = await import("@/app/api/extension/session/route");
    const res = await GET(new NextRequest("http://localhost/api/extension/session"));
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data.authenticated).toBe(true);
    expect(data).toHaveProperty("connected");
    expect(data).toHaveProperty("account");
    expect(data).not.toHaveProperty("orgName");
    expect(data).not.toHaveProperty("hasOrg");
  });

  it("accepts a valid extension ticket without Clerk cookies", async () => {
    const { auth } = await import("@/lib/owner-auth");
    const { getUserByOwnerKey } = await import("@/lib/queries");
    const { mintExtensionTicket } = await import("@/lib/extension-ticket");
    const ticket = mintExtensionTicket(
      "11111111-2222-4333-8444-555555555555",
    );

    const { GET } = await import("@/app/api/extension/session/route");
    const res = await GET(
      new NextRequest("http://localhost/api/extension/session", {
        headers: { Authorization: `Bearer ${ticket}` },
      }),
    );

    expect(res.status).toBe(200);
    expect(auth).not.toHaveBeenCalled();
    expect(getUserByOwnerKey).not.toHaveBeenCalled();
  });

  it("rejects an invalid extension ticket without falling back to Clerk", async () => {
    const { auth } = await import("@/lib/owner-auth");
    const { GET } = await import("@/app/api/extension/session/route");
    const res = await GET(
      new NextRequest("http://localhost/api/extension/session", {
        headers: { Authorization: "Bearer em_xt_invalid.invalid" },
      }),
    );

    expect(res.status).toBe(401);
    expect(auth).not.toHaveBeenCalled();
  });
});
