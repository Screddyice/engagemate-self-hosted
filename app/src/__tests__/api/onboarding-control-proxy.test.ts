import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/queries", () => ({ getUserByOwnerKey: vi.fn() }));

const mockFetch = vi.fn();
global.fetch = mockFetch;

describe("onboarding control API proxies", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    mockFetch.mockReset();
    vi.stubEnv("CONTROL_API_URL", "http://api:8000");
    vi.stubEnv("INTERNAL_API_KEY", "internal-test-key");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  async function authenticate() {
    const { auth } = await import("@/lib/owner-auth");
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-1" } as never);
    vi.mocked(getUserByOwnerKey).mockResolvedValue({ id: "user-1" } as never);
  }

  it("GET /api/onboarding/brand/profile returns the caller's persisted state", async () => {
    await authenticate();
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ profile_id: "profile-1", status: "complete" }), {
        status: 200,
      }),
    );
    const { GET } = await import("@/app/api/onboarding/brand/profile/route");

    const response = await GET(
      new Request(
        "http://localhost/api/onboarding/brand/profile?ig_account_id=account-1",
      ),
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      profile_id: "profile-1",
      status: "complete",
    });
    expect(mockFetch).toHaveBeenCalledWith(
      "http://api:8000/v1/onboarding/brand/profile?ig_account_id=account-1",
      expect.objectContaining({
        cache: "no-store",
      }),
    );
    const options = mockFetch.mock.calls[0][1] as RequestInit;
    expect(new Headers(options.headers).get("X-User-Id")).toBe("user-1");
  });

  it("POST /api/onboarding/launch forwards the owned account and timezone", async () => {
    await authenticate();
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ enabled: true, timezone: "Asia/Makassar" }), {
        status: 200,
      }),
    );
    const { POST } = await import("@/app/api/onboarding/launch/route");
    const request = new Request("http://localhost/api/onboarding/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ig_account_id: "account-1",
        timezone: "Asia/Makassar",
      }),
    });

    const response = await POST(request);

    expect(response.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledWith(
      "http://api:8000/v1/onboarding/launch",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          ig_account_id: "account-1",
          timezone: "Asia/Makassar",
        }),
      }),
    );
    const options = mockFetch.mock.calls[0][1] as RequestInit;
    expect(options.signal).toBeInstanceOf(AbortSignal);
  });

  it("returns a JSON 401 without calling the Control API", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);
    const { GET } = await import("@/app/api/onboarding/brand/profile/route");

    const response = await GET(
      new Request("http://localhost/api/onboarding/brand/profile"),
    );

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({ error: "Unauthorized" });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it.each([
    {
      label: "launch",
      load: () => import("@/app/api/onboarding/launch/route"),
      request: () =>
        new Request("http://localhost/api/onboarding/launch", {
          method: "POST",
          body: JSON.stringify({ ig_account_id: "account-1", timezone: "UTC" }),
        }),
    },
    {
      label: "candidate approval",
      load: () => import("@/app/api/onboarding/brand/approve/route"),
      request: () =>
        new Request("http://localhost/api/onboarding/brand/approve", {
          method: "POST",
          body: JSON.stringify({
            profile_id: "profile-1",
            candidate_ids: ["candidate-1"],
            ig_account_id: "account-1",
          }),
        }),
    },
    {
      label: "source audience approval",
      load: () => import("@/app/api/onboarding/brand/approve-sources/route"),
      request: () =>
        new Request("http://localhost/api/onboarding/brand/approve-sources", {
          method: "POST",
          body: JSON.stringify({
            profile_id: "profile-1",
            reference_urls: ["one", "two", "three"],
            ig_account_id: "account-1",
          }),
        }),
    },
    {
      label: "source draft save",
      load: () => import("@/app/api/onboarding/brand/sources/route"),
      request: () => new Request("http://localhost/api/onboarding/brand/sources", {
        method: "POST", body: JSON.stringify({ ig_account_id: "account-1", reference_urls: ["one", "two", "three"] }),
      }),
    },
    {
      label: "proxy profile capture",
      load: () => import("@/app/api/onboarding/profiles/route"),
      request: () => new Request("http://localhost/api/onboarding/profiles", {
        method: "POST", body: JSON.stringify({ ig_account_id: "account-1", usernames: ["one", "two", "three"] }),
      }),
    },
  ])("denies unauthenticated $label requests before database or upstream access", async ({ load, request }) => {
    const { auth } = await import("@/lib/owner-auth");
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);
    const { POST } = await load();

    const response = await POST(request());

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({ error: "Unauthorized" });
    expect(getUserByOwnerKey).not.toHaveBeenCalled();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns a JSON 404 when the Clerk user has no EngageMate row", async () => {
    const { auth } = await import("@/lib/owner-auth");
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-missing" } as never);
    vi.mocked(getUserByOwnerKey).mockResolvedValue(null);
    const { POST } = await import("@/app/api/onboarding/launch/route");

    const response = await POST(
      new Request("http://localhost/api/onboarding/launch", {
        method: "POST",
        body: JSON.stringify({ ig_account_id: "account-1", timezone: "UTC" }),
      }),
    );

    expect(response.status).toBe(404);
    await expect(response.json()).resolves.toEqual({ error: "User not found" });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns a JSON 503 when the Control API is not configured", async () => {
    await authenticate();
    vi.stubEnv("CONTROL_API_URL", "");
    const { GET } = await import("@/app/api/onboarding/brand/profile/route");

    const response = await GET(
      new Request(
        "http://localhost/api/onboarding/brand/profile?ig_account_id=account-1",
      ),
    );

    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toEqual({
      error: "Onboarding service is not configured",
    });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("rejects malformed launch JSON before calling the Control API", async () => {
    await authenticate();
    const { POST } = await import("@/app/api/onboarding/launch/route");

    const response = await POST(
      new Request("http://localhost/api/onboarding/launch", {
        method: "POST",
        body: "{",
      }),
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ error: "Invalid JSON" });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns a JSON 502 when the Control API cannot be reached", async () => {
    await authenticate();
    mockFetch.mockRejectedValueOnce(new TypeError("network unavailable"));
    const { GET } = await import("@/app/api/onboarding/brand/profile/route");

    const response = await GET(
      new Request(
        "http://localhost/api/onboarding/brand/profile?ig_account_id=account-1",
      ),
    );

    expect(response.status).toBe(502);
    await expect(response.json()).resolves.toEqual({
      error: "Onboarding service is unavailable",
    });
  });

  it("rejects malformed upstream JSON as a 502", async () => {
    await authenticate();
    mockFetch.mockResolvedValueOnce(new Response("gateway html", { status: 200 }));
    const { POST } = await import("@/app/api/onboarding/launch/route");

    const response = await POST(
      new Request("http://localhost/api/onboarding/launch", {
        method: "POST",
        body: JSON.stringify({ ig_account_id: "account-1", timezone: "UTC" }),
      }),
    );

    expect(response.status).toBe(502);
    await expect(response.json()).resolves.toEqual({
      error: "Onboarding service returned an invalid response",
    });
  });

  it("normalizes an upstream detail while preserving its status", async () => {
    await authenticate();
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "save the account voice before launch" }), {
        status: 409,
      }),
    );
    const { POST } = await import("@/app/api/onboarding/launch/route");

    const response = await POST(
      new Request("http://localhost/api/onboarding/launch", {
        method: "POST",
        body: JSON.stringify({ ig_account_id: "account-1", timezone: "UTC" }),
      }),
    );

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toEqual({
      error: "save the account voice before launch",
    });
  });

  it("normalizes FastAPI validation details into a stable public error", async () => {
    await authenticate();
    mockFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          detail: [
            {
              loc: ["body", "timezone"],
              msg: "Field required",
              type: "missing",
            },
          ],
        }),
        { status: 422 },
      ),
    );
    const { POST } = await import("@/app/api/onboarding/launch/route");

    const response = await POST(
      new Request("http://localhost/api/onboarding/launch", {
        method: "POST",
        body: JSON.stringify({ ig_account_id: "account-1" }),
      }),
    );

    expect(response.status).toBe(422);
    await expect(response.json()).resolves.toEqual({
      error: "timezone: Field required",
      code: "validation_error",
    });
  });

  it("preserves lightweight polling and Retry-After through the proxy", async () => {
    await authenticate();
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Poll later" }), {
        status: 429,
        headers: { "Retry-After": "120" },
      }),
    );
    const { GET } = await import("@/app/api/onboarding/brand/profile/route");

    const response = await GET(
      new Request(
        "http://localhost/api/onboarding/brand/profile?details=false&ig_account_id=account-1",
      ),
    );

    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("120");
    expect(mockFetch).toHaveBeenCalledWith(
      "http://api:8000/v1/onboarding/brand/profile?ig_account_id=account-1&details=false",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("requires an Instagram account before polling profile state", async () => {
    await authenticate();
    const { GET } = await import("@/app/api/onboarding/brand/profile/route");

    const response = await GET(
      new Request("http://localhost/api/onboarding/brand/profile"),
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({
      error: "Instagram account is required",
    });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("returns JSON auth errors from brand analysis", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);
    const { POST } = await import("@/app/api/onboarding/brand/route");

    const response = await POST(
      new Request("http://localhost/api/onboarding/brand", {
        method: "POST",
        body: JSON.stringify({}),
      }),
    );

    expect(response.status).toBe(401);
    await expect(response.json()).resolves.toEqual({ error: "Unauthorized" });
  });

  it("returns a bounded JSON error when candidate approval cannot reach Control", async () => {
    await authenticate();
    mockFetch.mockRejectedValueOnce(new TypeError("network unavailable"));
    const { POST } = await import("@/app/api/onboarding/brand/approve/route");

    const response = await POST(
      new Request("http://localhost/api/onboarding/brand/approve", {
        method: "POST",
        body: JSON.stringify({ candidate_ids: ["candidate-1"] }),
      }),
    );

    expect(response.status).toBe(502);
    await expect(response.json()).resolves.toEqual({
      error: "Onboarding service is unavailable",
    });
  });

  it("rejects malformed candidate approval JSON before forwarding it", async () => {
    await authenticate();
    const { POST } = await import("@/app/api/onboarding/brand/approve/route");

    const response = await POST(
      new Request("http://localhost/api/onboarding/brand/approve", {
        method: "POST",
        body: "{",
      }),
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ error: "Invalid JSON" });
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
