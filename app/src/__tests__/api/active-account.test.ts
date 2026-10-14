import { beforeEach, describe, expect, it, vi } from "vitest";

const setMock = vi.fn();

vi.mock("next/headers", () => ({
  cookies: () => ({ set: setMock }),
}));

vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
  getAccountById: vi.fn(),
}));

function request(igAccountId: string) {
  return new Request("http://localhost/api/accounts/active", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ igAccountId }),
  });
}

describe("POST /api/accounts/active", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("rejects unauthenticated switches", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);
    const { POST } = await import("@/app/api/accounts/active/route");

    expect((await POST(request("account-1"))).status).toBe(401);
    expect(setMock).not.toHaveBeenCalled();
  });

  it("rejects a non-owned account without setting the cookie", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-1" } as never);
    const queries = await import("@/lib/queries");
    vi.mocked(queries.getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(queries.getAccountById).mockResolvedValue(null);
    const { POST } = await import("@/app/api/accounts/active/route");

    expect((await POST(request("account-2"))).status).toBe(404);
    expect(setMock).not.toHaveBeenCalled();
  });

  it("persists an owned account in an HTTP-only cookie", async () => {
    vi.stubEnv("NODE_ENV", "production");
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-1" } as never);
    const queries = await import("@/lib/queries");
    vi.mocked(queries.getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(queries.getAccountById).mockResolvedValue({
      id: "account-2",
      userId: "user-1",
    } as never);
    const { POST } = await import("@/app/api/accounts/active/route");

    const response = await POST(request("account-2"));
    expect(response.status).toBe(200);
    expect(setMock).toHaveBeenCalledWith(
      "engagemate_ig_account",
      "account-2",
      expect.objectContaining({
        httpOnly: true,
        sameSite: "lax",
        secure: true,
        path: "/",
      }),
    );
  });
});
