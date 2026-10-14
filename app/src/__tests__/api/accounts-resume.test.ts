import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
  getAccountById: vi.fn(),
}));
vi.mock("@/lib/onboarding-control-api", () => ({
  requestOnboardingControl: vi.fn(),
}));

describe("POST /api/accounts/[id]/resume", () => {
  let POST: (
    request: Request,
    context: { params: Promise<{ id: string }> },
  ) => Promise<Response>;

  beforeEach(async () => {
    vi.resetModules();
    POST = (await import("@/app/api/accounts/[id]/resume/route")).POST;
    vi.clearAllMocks();
  });

  const request = () =>
    new Request("http://localhost/api/accounts/account-1/resume", {
      method: "POST",
    });

  const context = { params: Promise.resolve({ id: "account-1" }) };

  it("requires a signed-in user", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const response = await POST(request(), context);

    expect(response.status).toBe(401);
  });

  it("does not forward resume for an account the user does not own", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-user" } as never);
    const { getUserByOwnerKey, getAccountById } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getAccountById).mockResolvedValue(null);

    const response = await POST(request(), context);
    const { requestOnboardingControl } = await import(
      "@/lib/onboarding-control-api"
    );

    expect(response.status).toBe(404);
    expect(requestOnboardingControl).not.toHaveBeenCalled();
  });

  it("requires a fresh account session before resume", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-user" } as never);
    const { getUserByOwnerKey, getAccountById } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getAccountById).mockResolvedValue({
      id: "account-1",
      username: "target_owner",
      healthStatus: "paused",
      pausedAt: new Date("2026-09-24T10:00:00Z"),
    } as never);
    const { requestOnboardingControl } = await import(
      "@/lib/onboarding-control-api"
    );
    vi.mocked(requestOnboardingControl).mockResolvedValue({
      status: 200,
      body: {
        id: "account-1",
        username: "target_owner",
        captured_at: "2026-09-24T09:00:00Z",
        health: "active",
      },
    } as never);

    const response = await POST(request(), context);

    expect(response.status).toBe(409);
    expect(requestOnboardingControl).toHaveBeenCalledTimes(1);
  });

  it("forwards an owned account resume to the tenant-scoped control API", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-user" } as never);
    const { getUserByOwnerKey, getAccountById } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getAccountById).mockResolvedValue({
      id: "account-1",
      username: "target_owner",
      healthStatus: "paused",
      pausedAt: new Date("2026-09-24T10:00:00Z"),
    } as never);
    const { requestOnboardingControl } = await import(
      "@/lib/onboarding-control-api"
    );
    vi.mocked(requestOnboardingControl)
      .mockResolvedValueOnce({
        status: 200,
        body: {
          id: "account-1",
          username: "target_owner",
          captured_at: "2026-09-24T11:00:00Z",
          health: "active",
        },
      } as never)
      .mockResolvedValueOnce({
      status: 200,
      body: { id: "account-1", health: "active" },
      } as never);

    const response = await POST(request(), context);
    expect(requestOnboardingControl).toHaveBeenNthCalledWith(
      1,
      "/v1/accounts/account-1",
      "user-1",
      expect.objectContaining({ method: "GET" }),
    );
    expect(requestOnboardingControl).toHaveBeenNthCalledWith(
      2,
      "/v1/accounts/account-1/resume",
      "user-1",
      expect.objectContaining({ method: "POST" }),
    );
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      id: "account-1",
      health: "active",
    });
  });
});
