import { beforeEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/queries", () => ({ getUserByOwnerKey: vi.fn() }));

const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);

const USER_UUID = "11111111-2222-4333-8444-555555555555";

function request(
  authorization: string,
  options: {
    body?: Record<string, unknown>;
    headers?: Record<string, string>;
  } = {},
) {
  return new NextRequest("http://localhost:3030/api/extension/suggest-geo", {
    method: "POST",
    headers: {
      Authorization: authorization,
      "Content-Type": "application/json",
      ...options.headers,
    },
    body: JSON.stringify(options.body ?? { timezone: "America/Chicago" }),
  });
}

describe("POST /api/extension/suggest-geo", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    vi.stubEnv("CONTROL_API_URL", "https://control.example.internal");
    vi.stubEnv("INTERNAL_API_KEY", "internal-test-key");
    vi.stubEnv("NODE_ENV", "test");
  });

  it("accepts a user-bound extension ticket and forwards the user id", async () => {
    const { auth } = await import("@/lib/owner-auth");
    const { getUserByOwnerKey } = await import("@/lib/queries");
    const { mintExtensionTicket } = await import("@/lib/extension-ticket");
    const ticket = mintExtensionTicket(USER_UUID);
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          recommended: { country: "US", region: "TX", city: "Austin" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const { POST } = await import("@/app/api/extension/suggest-geo/route");
    const response = await POST(request(`Bearer ${ticket}`));

    expect(response.status).toBe(200);
    expect(auth).not.toHaveBeenCalled();
    expect(getUserByOwnerKey).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledWith(
      "https://control.example.internal/v1/accounts/suggest-login-geo",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "X-Internal-Api-Key": "internal-test-key",
          "X-User-Id": USER_UUID,
        }),
      }),
    );
  });

  it("rejects an invalid extension ticket before calling the Control API", async () => {
    const { auth } = await import("@/lib/owner-auth");
    const { POST } = await import("@/app/api/extension/suggest-geo/route");
    const response = await POST(request("Bearer em_xt_invalid.invalid"));

    expect(response.status).toBe(401);
    expect(auth).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses the trusted request IP to derive coordinates before selecting inventory", async () => {
    const { mintExtensionTicket } = await import("@/lib/extension-ticket");
    const ticket = mintExtensionTicket(USER_UUID);
    fetchMock
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            success: true,
            ip: "203.0.113.10",
            latitude: -8.65,
            longitude: 115.22,
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            recommended: { country: "ID", region: "BA", city: "Denpasar" },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );

    const { POST } = await import("@/app/api/extension/suggest-geo/route");
    const response = await POST(
      request(`Bearer ${ticket}`, {
        headers: { "x-forwarded-for": "198.51.100.4, 203.0.113.10" },
      }),
    );

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe("https://ipwho.is/203.0.113.10");
    const [, controlInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(controlInit.body))).toEqual({
      timezone: "America/Chicago",
      latitude: -8.65,
      longitude: 115.22,
    });
  });

  it("does not let the extension choose its own IP or coordinates", async () => {
    const { mintExtensionTicket } = await import("@/lib/extension-ticket");
    const ticket = mintExtensionTicket(USER_UUID);
    fetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          recommended: { country: "US", region: "TX", city: "Austin" },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const { POST } = await import("@/app/api/extension/suggest-geo/route");
    const response = await POST(
      request(`Bearer ${ticket}`, {
        body: {
          timezone: "America/Chicago",
          source_ip: "8.8.8.8",
          latitude: 1,
          longitude: 2,
        },
      }),
    );

    expect(response.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, controlInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(controlInit.body))).toEqual({
      timezone: "America/Chicago",
    });
  });
});
