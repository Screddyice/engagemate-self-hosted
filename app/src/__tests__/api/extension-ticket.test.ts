import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/queries", () => ({ getUserByOwnerKey: vi.fn() }));

const USER_ID = "11111111-2222-4333-8444-555555555555";

describe("POST /api/extension/ticket", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    vi.stubEnv("INTERNAL_API_KEY", "test-signing-key");
  });

  it("requires a signed-in EngageMate user", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);
    const { POST } = await import("@/app/api/extension/ticket/route");

    expect((await POST()).status).toBe(401);
  });

  it("mints a no-store user-bound ticket", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: USER_ID } as never);
    const { POST } = await import("@/app/api/extension/ticket/route");
    const { verifyExtensionTicket } = await import("@/lib/extension-ticket");

    const response = await POST();
    const body = await response.json();
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(verifyExtensionTicket(body.ticket)).toBe(USER_ID);
    expect(body.expires_in).toBe(30 * 24 * 60 * 60);
  });
});
