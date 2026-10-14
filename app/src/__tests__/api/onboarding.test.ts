import { describe, it, expect, vi, beforeEach } from "vitest";

vi.mock("@/lib/owner-auth", () => ({
  auth: vi.fn(),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
  getAccountById: vi.fn(),
  getUserAccounts: vi.fn(),
  getAgentConfig: vi.fn(),
}));

vi.mock("@/db", () => {
  const updateMock = vi.fn();
  const insertMock = vi.fn();
  const deleteMock = vi.fn();
  const selectMock = vi.fn();
  return {
    db: {
      update: updateMock,
      insert: insertMock,
      delete: deleteMock,
      select: selectMock,
    },
    __updateMock: updateMock,
    __insertMock: insertMock,
    __deleteMock: deleteMock,
    __selectMock: selectMock,
  };
});

function profileRequest(body: unknown) {
  return new Request("http://localhost/api/onboarding/profile", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function voiceRequest(body: unknown) {
  return new Request("http://localhost/api/onboarding/voice", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

describe("Onboarding API Routes", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  // ─── Profile Route ──────────────────────────────────────

  // Test 59
  it("PUT /api/onboarding/profile should return 401 when not authenticated", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const { PUT } = await import("@/app/api/onboarding/profile/route");
    const res = await PUT(profileRequest({ name: "Test" }));
    expect(res.status).toBe(401);
  });

  // Test 60
  it("PUT /api/onboarding/profile should return 404 when the user row is missing", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue(null);

    const { PUT } = await import("@/app/api/onboarding/profile/route");
    const res = await PUT(profileRequest({ name: "Test" }));
    expect(res.status).toBe(404);
  });

  // Test 61
  it("PUT /api/onboarding/profile should reject a blank name", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);

    const { PUT } = await import("@/app/api/onboarding/profile/route");
    const res = await PUT(profileRequest({ name: "   " }));
    expect(res.status).toBe(400);
  });

  // ─── Voice Route ────────────────────────────────────────

  // Test 62
  it("PUT /api/onboarding/voice should return 401 when not authenticated", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const { PUT } = await import("@/app/api/onboarding/voice/route");
    const res = await PUT(voiceRequest({ persona: "test" }));
    expect(res.status).toBe(401);
  });

  // Test 63
  it("PUT /api/onboarding/voice should reject a blank persona", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);

    const { PUT } = await import("@/app/api/onboarding/voice/route");
    const res = await PUT(voiceRequest({ persona: "  " }));
    expect(res.status).toBe(400);
  });

  // Test 64
  it("PUT /api/onboarding/voice should 404 when the named account is not the caller's", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey, getAccountById } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getAccountById).mockResolvedValue(null);

    const { PUT } = await import("@/app/api/onboarding/voice/route");
    const res = await PUT(
      voiceRequest({ igAccountId: "someone-elses", persona: "hello" })
    );
    expect(res.status).toBe(404);
    expect(vi.mocked(getAccountById)).toHaveBeenCalledWith(
      "user-1",
      "someone-elses"
    );
  });

  // Test 65
  it("PUT /api/onboarding/voice should require igAccountId when several accounts exist", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const { getUserByOwnerKey, getUserAccounts } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
    vi.mocked(getUserAccounts).mockResolvedValue([
      { id: "acct-1" },
      { id: "acct-2" },
    ] as never);

    const { PUT } = await import("@/app/api/onboarding/voice/route");
    const res = await PUT(voiceRequest({ persona: "hello" }));
    expect(res.status).toBe(400);
  });

  it("PUT /api/onboarding/voice should require a connected Instagram account", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);

    const queries = await import("@/lib/queries") as unknown as {
      getUserByOwnerKey: ReturnType<typeof vi.fn>;
      getUserAccounts: ReturnType<typeof vi.fn>;
    };
    queries.getUserByOwnerKey.mockResolvedValue({ id: "user-1" });
    queries.getUserAccounts.mockResolvedValue([]);

    const { PUT } = await import("@/app/api/onboarding/voice/route");
    const res = await PUT(voiceRequest({ persona: "  Clear and human  " }));

    expect(res.status).toBe(409);
    await expect(res.json()).resolves.toEqual({
      error: "Connect Instagram before saving a voice",
    });
  });
});
