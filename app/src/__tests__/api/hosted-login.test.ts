import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
vi.mock("@/lib/owner-auth", () => ({ auth: vi.fn() }));
vi.mock("@/lib/queries", () => ({ getUserByOwnerKey: vi.fn() }));
vi.mock("next/headers", () => ({ cookies: vi.fn(() => ({ set: setCookie })) }));
const { setCookie } = vi.hoisted(() => ({ setCookie: vi.fn() }));
import { auth } from "@/lib/owner-auth";
import { getUserByOwnerKey } from "@/lib/queries";
import { POST } from "@/app/api/instagram/connect/route";
import { GET, DELETE } from "@/app/api/instagram/connect/[id]/route";
import { GET as STEP } from "@/app/api/instagram/connect/[id]/step/route";
import { GET as VIEW } from "@/app/api/instagram/connect/[id]/view/route";
import { POST as INPUT } from "@/app/api/instagram/connect/[id]/input/route";
import { POST as COMPLETE } from "@/app/api/instagram/connect/[id]/complete/route";
import { cleanInput, cleanStep } from "@/lib/hosted-login-proxy";
import { hostedLoginDocument } from "@/lib/hosted-login-guided-view";
const id = "12345678-1234-1234-1234-123456789abc";
const params = { params: Promise.resolve({ id }) };
const url = "https://app.example.com/api/instagram/connect";
const active = {
  id,
  state: "active",
  expires_at: "2030-01-01T00:00:00Z",
  width: 1024,
  height: 768,
};
const fetchMock = vi.fn();
function req(
  method = "POST",
  body?: unknown,
  origin = "https://app.example.com",
) {
  return new Request(url, {
    method,
    headers: { Origin: origin, "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.stubEnv("HOSTED_LOGIN_ENABLED", "true");
  vi.stubEnv("NEXT_PUBLIC_APP_URL", "https://app.example.com");
  vi.stubEnv("INTERNAL_API_KEY", "test-private-key");
  vi.stubEnv("LOGIN_SERVICE_URL", "http://login:8000");
  vi.mocked(auth).mockResolvedValue({ userId: "clerk-owner" } as never);
  vi.mocked(getUserByOwnerKey).mockResolvedValue({
    id: "database-owner",
  } as never);
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe("hosted login boundary", () => {
  it("requires authentication and keeps feature disabled by default", async () => {
    vi.mocked(auth).mockResolvedValue({ userId: null } as never);
    expect((await POST(req("POST", {}))).status).toBe(401);
    vi.mocked(auth).mockResolvedValue({ userId: "clerk-owner" } as never);
    vi.stubEnv("HOSTED_LOGIN_ENABLED", "");
    expect((await GET(req("GET"), params)).status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it.each(["https://attacker.example", "null", ""])(
    "rejects write origin %s",
    async (origin) => {
      expect(
        (
          await POST(
            req("POST", { username: "demo_user", timezone: "UTC" }, origin),
          )
        ).status,
      ).toBe(403);
      expect(
        (await DELETE(req("DELETE", undefined, origin), params)).status,
      ).toBe(403);
      expect(
        (
          await INPUT(
            req("POST", { type: "key", key: "Enter" }, origin),
            params,
          )
        ).status,
      ).toBe(403);
      expect(
        (await COMPLETE(req("POST", undefined, origin), params)).status,
      ).toBe(403);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );
  it("derives tenant and destination server-side and strips extra fields", async () => {
    fetchMock.mockResolvedValue(
      Response.json({ ...active, cookie: "never-echo", proxy: "never-echo" }),
    );
    const r = await POST(
      req("POST", {
        username: "@demo_user",
        timezone: "UTC",
        userId: "attacker",
        url: "https://attacker.example",
      }),
    );
    expect(r.status).toBe(200);
    expect(fetchMock.mock.calls[0][0]).toBe("http://login:8000/v1/connect");
    const options = fetchMock.mock.calls[0][1];
    expect(options.headers["X-User-Id"]).toBe("database-owner");
    expect(options.headers["X-Internal-Api-Key"]).toBe("test-private-key");
    expect(JSON.parse(options.body)).toEqual({
      username: "demo_user",
      timezone: "UTC",
    });
    expect(await r.text()).not.toContain("never-echo");
    expect(r.headers.get("cache-control")).toContain("no-store");
  });
  it("rejects oversized chunked bodies, invalid input and path traversal before proxying", async () => {
    expect(
      (await POST(req("POST", { username: "x".repeat(9000), timezone: "UTC" })))
        .status,
    ).toBe(400);
    expect((await GET(req("GET"), { params: Promise.resolve({ id: "../other" }) })).status).toBe(
      400,
    );
    expect(
      (await INPUT(req("POST", { type: "click", x: 1024, y: 0 }), params))
        .status,
    ).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(cleanInput({ type: "text", text: "secret" })).toBeNull();
    expect(cleanInput({ type: "key", key: "Enter" })).toBeNull();
    expect(cleanInput({ type: "sign_in", username: "demo_user", password: "fixture", url: "unsafe" })).toEqual({
      type: "sign_in", username: "demo_user", password: "fixture",
    });
    expect(cleanInput({ type: "sign_in", username: "DEMO_user", password: "fixture" })).toBeNull();
    expect(cleanInput({ type: "sign_in", username: "demo_user", password: "bad\npass" })).toBeNull();
    expect(cleanInput({ type: "submit_code", code: "123456" })).toEqual({type:"submit_code",code:"123456"});
    expect(cleanInput({ type: "submit_code", code: "123\n456" })).toBeNull();
    expect(cleanInput({ type: "choose_action", action: "click_anything" })).toBeNull();
  });
  it("passes ownership checks through on every session operation and sanitizes errors", async () => {
    fetchMock.mockImplementation(() =>
      Promise.resolve(
        Response.json(
          { code: "session_not_found", secret: "upstream-secret" },
          { status: 404 },
        ),
      ),
    );
    for (const handler of [GET, STEP, VIEW]) {
      const r = await handler(req("GET"), params);
      expect(r.status).toBe(404);
      expect(await r.json()).toEqual({ code: "session_not_found" });
    }
    expect(fetchMock).toHaveBeenCalledTimes(3);
    fetchMock.mockResolvedValueOnce(
      Response.json(
        { detail: "cookie: secret", code: "secret" },
        { status: 500 },
      ),
    );
    expect(await (await GET(req("GET"), params)).json()).toEqual({
      code: "connection_unavailable",
    });
  });
  it("forwards only named sign-in steps and approved actions", async () => {
    expect(cleanStep({kind:"code",channel:"sms",invalid:false,options:["resend_code"],html:"private"})).toEqual({kind:"code",channel:"sms",invalid:false,options:["resend_code"]});
    expect(cleanStep({kind:"code",channel:"secret",invalid:false,options:[]})).toBeNull();
    expect(cleanStep({kind:"method",options:["click_anything"]})).toBeNull();
    fetchMock.mockResolvedValueOnce(Response.json({kind:"approval",options:["try_another_way"],html:"private"}));
    const response = await STEP(req("GET"), params);
    expect(await response.json()).toEqual({kind:"approval",options:["try_another_way"]});
    expect(fetchMock.mock.calls[0][0]).toBe(`http://login:8000/v1/connect/${id}/step`);
  });
  it("persists the verified selected account without exposing cookies or proxy configuration", async () => {
    fetchMock.mockResolvedValue(
      Response.json({
        ...active,
        state: "connected",
        account: {
          id,
          username: "demo_user",
          health: "active",
          proxy_bound: true,
          cookies: "secret",
        },
      }),
    );
    const result = await COMPLETE(req(), params);
    expect(await result.json()).toMatchObject({
      state: "connected",
      account: { id, username: "demo_user", healthStatus: "active" },
    });
    expect(setCookie).toHaveBeenCalledWith(
      expect.any(String),
      id,
      expect.objectContaining({ httpOnly: true, sameSite: "lax" }),
    );
  });
  it("serves an isolated no-analytics document with restrictive CSP after ownership verification", async () => {
    fetchMock.mockResolvedValue(Response.json({ ...active, username: "demo_user" }));
    const response = await VIEW(req("GET"), params);
    const html = await response.text();
    expect(response.headers.get("content-security-policy")).toContain(
      "frame-ancestors 'none'",
    );
    expect(response.headers.get("content-security-policy")).toContain(
      "script-src 'nonce-",
    );
    expect(response.headers.get("referrer-policy")).toBe("no-referrer");
    expect(html).toContain("Finish connecting");
    expect(html).toContain('type="password"');
    expect(html).toContain('id="username"');
    expect(html).toContain('id="code-form"');
    expect(html).toContain('expectedUsername = "demo_user"');
    expect(html).not.toMatch(/clarity|googletag|next\/script|<iframe|<img|\/frame/i);
  });
  it("keeps credentials out of the generated document and includes a runnable login handler", () => {
    const html = hostedLoginDocument(id, "fixture-nonce", active.expires_at, "demo_user");
    const script = html.match(/<script nonce="fixture-nonce">([\s\S]*?)<\/script>/)?.[1];
    expect(script).toBeTruthy();
    expect(() => new Function(script!)).not.toThrow();
    expect(html).toContain('type:"sign_in",username:name,password');
    expect(html).not.toContain("fixture-password");
  });
  it("aborts stalled upstream calls with fixed safe errors", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url, opts) =>
        new Promise((_resolve, reject) =>
          opts.signal.addEventListener("abort", () =>
            reject(new Error("secret upstream timeout")),
          ),
        ),
    );
    const pending = GET(req("GET"), params);
    await vi.advanceTimersByTimeAsync(15_001);
    expect(await (await pending).json()).toEqual({
      code: "connection_unavailable",
    });
  });
});

it("renders a completed connection so a reload can recover its receipt", async () => {
  fetchMock.mockResolvedValue(Response.json({
    ...active, state: "connected", expires_at: "2020-01-01T00:00:00Z",
    account: { id, username: "demo_user", health: "paused", proxy_bound: true },
  }));
  const response = await VIEW(req("GET"), params);
  expect(response.status).toBe(200);
  expect(await response.text()).toContain("Connection confirmed.");
});
