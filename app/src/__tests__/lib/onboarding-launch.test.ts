import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { launchOnboardingAccount } from "@/lib/onboarding-launch";

const mockFetch = vi.fn();
global.fetch = mockFetch;

describe("launchOnboardingAccount", () => {
  beforeEach(() => {
    mockFetch.mockReset();
    vi.spyOn(Intl.DateTimeFormat.prototype, "resolvedOptions").mockReturnValue({
      timeZone: "Asia/Makassar",
    } as Intl.ResolvedDateTimeFormatOptions);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("posts the selected account with the browser timezone", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ selected: "account-1" }), { status: 200 }),
    );
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ enabled: true }), { status: 200 }),
    );

    await launchOnboardingAccount("account-1", "Agent status was not saved");

    expect(mockFetch).toHaveBeenNthCalledWith(1, "/api/accounts/active", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ igAccountId: "account-1" }),
    });
    expect(mockFetch).toHaveBeenNthCalledWith(2, "/api/onboarding/launch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ig_account_id: "account-1",
        timezone: "Asia/Makassar",
      }),
    });
  });

  it("throws the server detail when launch is rejected", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ selected: "account-1" }), { status: 200 }),
    );
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Approve a target first" }), {
        status: 409,
      }),
    );

    await expect(
      launchOnboardingAccount("account-1", "Launch failed"),
    ).rejects.toThrow("Approve a target first");
  });

  it("uses the caller fallback when the error response is not JSON", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ selected: "account-1" }), { status: 200 }),
    );
    mockFetch.mockResolvedValueOnce(new Response("gateway error", { status: 502 }));

    await expect(
      launchOnboardingAccount("account-1", "Agent status was not saved"),
    ).rejects.toThrow("Agent status was not saved");
  });

  it("does not launch when the selected account cannot be persisted", async () => {
    mockFetch.mockResolvedValueOnce(new Response("unavailable", { status: 503 }));

    await expect(
      launchOnboardingAccount("account-1", "Agent status was not saved"),
    ).rejects.toThrow("Could not select the Instagram account for launch");

    expect(mockFetch).toHaveBeenCalledTimes(1);
  });
});
