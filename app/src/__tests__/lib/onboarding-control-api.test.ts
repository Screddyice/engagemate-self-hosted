import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getOnboardingBrandProfile,
  requestOnboardingControl,
} from "@/lib/onboarding-control-api";

const mockFetch = vi.fn();
global.fetch = mockFetch;

const completeProfile = {
  profile_id: "profile-1",
  ig_account_id: "account-1",
  status: "complete" as const,
  reference_urls: ["one", "two", "three"],
  answers: { voice: "Direct" },
  analysis: null,
  candidates: [],
};

describe("getOnboardingBrandProfile", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockFetch.mockReset();
    vi.stubEnv("CONTROL_API_URL", "http://api:8000");
    vi.stubEnv("INTERNAL_API_KEY", "internal-test-key");
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns the persisted profile", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify(completeProfile), { status: 200 }),
    );

    await expect(getOnboardingBrandProfile("user-1", "account-1")).resolves.toEqual(
      completeProfile,
    );
    expect(mockFetch).toHaveBeenCalledWith(
      "http://api:8000/v1/onboarding/brand/profile?ig_account_id=account-1",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("returns null when the user has no profile", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "missing" }), { status: 404 }),
    );

    await expect(
      getOnboardingBrandProfile("user-1", "account-1"),
    ).resolves.toBeNull();
  });

  it("throws the bounded upstream error", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "Profile unavailable" }), {
        status: 503,
      }),
    );

    await expect(getOnboardingBrandProfile("user-1", "account-1")).rejects.toThrow(
      "Profile unavailable",
    );
  });

  it("aborts a stalled profile request after ten seconds", async () => {
    vi.useFakeTimers();
    let upstreamSignal: AbortSignal | undefined;
    mockFetch.mockImplementationOnce((_url: string, init: RequestInit) => {
      upstreamSignal = init.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        upstreamSignal?.addEventListener(
          "abort",
          () => reject(new Error("aborted")),
          { once: true },
        );
      });
    });

    const result = requestOnboardingControl(
      "/v1/onboarding/brand/profile",
      "user-1",
    );
    await vi.advanceTimersByTimeAsync(9_999);
    expect(upstreamSignal?.aborted).toBe(false);
    await vi.advanceTimersByTimeAsync(1);

    await expect(result).resolves.toEqual({
      status: 502,
      body: { error: "Onboarding service is unavailable" },
    });
    expect(upstreamSignal?.aborted).toBe(true);
  });

  it("propagates caller cancellation to the upstream request", async () => {
    const caller = new AbortController();
    let upstreamSignal: AbortSignal | undefined;
    mockFetch.mockImplementationOnce((_url: string, init: RequestInit) => {
      upstreamSignal = init.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        upstreamSignal?.addEventListener(
          "abort",
          () => reject(new Error("aborted")),
          { once: true },
        );
      });
    });

    const result = requestOnboardingControl(
      "/v1/onboarding/launch",
      "user-1",
      { signal: caller.signal },
    );
    caller.abort();

    await expect(result).resolves.toEqual({
      status: 502,
      body: { error: "Onboarding service is unavailable" },
    });
    expect(upstreamSignal?.aborted).toBe(true);
  });

  it("rejects malformed upstream JSON", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response("gateway html", { status: 200 }),
    );

    await expect(getOnboardingBrandProfile("user-1", "account-1")).rejects.toThrow(
      "Onboarding service returned an invalid response",
    );
  });

  it("preserves delta-seconds Retry-After when a 429 body is malformed", async () => {
    mockFetch.mockResolvedValueOnce(
      new Response("gateway html", {
        status: 429,
        headers: { "Retry-After": "120" },
      }),
    );

    await expect(
      requestOnboardingControl("/v1/onboarding/brand/profile", "user-1"),
    ).resolves.toEqual({
      status: 502,
      body: { error: "Onboarding service returned an invalid response" },
      headers: { "Retry-After": "120" },
    });
  });

  it("preserves an HTTP-date Retry-After when a 503 body is not an object", async () => {
    const retryAfter = "Fri, 05 Sep 2026 01:02:03 GMT";
    mockFetch.mockResolvedValueOnce(
      new Response(JSON.stringify(["temporarily unavailable"]), {
        status: 503,
        headers: { "Retry-After": retryAfter },
      }),
    );

    await expect(
      requestOnboardingControl("/v1/onboarding/brand/profile", "user-1"),
    ).resolves.toEqual({
      status: 502,
      body: { error: "Onboarding service returned an invalid response" },
      headers: { "Retry-After": retryAfter },
    });
  });
});
