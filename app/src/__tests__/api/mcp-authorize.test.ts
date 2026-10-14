import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  auth: vi.fn(),
  getUserByOwnerKey: vi.fn(),
}));

vi.mock("@/lib/owner-auth", () => ({ auth: mocks.auth }));
vi.mock("@/lib/queries", () => ({
  getUserByOwnerKey: mocks.getUserByOwnerKey,
}));

import { GET, POST } from "@/app/api/mcp/authorize/route";

const STATE = "s".repeat(48);
const USER = { id: "11111111-1111-4111-8111-111111111111" };

describe("MCP authorization proxy", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mocks.auth.mockResolvedValue({ userId: "clerk-user" });
    mocks.getUserByOwnerKey.mockResolvedValue(USER);
    process.env.CONTROL_API_URL = "http://api:8000";
    process.env.INTERNAL_API_KEY = "internal-test-key";
  });

  it("requires a signed-in, known EngageMate user", async () => {
    mocks.auth.mockResolvedValueOnce({ userId: null });
    const signedOut = await GET(
      new Request(`http://app.test/api/mcp/authorize?state=${STATE}`),
    );
    expect(signedOut.status).toBe(401);

    mocks.auth.mockResolvedValueOnce({ userId: "clerk-user" });
    mocks.getUserByOwnerKey.mockResolvedValueOnce(null);
    const unknown = await GET(
      new Request(`http://app.test/api/mcp/authorize?state=${STATE}`),
    );
    expect(unknown.status).toBe(404);
  });

  it("forwards approval with trusted headers and returns a narrow response", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json({
        continue_url: "https://agent.example/callback?code=opaque&state=client-state",
      }),
    );

    const response = await POST(
      new Request("http://app.test/api/mcp/authorize", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          state: STATE,
          approved: true,
          approved_scopes: ["accounts:read"],
        }),
      }),
    );

    expect(response.status).toBe(200);
    const [, init] = fetchMock.mock.calls[0];
    const headers = init?.headers as Record<string, string>;
    expect(headers["X-Internal-Api-Key"]).toBe("internal-test-key");
    expect(headers["X-User-Id"]).toBe(USER.id);
    const body = await response.json();
    expect(body).toEqual({
      continue_url: "https://agent.example/callback?code=opaque&state=client-state",
    });
    expect(JSON.stringify(body)).not.toContain("internal-test-key");
    expect(JSON.stringify(body)).not.toContain(USER.id);
  });

  it("does not forward an invalid state", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const response = await POST(
      new Request("http://app.test/api/mcp/authorize", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          state: "bad",
          approved: true,
          approved_scopes: ["accounts:read"],
        }),
      }),
    );
    expect(response.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
