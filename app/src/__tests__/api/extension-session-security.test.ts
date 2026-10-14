import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { NextRequest } from "next/server";

vi.mock("@/lib/owner-auth", () => ({
  auth: vi.fn(),
}));

vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: vi.fn(),
}));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const SECRET_COOKIE = "IGSESSION-super-secret-cookie-value-xyz";
const CONTROL_BASE = "https://control.example.internal";
const USER_UUID = "11111111-2222-4333-8444-555555555555";

function sessionPayload(overrides: Record<string, unknown> = {}) {
  return {
    username: "story_owner",
    ig_user_id: "17841400000000000",
    cookies: [
      {
        name: "sessionid",
        value: SECRET_COOKIE,
        domain: ".instagram.com",
        path: "/",
        secure: true,
        httpOnly: true,
        sameSite: "None",
        expirationDate: 1780000000,
      },
      {
        name: "ds_user_id",
        value: "17841400000000000",
        domain: ".instagram.com",
        path: "/",
        secure: true,
        httpOnly: false,
        sameSite: "None",
        expirationDate: 1780000000,
      },
    ],
    user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
    login_geo: { country: "US", region: "CA", city: "San Francisco" },
    ...overrides,
  };
}

function makePost(
  body: Record<string, unknown> = sessionPayload(),
  authorization?: string,
) {
  return new NextRequest("http://localhost:3030/api/extension/session", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(authorization ? { Authorization: authorization } : {}),
    },
    body: JSON.stringify(body),
  });
}

describe("POST /api/extension/session (security)", () => {
  const originalEnv = { ...process.env };
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>;
  let consoleLogSpy: ReturnType<typeof vi.spyOn>;
  let consoleWarnSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    process.env = { ...originalEnv };
    process.env.CONTROL_API_URL = CONTROL_BASE;
    process.env.INTERNAL_API_KEY = "internal-test-key";
    vi.stubEnv("NODE_ENV", "test");
    delete process.env.PIPELINE_API_URL;
    delete process.env.PIPELINE_API_KEY;

    consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    consoleLogSpy = vi.spyOn(console, "log").mockImplementation(() => {});
    consoleWarnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    consoleErrorSpy.mockRestore();
    consoleLogSpy.mockRestore();
    consoleWarnSpy.mockRestore();
  });

  function loggedText(): string {
    const parts: unknown[] = [];
    for (const spy of [consoleErrorSpy, consoleLogSpy, consoleWarnSpy]) {
      for (const call of spy.mock.calls) {
        parts.push(...call);
      }
    }
    return parts
      .map((p) => (typeof p === "string" ? p : JSON.stringify(p)))
      .join("\n");
  }

  it("returns 401 when Clerk auth is missing", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);

    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(makePost());
    expect(res.status).toBe(401);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("returns 404 when EngageMate user row is missing", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_missing" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue(null);

    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(makePost());
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("accepts a valid extension ticket without consulting Clerk cookies", async () => {
    const { auth } = await import("@/lib/owner-auth");
    const { getUserByOwnerKey } = await import("@/lib/queries");
    const { mintExtensionTicket } = await import("@/lib/extension-ticket");
    const ticket = mintExtensionTicket(USER_UUID);
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ id: "account-1", username: "story_owner" }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(makePost(sessionPayload(), `Bearer ${ticket}`));

    expect(res.status).toBe(201);
    expect(auth).not.toHaveBeenCalled();
    expect(getUserByOwnerKey).not.toHaveBeenCalled();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Record<string, string>)["X-User-Id"]).toBe(
      USER_UUID,
    );
  });

  it("rejects an invalid extension ticket before forwarding cookies", async () => {
    const { auth } = await import("@/lib/owner-auth");
    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(
      makePost(sessionPayload(), "Bearer em_xt_invalid.invalid"),
    );

    expect(res.status).toBe(401);
    expect(auth).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards only to the configured Control API with trusted headers", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: USER_UUID,
      ownerKey: "clerk_1",
    } as never);

    const redacted = {
      id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      username: "story_owner",
      health: "active",
      captured_at: "2026-07-31T12:00:00.000Z",
      proxy_bound: true,
    };
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify(redacted), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      })
    );

    const { POST } = await import("@/app/api/extension/session/route");
    const body = sessionPayload();
    const res = await POST(makePost(body));
    expect(res.status).toBe(201);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${CONTROL_BASE}/v1/accounts/session`);
    expect(url.startsWith(CONTROL_BASE)).toBe(true);
    expect(url).not.toContain("PIPELINE");

    const headers = init.headers as Record<string, string>;
    expect(headers["X-Internal-Api-Key"]).toBe("internal-test-key");
    expect(headers["X-User-Id"]).toBe(USER_UUID);
    expect(headers["Content-Type"]).toBe("application/json");

    const forwarded = JSON.parse(String(init.body));
    expect(forwarded.cookies[0].value).toBe(SECRET_COOKIE);
    expect(forwarded.username).toBe("story_owner");
  });

  it("returns the redacted account response and never echoes cookies", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: USER_UUID,
      ownerKey: "clerk_1",
    } as never);

    const redacted = {
      id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      username: "story_owner",
      health: "active",
      captured_at: "2026-07-31T12:00:00.000Z",
      proxy_bound: false,
    };
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify(redacted), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );

    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(makePost());
    expect(res.status).toBe(200);
    const data = await res.json();
    expect(data).toEqual(redacted);
    expect(JSON.stringify(data)).not.toContain(SECRET_COOKIE);
    expect(data).not.toHaveProperty("cookies");
    expect(loggedText()).not.toContain(SECRET_COOKIE);
  });

  it("rejects non-HTTPS Control API base URL in production", async () => {
    vi.stubEnv("NODE_ENV", "production");
    process.env.CONTROL_API_URL = "http://control.internal:8000";

    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: USER_UUID,
      ownerKey: "clerk_1",
    } as never);

    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(makePost());
    expect(res.status).toBe(500);
    const data = await res.json();
    expect(data.error).toMatch(/https|misconfigured|control/i);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(JSON.stringify(data)).not.toContain(SECRET_COOKIE);
  });

  it("allows only the private Compose API URL over HTTP in production", async () => {
    vi.stubEnv("NODE_ENV", "production");
    process.env.CONTROL_API_URL = "http://api:8000";

    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: USER_UUID,
      ownerKey: "clerk_1",
    } as never);
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ id: USER_UUID, username: "story_owner" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(makePost());
    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api:8000/v1/accounts/session",
      expect.any(Object),
    );
  });

  it("strips cookie values from error responses when upstream body includes them", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: USER_UUID,
      ownerKey: "clerk_1",
    } as never);

    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: `validation failed for cookie ${SECRET_COOKIE}`,
          cookies: [{ name: "sessionid", value: SECRET_COOKIE }],
        }),
        { status: 502, headers: { "Content-Type": "application/json" } }
      )
    );

    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(makePost());
    expect(res.status).toBe(502);
    const text = await res.text();
    expect(text).not.toContain(SECRET_COOKIE);
    expect(loggedText()).not.toContain(SECRET_COOKIE);
  });

  it("never puts cookie values in error responses on network failure", async () => {
    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: USER_UUID,
      ownerKey: "clerk_1",
    } as never);

    fetchMock.mockRejectedValue(
      new Error(`upstream failed while handling ${SECRET_COOKIE}`)
    );

    const { POST } = await import("@/app/api/extension/session/route");
    const res = await POST(makePost());
    expect(res.status).toBe(502);
    const text = await res.text();
    expect(text).not.toContain(SECRET_COOKIE);
    expect(loggedText()).not.toContain(SECRET_COOKIE);
  });

  it("does not use PIPELINE_API_URL when CONTROL_API_URL is set", async () => {
    process.env.PIPELINE_API_URL = "https://pipeline.should-not-be-used.example";
    process.env.CONTROL_API_URL = CONTROL_BASE;

    const { auth } = await import("@/lib/owner-auth");
    vi.mocked(auth).mockResolvedValue({ userId: "clerk_1" } as never);
    const { getUserByOwnerKey } = await import("@/lib/queries");
    vi.mocked(getUserByOwnerKey).mockResolvedValue({
      id: USER_UUID,
      ownerKey: "clerk_1",
    } as never);

    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
          username: "story_owner",
          health: "active",
          captured_at: "2026-07-31T12:00:00.000Z",
          proxy_bound: true,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } }
      )
    );

    const { POST } = await import("@/app/api/extension/session/route");
    await POST(makePost());
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe(`${CONTROL_BASE}/v1/accounts/session`);
    expect(url).not.toContain("pipeline.should-not-be-used");
  });
});
