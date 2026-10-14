import { beforeEach, describe, expect, it, vi } from "vitest";
import { unsubscribeToken } from "@/lib/email-unsubscribe";

const mocks = vi.hoisted(() => ({ unsubscribeUserFromCategory: vi.fn() }));
vi.mock("@/lib/queries", () => ({
  unsubscribeUserFromCategory: mocks.unsubscribeUserFromCategory,
}));

const USER = "11111111-1111-4111-8111-111111111111";

function post(body: unknown, serialize = true) {
  return new Request("http://localhost/api/email/unsubscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: serialize ? JSON.stringify(body) : String(body),
  });
}

describe("/api/email/unsubscribe", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.stubEnv("INTERNAL_API_KEY", "test-internal-key");
    mocks.unsubscribeUserFromCategory.mockResolvedValue(true);
  });

  it("turns off exactly the signed category for the signed user", async () => {
    const { POST } = await import("@/app/api/email/unsubscribe/route");
    const t = unsubscribeToken("test-internal-key", USER, "weekly_report");
    const response = await POST(post({ u: USER, c: "weekly_report", t }));

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ unsubscribed: "weekly_report" });
    expect(mocks.unsubscribeUserFromCategory).toHaveBeenCalledWith(
      USER,
      "weekly_report",
    );
  });

  it.each([
    { label: "a token for another category", body: { u: USER, c: "onboarding", t: unsubscribeToken("test-internal-key", USER, "activity") } },
    { label: "a forged token", body: { u: USER, c: "activity", t: "forged" } },
    { label: "a missing token", body: { u: USER, c: "activity" } },
    { label: "an array", body: [] },
  ])("refuses $label", async ({ body }) => {
    const { POST } = await import("@/app/api/email/unsubscribe/route");
    const response = await POST(post(body));
    expect(response.status).toBe(400);
    expect(mocks.unsubscribeUserFromCategory).not.toHaveBeenCalled();
  });

  it("refuses malformed JSON", async () => {
    const { POST } = await import("@/app/api/email/unsubscribe/route");
    const response = await POST(post("{", false));
    expect(response.status).toBe(400);
  });
});
